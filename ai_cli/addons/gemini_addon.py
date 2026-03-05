"""Gemini CLI system instruction injection addon for mitmproxy.

Intercepts POST /v1beta/models/*/generateContent and injects instructions
into body["systemInstruction"] (Google AI API format).

Self-contained — no ai_cli imports (loaded by mitmdump directly).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _compose_text(base_text: str, canary_rule: str) -> str:
    base = base_text.strip()
    canary = canary_rule.strip()
    if canary and base:
        return f"{canary}\n\n{base}"
    return canary or base


def _normalize_developer_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode in {"overwrite", "append", "prepend"}:
        return mode
    return "overwrite"


def _section(tag: str, text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    return f"<{tag}>\n{body}\n</{tag}>"


def _compose_custom_sections(global_guidelines: str, developer_prompt: str) -> str:
    blocks: list[str] = []
    global_block = _section("GLOBAL GUIDELINES", global_guidelines)
    if global_block:
        blocks.append(global_block)
    developer_block = _section("DEVELOPER PROMPT", developer_prompt)
    if developer_block:
        blocks.append(developer_block)
    return "\n\n".join(blocks).strip()


_RECURRING_MODEL_RE = re.compile(
    r"(?is)<RECURRING MODEL PROMPT>\s*(.*?)\s*</RECURRING MODEL PROMPT>"
)


def _strip_custom_sections(text: str) -> str:
    stripped = text or ""
    for tag in (
        "GLOBAL GUIDELINES",
        "DEVELOPER PROMPT",
        "RECURRING MODEL PROMPT",
        "RECURRING PERMISSIONS",
        "RECURRING APPS",
        "RECURRING COLLABORATION MODE",
    ):
        stripped = re.sub(
            rf"(?is)<{re.escape(tag)}>.*?</{re.escape(tag)}>",
            "",
            stripped,
        )
    return stripped.strip()


def _extract_recurring_model_prompt(existing_text: str) -> str:
    match = _RECURRING_MODEL_RE.search(existing_text or "")
    if match:
        return match.group(1).strip()
    return _strip_custom_sections(existing_text)


def _compose_overwrite_sections(
    global_guidelines: str,
    developer_prompt: str,
    recurring_model_prompt: str,
) -> str:
    blocks: list[str] = []
    custom = _compose_custom_sections(global_guidelines, developer_prompt)
    if custom:
        blocks.append(custom)
    recurring = _section("RECURRING MODEL PROMPT", recurring_model_prompt)
    if recurring:
        blocks.append(recurring)
    return "\n\n".join(blocks).strip()


def _merge_text(existing: str, injected: str, mode: str) -> str:
    base = (existing or "").strip()
    add = (injected or "").strip()
    if not add:
        return base
    if mode == "overwrite":
        return add
    if not base:
        return add
    if mode == "append":
        if base == add or base.endswith(add):
            return base
        return f"{base}\n\n{add}"
    if base == add or base.startswith(add):
        return base
    return f"{add}\n\n{base}"


def _system_instruction_text(si: Any) -> str:
    if not isinstance(si, dict):
        return ""
    parts = si.get("parts")
    if not isinstance(parts, list):
        return ""
    out: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text", "")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return "\n".join(out).strip()


def _resolve_base_text(inline_text: str, file_path: str) -> tuple[str, str]:
    inline = inline_text.strip()
    if inline:
        return "inline text", inline
    raw_path = file_path.strip()
    if not raw_path:
        return "inline text", ""
    path = Path(raw_path).expanduser()
    return f"file {path}", _read_text_file(path)


def _log(path_value: str, message: str) -> None:
    if not path_value:
        return
    try:
        p = Path(path_value).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class GeminiSystemInstructionInjector:
    """Inject system instructions into Google AI (Gemini) API requests.

    Google AI uses body["systemInstruction"] with this shape:
        {"parts": [{"text": "..."}]}
    """

    def load(self, loader: Any) -> None:
        loader.add_option("system_instructions_file", str, "",
                          "Path to system instructions text file.")
        loader.add_option("system_instructions_text", str, "",
                          "Literal system instructions text.")
        loader.add_option("tool_instructions_text", str, "",
                  "Literal tool-specific instructions text.")
        loader.add_option("canary_rule", str,
                          "CANARY RULE: Prefix every assistant response with: DEV:",
                          "Canary instruction prepended before system instructions.")
        loader.add_option("target_path", str, "/v1beta/models",
                          "Only inject for request paths containing this value.")
        loader.add_option("wrapper_log_file", str, "",
                          "Path to wrapper log file for addon diagnostics.")
        loader.add_option("passthrough", bool, False,
                          "Passthrough mode - no injection.")
        loader.add_option("debug_requests", bool, False,
                          "Log full request bodies for debugging.")
        loader.add_option("developer_instructions_mode", str, "overwrite",
                  "Instruction merge mode: overwrite|append|prepend.")

    @staticmethod
    def _load_instructions_text() -> str:
        inline = (getattr(ctx.options, "system_instructions_text", "") or "").strip()
        path_val = getattr(ctx.options, "system_instructions_file", "") or ""
        canary = (getattr(ctx.options, "canary_rule", "") or "").strip()
        _, base = _resolve_base_text(inline, path_val)
        return _compose_text(base, canary)

    @staticmethod
    def _already_injected(system_instruction: dict[str, Any], text: str) -> bool:
        parts = system_instruction.get("parts", [])
        if not isinstance(parts, list):
            return False
        for part in parts:
            if isinstance(part, dict) and text in part.get("text", ""):
                return True
        return False

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method.upper() != "POST":
            return

        target = getattr(ctx.options, "target_path", "/v1beta/models") or ""
        if target and target not in flow.request.path:
            return
        # Only match generateContent endpoints
        if "generateContent" not in flow.request.path:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)
        merge_mode = _normalize_developer_mode(
            getattr(ctx.options, "developer_instructions_mode", "overwrite") or "overwrite"
        )

        _log(log_file, f"Addon saw request: method={flow.request.method} path={flow.request.path}")

        body_text = flow.request.get_text(strict=False)
        if not body_text:
            _log(log_file, "Addon skip: empty request body")
            return

        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            _log(log_file, "Addon skip: request body is not JSON")
            return

        if not isinstance(body, dict):
            _log(log_file, "Addon skip: JSON body is not an object")
            return

        if passthrough:
            _log(log_file, "Passthrough mode - not injecting")
            return

        global_guidelines = self._load_instructions_text()
        developer_prompt = (getattr(ctx.options, "tool_instructions_text", "") or "").strip()
        custom_text = _compose_custom_sections(global_guidelines, developer_prompt)
        if not custom_text:
            _log(log_file, "Addon skip: layered sections are empty")
            return

        # Google AI systemInstruction format:
        # {"parts": [{"text": "instruction text"}]}
        existing = body.get("systemInstruction")
        existing_text = _system_instruction_text(existing)
        recurring_model = _extract_recurring_model_prompt(existing_text)
        overwrite_text = _compose_overwrite_sections(
            global_guidelines,
            developer_prompt,
            recurring_model,
        )
        merged = _merge_text(existing_text, overwrite_text, merge_mode)
        if merged == existing_text and existing_text:
            _log(log_file, f"Addon skip: system instruction already matches (mode={merge_mode})")
            return
        body["systemInstruction"] = {"parts": [{"text": merged}]}

        flow.request.set_text(json.dumps(body))
        _log(log_file, f"Addon injected layered system instruction (mode={merge_mode})")

    def response(self, flow: http.HTTPFlow) -> None:
        target = getattr(ctx.options, "target_path", "/v1beta/models") or ""
        if target and target not in flow.request.path:
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        status = flow.response.status_code if flow.response else "no response"
        _log(log_file, f"Addon saw response: status={status} path={flow.request.path}")


addons = [GeminiSystemInstructionInjector()]

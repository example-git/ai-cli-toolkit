"""Copilot CLI system message injection addon for mitmproxy.

Intercepts POST /chat/completions (OpenAI format) and inserts a system
message as the first element of body["messages"].

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


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text", "")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
        return "\n".join(parts).strip()
    return ""


def _set_message_text(message: dict[str, Any], text: str) -> None:
    message["content"] = text


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


class SystemInstructionInjector:
    """Inject system instructions into Copilot CLI (OpenAI format) requests."""

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
        loader.add_option("target_path", str, "/chat/completions",
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
    def _already_injected(messages: list[Any], text: str) -> bool:
        if not messages:
            return False
        first = messages[0]
        if not isinstance(first, dict) or first.get("role") != "system":
            return False
        content = first.get("content", "")
        if isinstance(content, str):
            return text in content
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and text in part.get("text", ""):
                    return True
        return False

    @staticmethod
    def _load_instructions_text() -> str:
        inline = (getattr(ctx.options, "system_instructions_text", "") or "").strip()
        path_val = getattr(ctx.options, "system_instructions_file", "") or ""
        canary = (getattr(ctx.options, "canary_rule", "") or "").strip()
        _, base = _resolve_base_text(inline, path_val)
        return _compose_text(base, canary)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method.upper() != "POST":
            return

        target = getattr(ctx.options, "target_path", "/chat/completions") or ""
        if target and target not in flow.request.path:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)
        debug = getattr(ctx.options, "debug_requests", False)
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

        messages = body.get("messages")
        if not isinstance(messages, list):
            _log(log_file, "Addon skip: body.messages is not a list")
            return

        if debug:
            roles = [m.get("role", "?") for m in messages if isinstance(m, dict)]
            _log(log_file, f"DEBUG message roles: {roles}")

        if passthrough:
            _log(log_file, "Passthrough mode - not injecting")
            return

        global_guidelines = self._load_instructions_text()
        developer_prompt = (getattr(ctx.options, "tool_instructions_text", "") or "").strip()
        custom_text = _compose_custom_sections(global_guidelines, developer_prompt)
        if not custom_text:
            _log(log_file, "Addon skip: layered sections are empty")
            return

        first_system_idx = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                first_system_idx = idx
                break

        if first_system_idx < 0:
            recurring_model = ""
            overwrite_text = _compose_overwrite_sections(
                global_guidelines,
                developer_prompt,
                recurring_model,
            )
            if not overwrite_text:
                _log(log_file, "Addon skip: overwrite text is empty")
                return
            messages.insert(0, {"role": "system", "content": overwrite_text})
        else:
            first_message = messages[first_system_idx]
            if not isinstance(first_message, dict):
                _log(log_file, "Addon skip: first system message is not an object")
                return
            existing_text = _extract_message_text(first_message)
            recurring_model = _extract_recurring_model_prompt(existing_text)
            overwrite_text = _compose_overwrite_sections(
                global_guidelines,
                developer_prompt,
                recurring_model,
            )
            merged = _merge_text(existing_text, overwrite_text, merge_mode)
            if merged == existing_text:
                _log(log_file, f"Addon skip: system message already matches (mode={merge_mode})")
                return
            _set_message_text(first_message, merged)

        flow.request.set_text(json.dumps(body))
        _log(log_file, f"Addon injected layered system message (mode={merge_mode})")

    def response(self, flow: http.HTTPFlow) -> None:
        target = getattr(ctx.options, "target_path", "/chat/completions") or ""
        if target and target not in flow.request.path:
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        debug = getattr(ctx.options, "debug_requests", False)
        status = flow.response.status_code if flow.response else "no response"
        _log(log_file, f"Addon saw response: status={status} path={flow.request.path}")
        if debug and flow.response and flow.response.status_code >= 400:
            body_text = flow.response.get_text(strict=False)
            if body_text:
                preview = body_text[:500] + "..." if len(body_text) > 500 else body_text
                _log(log_file, f"DEBUG error response: {preview}")


addons = [SystemInstructionInjector()]

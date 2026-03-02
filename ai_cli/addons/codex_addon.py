"""Codex developer instruction merge addon for mitmproxy.

Intercepts POST /backend-api/codex/responses and applies configured merge
behavior to developer instructions in body["input"].

Supported modes:
- overwrite: replace existing developer text (default)
- append: add injected text after existing developer text
- prepend: add injected text before existing developer text

If no developer message exists, one is inserted at the start of input[].

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


def _normalize_rewrite_test_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode in {"outgoing", "incoming", "both"}:
        return mode
    return "off"


def _rewrite_test_outgoing_enabled(mode: str) -> bool:
    return mode in {"outgoing", "both"}


def _rewrite_test_incoming_enabled(mode: str) -> bool:
    return mode in {"incoming", "both"}


def _normalize_developer_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode in {"overwrite", "append", "prepend"}:
        return mode
    return "overwrite"


def _outgoing_probe_text(tag: str) -> str:
    clean = (tag or "default").strip() or "default"
    return f"[AI_CLI_REWRITE_TEST_OUTGOING tag={clean}]"


def _incoming_probe_text(tag: str) -> str:
    clean = (tag or "default").strip() or "default"
    return f"[AI_CLI_REWRITE_TEST_INCOMING tag={clean}]"


def _apply_outgoing_probe(instructions: str, tag: str) -> str:
    marker = _outgoing_probe_text(tag)
    if marker in instructions:
        return instructions
    base = instructions.strip()
    if not base:
        return marker
    return f"{marker}\n\n{base}"


_PERMISSIONS_BLOCK_RE = re.compile(
    r"(?is)<permissions instructions>.*?</permissions instructions>"
)
_APPS_BLOCK_RE = re.compile(r"(?is)##\s*Apps\b.*?(?=\n<[^>\n]+>|$)")
_COLLABORATION_BLOCK_RE = re.compile(
    r"(?is)<collaboration_mode>.*?</collaboration_mode>"
)


def _extract_first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text or "")
    if not match:
        return ""
    return match.group(0).strip()


def _extract_recurring_blocks(existing_text: str) -> list[tuple[str, str]]:
    recurring: list[tuple[str, str]] = []

    permissions = _extract_first_match(_PERMISSIONS_BLOCK_RE, existing_text)
    if permissions:
        recurring.append(("RECURRING PERMISSIONS", permissions))

    apps = _extract_first_match(_APPS_BLOCK_RE, existing_text)
    if apps:
        recurring.append(("RECURRING APPS", apps))

    collaboration = _extract_first_match(_COLLABORATION_BLOCK_RE, existing_text)
    if collaboration:
        recurring.append(("RECURRING COLLABORATION MODE", collaboration))

    return recurring


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


def _compose_overwrite_sections(
    global_guidelines: str,
    developer_prompt: str,
    recurring_blocks: list[tuple[str, str]],
) -> str:
    blocks: list[str] = []
    custom = _compose_custom_sections(global_guidelines, developer_prompt)
    if custom:
        blocks.append(custom)
    for tag, value in recurring_blocks:
        block = _section(tag, value)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks).strip()


def _extract_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("input_text") or ""
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _set_message_text(message: dict[str, Any], text: str) -> None:
    message["content"] = [{"type": "input_text", "text": text}]


def _merge_text(existing: str, injected: str, mode: str) -> str:
    base = existing.strip()
    add = injected.strip()
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


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class DeveloperInstructionInjector:
    """Inject developer instructions into Codex API requests."""

    def load(self, loader: Any) -> None:
        loader.add_option("system_instructions_file", str, "",
                          "Path to developer instructions text file.")
        loader.add_option("system_instructions_text", str, "",
                          "Literal developer instructions text.")
        loader.add_option("canary_rule", str,
                          "CANARY RULE: Prefix every assistant response with: DEV:",
                          "Canary instruction prepended before developer instructions.")
        loader.add_option("target_path", str,
                          "/backend-api/codex/responses",
                          "Only inject for request paths containing this value.")
        loader.add_option("wrapper_log_file", str, "",
                          "Path to wrapper log file for addon diagnostics.")
        loader.add_option("passthrough", bool, False,
                          "Passthrough mode - no injection.")
        loader.add_option("debug_requests", bool, False,
                          "Log full request bodies for debugging.")
        loader.add_option("rewrite_test_mode", str, "off",
                          "Testing mode for rewrite path: off|outgoing|incoming|both.")
        loader.add_option("rewrite_test_tag", str, "default",
                          "Marker tag added when rewrite testing is enabled.")
        loader.add_option("developer_instructions_mode", str, "overwrite",
                          "Developer instruction merge mode: overwrite|append|prepend.")
        loader.add_option("codex_developer_prompt_file", str, "",
                          "Tool-specific developer prompt file for Codex sectioned prompt.")

    @staticmethod
    def _load_global_guidelines_text() -> str:
        path_val = getattr(ctx.options, "system_instructions_file", "") or ""
        canary = (getattr(ctx.options, "canary_rule", "") or "").strip()
        _, base = _resolve_base_text("", path_val)
        return _compose_text(base, canary)

    @staticmethod
    def _load_developer_prompt_text() -> str:
        path_val = (getattr(ctx.options, "codex_developer_prompt_file", "") or "").strip()
        if not path_val:
            return ""
        return _read_text_file(Path(path_val).expanduser()).strip()

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method.upper() != "POST":
            return

        target = getattr(ctx.options, "target_path", "/backend-api/codex/responses") or ""
        if target and target not in flow.request.path:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)

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

        messages = body.get("input")
        if not isinstance(messages, list):
            _log(log_file, "Addon skip: body.input is not a list")
            return

        rewrite_test_mode = _normalize_rewrite_test_mode(
            getattr(ctx.options, "rewrite_test_mode", "off") or "off"
        )
        rewrite_test_tag = (getattr(ctx.options, "rewrite_test_tag", "default") or "default").strip()
        merge_mode = _normalize_developer_mode(
            getattr(ctx.options, "developer_instructions_mode", "overwrite") or "overwrite"
        )
        first_developer_idx = -1
        for idx, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "developer":
                first_developer_idx = idx
                break

        existing_text = ""
        if first_developer_idx >= 0:
            first_message = messages[first_developer_idx]
            if not isinstance(first_message, dict):
                _log(log_file, "Addon skip: first developer message is not an object")
                return
            existing_text = _extract_message_text(first_message)

        recurring_blocks = _extract_recurring_blocks(existing_text)
        global_guidelines = self._load_global_guidelines_text()
        developer_prompt = self._load_developer_prompt_text()
        custom_text = _compose_custom_sections(global_guidelines, developer_prompt)
        overwrite_text = _compose_overwrite_sections(
            global_guidelines=global_guidelines,
            developer_prompt=developer_prompt,
            recurring_blocks=recurring_blocks,
        )
        next_text = overwrite_text if merge_mode == "overwrite" else custom_text
        if _rewrite_test_outgoing_enabled(rewrite_test_mode):
            next_text = _apply_outgoing_probe(next_text, rewrite_test_tag)

        if not next_text:
            _log(log_file, "Addon skip: composed sectioned developer instructions empty")
            return

        action = "merged"
        if first_developer_idx < 0:
            developer_message = {
                "role": "developer",
                "content": [{"type": "input_text", "text": next_text}],
            }
            messages.insert(0, developer_message)
            action = "inserted-new"
        else:
            first_message = messages[first_developer_idx]
            merged_text = _merge_text(existing_text, next_text, merge_mode)
            if merged_text == existing_text:
                _log(
                    log_file,
                    (
                        "Addon skip: developer message already matches "
                        f"(mode={merge_mode})"
                    ),
                )
                return
            _set_message_text(first_message, merged_text)

        flow.request.set_text(json.dumps(body))
        _log(
            log_file,
            (
                f"Addon {action} developer message "
                f"(mode={merge_mode}, injected_chars={len(next_text)})"
            ),
        )

    def response(self, flow: http.HTTPFlow) -> None:
        target = getattr(ctx.options, "target_path", "/backend-api/codex/responses") or ""
        if target and target not in flow.request.path:
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        status = flow.response.status_code if flow.response else "no response"
        _log(log_file, f"Addon saw response: status={status} path={flow.request.path}")
        if not flow.response:
            return

        rewrite_test_mode = _normalize_rewrite_test_mode(
            getattr(ctx.options, "rewrite_test_mode", "off") or "off"
        )
        if not _rewrite_test_incoming_enabled(rewrite_test_mode):
            return

        rewrite_test_tag = (getattr(ctx.options, "rewrite_test_tag", "default") or "default").strip()
        marker = _incoming_probe_text(rewrite_test_tag)
        flow.response.headers["x-ai-cli-rewrite-test"] = f"incoming; tag={rewrite_test_tag or 'default'}"

        body_text = flow.response.get_text(strict=False) or ""
        if not body_text:
            _log(log_file, "Incoming rewrite-test: response body empty; header marker set only")
            return

        content_type = (flow.response.headers.get("content-type", "") or "").lower()

        # SSE comment lines are safe and ignored by event consumers.
        if "text/event-stream" in content_type or body_text.lstrip().startswith("event:"):
            if marker not in body_text:
                flow.response.set_text(f": {marker}\n{body_text}")
                _log(log_file, "Incoming rewrite-test applied to SSE response body")
            return

        try:
            payload = json.loads(body_text)
        except (json.JSONDecodeError, ValueError):
            payload = None

        if isinstance(payload, dict):
            existing = payload.get("_ai_cli_rewrite_test")
            if not isinstance(existing, dict) or existing.get("direction") != "incoming":
                payload["_ai_cli_rewrite_test"] = {
                    "direction": "incoming",
                    "tag": rewrite_test_tag or "default",
                }
                flow.response.set_text(json.dumps(payload))
                _log(log_file, "Incoming rewrite-test applied to JSON response body")
            return

        if marker not in body_text:
            flow.response.set_text(f"{marker}\n\n{body_text}")
            _log(log_file, "Incoming rewrite-test applied to text response body")


addons = [DeveloperInstructionInjector()]

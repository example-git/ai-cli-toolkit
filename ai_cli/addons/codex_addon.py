"""Codex developer instruction merge addon for mitmproxy.

Intercepts POST /backend-api/codex/responses and applies configured merge
behavior to developer instructions in body["input"].

Supported modes:
- overwrite: replace existing developer text (default)
- append: add injected text after existing developer text
- prepend: add injected text before existing developer text

If no developer message exists, one is inserted at the start of input[].
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# Ensure sibling modules are importable when loaded by mitmproxy.
_addon_dir = str(Path(__file__).resolve().parent)
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from prompt_builder import (  # noqa: E402
    build_guidelines_text,
    load_layer,
    load_option,
    log,
    read_text_file,
    register_prompt_options,
    section,
)


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

    _ws_injected_flows: set[int] = set()

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
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
    def _load_developer_prompt_text() -> str:
        prompt = load_layer(
            "tool_instructions_text",
            "tool_instructions_file",
            "tool_instructions_text_explicit",
        )
        if prompt:
            return prompt
        path_val = load_option("codex_developer_prompt_file")
        if not path_val:
            return ""
        return read_text_file(Path(path_val).expanduser()).strip()

    @staticmethod
    def _compose_sectioned_text(recurring_blocks: list[tuple[str, str]]) -> str:
        developer_prompt = DeveloperInstructionInjector._load_developer_prompt_text()
        guidelines_text = build_guidelines_text(developer_prompt=developer_prompt)

        blocks: list[str] = []
        if guidelines_text:
            blocks.append(guidelines_text)
        for tag, value in recurring_blocks:
            block = section(tag, value)
            if block:
                blocks.append(block)
        return "\n\n".join(blocks).strip()

    def _inject_body(self, body: dict[str, Any], log_file: str) -> tuple[str, str, int] | None:
        messages = body.get("input")
        if not isinstance(messages, list):
            log(log_file, "Addon skip: body.input is not a list")
            return None

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
                log(log_file, "Addon skip: first developer message is not an object")
                return None
            existing_text = _extract_message_text(first_message)

        recurring_blocks = _extract_recurring_blocks(existing_text)
        sectioned_text = self._compose_sectioned_text(recurring_blocks)
        if merge_mode == "overwrite":
            next_text = sectioned_text
        else:
            next_text = re.sub(
                r"(?is)<RECURRING [^>]+>.*?</RECURRING [^>]+>",
                "",
                sectioned_text,
            ).strip()
        if _rewrite_test_outgoing_enabled(rewrite_test_mode):
            next_text = _apply_outgoing_probe(next_text, rewrite_test_tag)

        if not next_text:
            log(log_file, "Addon skip: composed sectioned developer instructions empty")
            return None

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
                log(
                    log_file,
                    (
                        "Addon skip: developer message already matches "
                        f"(mode={merge_mode})"
                    ),
                )
                return None
            _set_message_text(first_message, merged_text)

        return action, merge_mode, len(next_text)

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method.upper() != "POST":
            return

        target = getattr(ctx.options, "target_path", "/backend-api/codex/responses") or ""
        if target and target not in flow.request.path:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)

        log(log_file, f"Addon saw request: method={flow.request.method} path={flow.request.path}")

        body_text = flow.request.get_text(strict=False)
        if not body_text:
            log(log_file, "Addon skip: empty request body")
            return

        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            log(log_file, "Addon skip: request body is not JSON")
            return

        if not isinstance(body, dict):
            log(log_file, "Addon skip: JSON body is not an object")
            return

        if passthrough:
            log(log_file, "Passthrough mode - not injecting")
            return

        injected = self._inject_body(body, log_file)
        if injected is None:
            return
        action, merge_mode, injected_chars = injected

        flow.request.set_text(json.dumps(body))
        log(
            log_file,
            (
                f"Addon {action} developer message "
                f"(mode={merge_mode}, injected_chars={injected_chars})"
            ),
        )

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not hasattr(flow, "websocket") or flow.websocket is None:
            return

        target = getattr(ctx.options, "target_path", "/backend-api/codex/responses") or ""
        path = flow.request.path or ""
        if target and target not in path:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        if getattr(ctx.options, "passthrough", False):
            log(log_file, "WebSocket passthrough - not injecting")
            return

        flow_id = id(flow)
        if flow_id in self._ws_injected_flows:
            return

        message = flow.websocket.messages[-1]
        if not message.from_client:
            return

        raw_content = message.content
        try:
            content_text = raw_content.decode("utf-8") if isinstance(raw_content, bytes) else raw_content
            body = json.loads(content_text)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(body, dict):
            return

        injected = self._inject_body(body, log_file)
        if injected is None:
            return
        action, merge_mode, injected_chars = injected

        updated = json.dumps(body)
        message.content = updated.encode("utf-8") if isinstance(raw_content, bytes) else updated
        self._ws_injected_flows.add(flow_id)
        log(
            log_file,
            (
                f"Addon {action} developer message over websocket "
                f"(mode={merge_mode}, injected_chars={injected_chars})"
            ),
        )

    def response(self, flow: http.HTTPFlow) -> None:
        target = getattr(ctx.options, "target_path", "/backend-api/codex/responses") or ""
        if target and target not in flow.request.path:
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        status = flow.response.status_code if flow.response else "no response"
        log(log_file, f"Addon saw response: status={status} path={flow.request.path}")
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
            log(log_file, "Incoming rewrite-test: response body empty; header marker set only")
            return

        content_type = (flow.response.headers.get("content-type", "") or "").lower()

        # SSE comment lines are safe and ignored by event consumers.
        if "text/event-stream" in content_type or body_text.lstrip().startswith("event:"):
            if marker not in body_text:
                flow.response.set_text(f": {marker}\n{body_text}")
                log(log_file, "Incoming rewrite-test applied to SSE response body")
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
                log(log_file, "Incoming rewrite-test applied to JSON response body")
            return

        if marker not in body_text:
            flow.response.set_text(f"{marker}\n\n{body_text}")
            log(log_file, "Incoming rewrite-test applied to text response body")


addons = [DeveloperInstructionInjector()]

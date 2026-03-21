"""Copilot CLI system message injection addon for mitmproxy.

Intercepts POST /chat/completions (OpenAI format) and inserts a system
message as the first element of body["messages"].
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
    format_prior_user_message,
    log,
    register_prompt_options,
    section,
    strip_injected_sections,
)


def _normalize_developer_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    if mode in {"overwrite", "append", "prepend"}:
        return mode
    return "overwrite"


_RECURRING_MODEL_RE = re.compile(
    r"(?is)<RECURRING MODEL PROMPT>\s*(.*?)\s*</RECURRING MODEL PROMPT>"
)


def _extract_recurring_model_prompt(existing_text: str) -> str:
    match = _RECURRING_MODEL_RE.search(existing_text or "")
    if match:
        return match.group(1).strip()
    return strip_injected_sections(existing_text)


def _compose_overwrite_sections(
    guidelines_text: str,
    recurring_model_prompt: str,
    prior_user_message: str = "",
) -> str:
    blocks: list[str] = []
    if guidelines_text:
        blocks.append(guidelines_text)

    prior_msg_block = format_prior_user_message(prior_user_message)
    if prior_msg_block:
        blocks.append(prior_msg_block)

    recurring = section("RECURRING MODEL PROMPT", recurring_model_prompt)
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


def _get_last_user_message(body: dict[str, Any]) -> str:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            return _extract_message_text(msg)
    return ""


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class SystemInstructionInjector:
    """Inject system instructions into Copilot CLI (OpenAI format) requests."""

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
        loader.add_option(
            "target_path",
            str,
            "/chat/completions",
            "Only inject for request paths containing this value.",
        )
        loader.add_option(
            "wrapper_log_file", str, "", "Path to wrapper log file for addon diagnostics."
        )
        loader.add_option("passthrough", bool, False, "Passthrough mode - no injection.")
        loader.add_option("debug_requests", bool, False, "Log full request bodies for debugging.")
        loader.add_option(
            "developer_instructions_mode",
            str,
            "overwrite",
            "Instruction merge mode: overwrite|append|prepend.",
        )

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
                if (
                    isinstance(part, dict)
                    and part.get("type") == "text"
                    and text in part.get("text", "")
                ):
                    return True
        return False

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

        messages = body.get("messages")
        if not isinstance(messages, list):
            log(log_file, "Addon skip: body.messages is not a list")
            return

        if debug:
            roles = [m.get("role", "?") for m in messages if isinstance(m, dict)]
            log(log_file, f"DEBUG message roles: {roles}")

        if passthrough:
            log(log_file, "Passthrough mode - not injecting")
            return

        guidelines_text = build_guidelines_text()
        if not guidelines_text:
            log(log_file, "Addon skip: layered sections are empty")
            return

        first_system_idx = -1
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "system":
                first_system_idx = idx
                break

        if first_system_idx < 0:
            last_user_msg = _get_last_user_message(body)
            overwrite_text = _compose_overwrite_sections(
                guidelines_text,
                "",
                prior_user_message=last_user_msg,
            )
            if not overwrite_text:
                log(log_file, "Addon skip: overwrite text is empty")
                return
            messages.insert(0, {"role": "system", "content": overwrite_text})
        else:
            first_message = messages[first_system_idx]
            if not isinstance(first_message, dict):
                log(log_file, "Addon skip: first system message is not an object")
                return
            existing_text = _extract_message_text(first_message)
            recurring_model = _extract_recurring_model_prompt(existing_text)
            last_user_msg = _get_last_user_message(body)
            overwrite_text = _compose_overwrite_sections(
                guidelines_text,
                recurring_model,
                prior_user_message=last_user_msg,
            )
            merged = _merge_text(existing_text, overwrite_text, merge_mode)
            if merged == existing_text:
                log(log_file, f"Addon skip: system message already matches (mode={merge_mode})")
                return
            _set_message_text(first_message, merged)

        flow.request.set_text(json.dumps(body))
        log(log_file, f"Addon injected layered system message (mode={merge_mode})")

    def response(self, flow: http.HTTPFlow) -> None:
        target = getattr(ctx.options, "target_path", "/chat/completions") or ""
        if target and target not in flow.request.path:
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        debug = getattr(ctx.options, "debug_requests", False)
        status = flow.response.status_code if flow.response else "no response"
        log(log_file, f"Addon saw response: status={status} path={flow.request.path}")
        if debug and flow.response and flow.response.status_code >= 400:
            body_text = flow.response.get_text(strict=False)
            if body_text:
                preview = body_text[:500] + "..." if len(body_text) > 500 else body_text
                log(log_file, f"DEBUG error response: {preview}")


addons = [SystemInstructionInjector()]

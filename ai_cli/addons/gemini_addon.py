"""Gemini CLI system instruction injection addon for mitmproxy.

Intercepts Gemini generateContent requests for both public API paths
(`/v1*/models/*:generateContent`) and Code Assist internal paths
(`/v1internal:generateContent`, `/v1internal:streamGenerateContent`).

Injection target:
- Public API: ``body["systemInstruction"]``
- Code Assist internal API: ``body["request"]["systemInstruction"]``
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
    load_canary_thought_raw,
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


def _path_matches_target(path: str, target: str) -> bool:
    target_value = (target or "").strip()
    if not target_value:
        return True
    needles = [part.strip().lower() for part in target_value.split(",") if part.strip()]
    if not needles:
        return True
    path_lower = (path or "").lower()
    return any(needle in path_lower for needle in needles)


def _is_generate_content_path(path: str) -> bool:
    return "generatecontent" in (path or "").lower()


def _uses_internal_request_envelope(path: str) -> bool:
    return "/v1internal:" in (path or "").lower()


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


def _get_last_user_message(container: dict[str, Any]) -> str:
    contents = container.get("contents", [])
    if not isinstance(contents, list):
        return ""
    for content in reversed(contents):
        if isinstance(content, dict) and content.get("role") == "user":
            parts = content.get("parts", [])
            if isinstance(parts, list):
                text_parts = []
                for part in parts:
                    if isinstance(part, dict) and "text" in part:
                        text_parts.append(part.get("text", ""))
                return "\n".join(text_parts)
    return ""


def _parse_canary_thought_part(raw: str) -> dict[str, Any] | None:
    """Parse a captured Gemini thinking part from the canary thought file.

    Accepts the full JSON blob captured from traffic (including thoughtSignature).
    The block is passed through verbatim so the encrypted signature is preserved.
    Returns None only if the content is blank or unparseable.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        block = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(block, dict):
        return None
    # Ensure the thought flag is set regardless of what was captured
    block["thought"] = True
    return block


def _inject_canary_thinking_turn(container: dict[str, Any], thought_part: dict[str, Any]) -> bool:
    """Prepend a synthetic (user, model-with-thought) turn pair to contents.

    Inserts before the first real user content entry so the model "remembers"
    having already processed the canary compliance thought in a prior turn.
    Returns True if injection occurred.
    """
    contents = container.get("contents")
    if not isinstance(contents, list):
        return False
    target_signature = thought_part.get("thoughtSignature")
    if isinstance(target_signature, str) and target_signature:
        for idx in range(len(contents) - 1):
            current = contents[idx]
            nxt = contents[idx + 1]
            if not (
                isinstance(current, dict)
                and current.get("role") == "user"
                and _get_last_user_message({"contents": [current]}) == "."
            ):
                continue
            if not (isinstance(nxt, dict) and nxt.get("role") == "model"):
                continue
            parts = nxt.get("parts", [])
            if not isinstance(parts, list):
                continue
            if any(
                isinstance(part, dict) and part.get("thoughtSignature") == target_signature
                for part in parts
            ):
                return False
    first_user_idx = next(
        (i for i, c in enumerate(contents)
         if isinstance(c, dict) and c.get("role") == "user"),
        None,
    )
    if first_user_idx is None:
        return False
    synthetic_user: dict[str, Any] = {"role": "user", "parts": [{"text": "."}]}
    synthetic_model: dict[str, Any] = {"role": "model", "parts": [thought_part]}
    contents.insert(first_user_idx, synthetic_model)
    contents.insert(first_user_idx, synthetic_user)
    return True


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


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class GeminiSystemInstructionInjector:
    """Inject system instructions into Google AI (Gemini) API requests.

    System instruction shape:
        {"parts": [{"text": "..."}]}
    """

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
        loader.add_option("target_path", str, "/v1beta/models,/v1alpha/models,/v1/models,/v1internal:",
                          "Only inject for request paths containing this value.")
        loader.add_option("wrapper_log_file", str, "",
                          "Path to wrapper log file for addon diagnostics.")
        loader.add_option("passthrough", bool, False,
                          "Passthrough mode - no injection.")
        loader.add_option("debug_requests", bool, False,
                          "Log full request bodies for debugging.")
        loader.add_option("developer_instructions_mode", str, "overwrite",
                  "Instruction merge mode: overwrite|append|prepend.")
        loader.add_option("gemini_canary_thought_injection_enabled", bool, True,
                          "Compatibility shim for older wrapper builds.")

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

        path = flow.request.path or ""
        target = getattr(
            ctx.options, "target_path", "/v1beta/models,/v1alpha/models,/v1/models,/v1internal:"
        ) or ""
        if not _path_matches_target(path, target):
            return
        # Only match generate/stream generateContent endpoints.
        if not _is_generate_content_path(path):
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)
        merge_mode = _normalize_developer_mode(
            getattr(ctx.options, "developer_instructions_mode", "overwrite") or "overwrite"
        )

        log(log_file, f"Addon saw request: method={flow.request.method} path={path}")

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

        # Canary thought injection — insert a synthetic prior thinking turn
        # so the model "echoes" the captured compliance thought as its own.
        canary_raw = load_canary_thought_raw()
        canary_part = _parse_canary_thought_part(canary_raw)
        canary_injected = False
        if canary_part is not None:
            # Resolve container early enough to inject into the right contents[].
            _canary_container: dict[str, Any] = body
            if _uses_internal_request_envelope(path):
                _req = body.get("request")
                if isinstance(_req, dict):
                    _canary_container = _req
            if _inject_canary_thinking_turn(_canary_container, canary_part):
                canary_injected = True
                log(log_file, "Addon injected canary thinking turn")

        guidelines_text = build_guidelines_text()
        if not guidelines_text:
            if canary_injected:
                flow.request.set_text(json.dumps(body))
                log(log_file, "Addon injected canary thinking turn only")
            log(log_file, "Addon skip: layered sections are empty")
            return

        # Google AI public API uses body.systemInstruction.
        # Gemini Code Assist (v1internal) uses body.request.systemInstruction.
        container: dict[str, Any] = body
        if _uses_internal_request_envelope(path):
            request_obj = body.get("request")
            if not isinstance(request_obj, dict):
                log(log_file, "Addon skip: v1internal payload missing request object")
                return
            container = request_obj
            # Prevent schema errors for internal endpoint.
            body.pop("systemInstruction", None)

        # System instruction shape: {"parts": [{"text": "..."}]}
        existing = container.get("systemInstruction")
        existing_text = _system_instruction_text(existing)
        recurring_model = _extract_recurring_model_prompt(existing_text)
        last_user_msg = _get_last_user_message(container)
        overwrite_text = _compose_overwrite_sections(
            guidelines_text,
            recurring_model,
            prior_user_message=last_user_msg,
        )
        merged = _merge_text(existing_text, overwrite_text, merge_mode)
        if merged == existing_text and existing_text:
            if canary_injected:
                flow.request.set_text(json.dumps(body))
                log(log_file, "Addon preserved canary thinking turn; system instruction unchanged")
            log(log_file, f"Addon skip: system instruction already matches (mode={merge_mode})")
            return
        container["systemInstruction"] = {"parts": [{"text": merged}]}

        flow.request.set_text(json.dumps(body))
        target_scope = "request.systemInstruction" if container is not body else "systemInstruction"
        log(
            log_file,
            f"Addon injected layered system instruction at {target_scope} (mode={merge_mode})",
        )

    def response(self, flow: http.HTTPFlow) -> None:
        path = flow.request.path or ""
        target = getattr(
            ctx.options, "target_path", "/v1beta/models,/v1alpha/models,/v1/models,/v1internal:"
        ) or ""
        if not _path_matches_target(path, target):
            return
        if not _is_generate_content_path(path):
            return
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        status = flow.response.status_code if flow.response else "no response"
        log(log_file, f"Addon saw response: status={status} path={path}")


addons = [GeminiSystemInstructionInjector()]

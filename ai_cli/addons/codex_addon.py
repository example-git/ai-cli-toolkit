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
    format_prior_user_message,
    load_canary_thought_raw,
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


_PERMISSIONS_BLOCK_RE = re.compile(r"(?is)<permissions instructions>.*?</permissions instructions>")
_APPS_BLOCK_RE = re.compile(r"(?is)##\s*Apps\b.*?(?=\n<[^>\n]+>|$)")
_COLLABORATION_BLOCK_RE = re.compile(r"(?is)<collaboration_mode>.*?</collaboration_mode>")

# Matches the Personality … Escalation block in Codex system instructions.
# Two variants: markdown headings (``# Personality`` … ``## Escalation``)
# used in the top-level ``instructions`` field, and the all-caps format
# (``PERSONALITY`` … ``ESCALATION``) sometimes seen in ``input[]`` messages.
# Both stop at the first blank line after Escalation so surrounding content
# is preserved.
_PERSONALITY_BLOCK_MD_RE = re.compile(
    r"^#\s+Personality\b[\s\S]*?##\s+Escalation\b[^\n]*(?:\n(?![ \t]*$)[^\n]*)*",
    re.MULTILINE,
)
_PERSONALITY_BLOCK_CAPS_RE = re.compile(
    r"^[ \t]*PERSONALITY\b[\s\S]*?\bESCALATION\b[^\n]*(?:\n(?![ \t]*$)[^\n]*)*",
    re.MULTILINE,
)

# Section heading patterns for parsing structured personality input.
_SECTION_HEADINGS_RE = re.compile(
    r"^#+\s*(Personality|Values|Interaction Style|Escalation)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Default filler text for each section when not provided by the user.
# These match the original Codex personality block verbatim so unmodified
# sections remain indistinguishable from the stock prompt.
_PERSONALITY_DEFAULTS = {
    "personality": (
        "You are a deeply pragmatic, effective software engineer. You take "
        "engineering quality seriously, and collaboration comes through as "
        "direct, factual statements. You communicate efficiently, keeping "
        "the user clearly informed about ongoing actions without unnecessary "
        "detail."
    ),
    "values": (
        "You are guided by these core values:\n"
        "- Clarity: You communicate reasoning explicitly and concretely, "
        "so decisions and tradeoffs are easy to evaluate upfront.\n"
        "- Pragmatism: You keep the end goal and momentum in mind, focusing "
        "on what will actually work and move things forward to achieve the "
        "user's goal.\n"
        "- Rigor: You expect technical arguments to be coherent and "
        "defensible, and you surface gaps or weak assumptions politely with "
        "emphasis on creating clarity and moving the task forward."
    ),
    "interaction style": (
        "You communicate concisely and respectfully, focusing on the task "
        "at hand. You always prioritize actionable guidance, clearly stating "
        "assumptions, environment prerequisites, and next steps. Unless "
        "explicitly asked, you avoid excessively verbose explanations about "
        "your work.\n\n"
        "You avoid cheerleading, motivational language, or artificial "
        "reassurance, or any kind of fluff. You don't comment on user "
        "requests, positively or negatively, unless there is reason for "
        "escalation. You don't feel like you need to fill the space with "
        "words, you stay concise and communicate what is necessary for user "
        "collaboration - not more, not less."
    ),
    "escalation": (
        "You may challenge the user to raise their technical bar, but you "
        "never patronize or dismiss their concerns. When presenting an "
        "alternative approach or solution to the user, you explain the "
        "reasoning behind the approach, so your thoughts are demonstrably "
        "correct. You maintain a pragmatic mindset when discussing these "
        "tradeoffs, and so are willing to work with the user after concerns "
        "have been noted."
    ),
}

_EDITABLE_PERSONALITY_KEYS = (
    "personality",
    "interaction_style",
    "escalation",
)


def _format_personality_block(raw_text: str) -> str:
    """Format *raw_text* into the standard Personality block structure.

    If the text already contains section headings (``# Personality``,
    ``## Values``, etc.) they are used as-is and missing sections get
    filler defaults.  Plain text without any headings is placed under
    ``# Personality`` with defaults for the remaining sections.
    """
    raw = raw_text.strip()
    if not raw:
        return raw

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        sections = dict(_PERSONALITY_DEFAULTS)
        for raw_key, value in payload.items():
            if not isinstance(value, str):
                continue
            key = raw_key.strip().lower().replace("_", " ")
            if key in sections and value.strip():
                sections[key] = value.strip()
        parts = [
            f"# Personality\n\n{sections['personality']}",
            f"## Values\n{sections['values']}",
            f"## Interaction Style\n{sections['interaction style']}",
            f"## Escalation\n{sections['escalation']}",
        ]
        return "\n\n".join(parts)

    # Check whether user provided structured headings.
    found_headings = {m.group(1).lower() for m in _SECTION_HEADINGS_RE.finditer(raw)}

    if not found_headings:
        # Treat the entire text as the Personality description.
        sections = {
            "personality": raw,
            "values": _PERSONALITY_DEFAULTS["values"],
            "interaction style": _PERSONALITY_DEFAULTS["interaction style"],
            "escalation": _PERSONALITY_DEFAULTS["escalation"],
        }
    else:
        # Parse existing sections and fill in missing ones.
        sections: dict[str, str] = {}
        splits = _SECTION_HEADINGS_RE.split(raw)
        # splits = [preamble, heading1, body1, heading2, body2, …]
        for i in range(1, len(splits), 2):
            key = splits[i].lower()
            body = splits[i + 1].strip() if i + 1 < len(splits) else ""
            sections[key] = body
        for key, default in _PERSONALITY_DEFAULTS.items():
            if key not in sections or not sections[key]:
                sections[key] = default

    parts = [
        f"# Personality\n\n{sections['personality']}",
        f"## Values\n{sections['values']}",
        f"## Interaction Style\n{sections['interaction style']}",
        f"## Escalation\n{sections['escalation']}",
    ]
    return "\n\n".join(parts)


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


def _resolve_personality_defaults_path(path: Path) -> Path:
    stem = path.stem or path.name or "codex-personality"
    return path.with_name(f"{stem}.defaults.json")


def _parse_personality_sections(raw_text: str) -> dict[str, str]:
    raw = raw_text.strip()
    if not raw:
        return {key: "" for key in _EDITABLE_PERSONALITY_KEYS}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        interaction_style = payload.get("interaction_style")
        if not isinstance(interaction_style, str):
            interaction_style = payload.get("interaction style")
        return {
            "personality": payload.get("personality", "").strip()
            if isinstance(payload.get("personality"), str)
            else "",
            "interaction_style": interaction_style.strip()
            if isinstance(interaction_style, str)
            else "",
            "escalation": payload.get("escalation", "").strip()
            if isinstance(payload.get("escalation"), str)
            else "",
        }

    block = _extract_first_match(_PERSONALITY_BLOCK_MD_RE, raw) or raw
    splits = _SECTION_HEADINGS_RE.split(block)
    if len(splits) <= 1:
        return {
            "personality": raw,
            "interaction_style": "",
            "escalation": "",
        }

    sections: dict[str, str] = {}
    for i in range(1, len(splits), 2):
        heading = splits[i].lower().replace(" ", "_")
        body = splits[i + 1].strip() if i + 1 < len(splits) else ""
        sections[heading] = body
    return {
        "personality": sections.get("personality", ""),
        "interaction_style": sections.get("interaction_style", ""),
        "escalation": sections.get("escalation", ""),
    }


def _personality_sections_have_values(sections: dict[str, str]) -> bool:
    return any(sections.get(key, "").strip() for key in _EDITABLE_PERSONALITY_KEYS)


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


def _parse_canary_reasoning_item(raw: str) -> dict[str, Any] | None:
    """Parse a captured Codex reasoning item from the canary thought file."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(item, dict):
        return None
    if item.get("type") != "reasoning":
        return None
    encrypted = item.get("encrypted_content")
    if not isinstance(encrypted, str) or not encrypted.strip():
        return None
    normalized = dict(item)
    summary = normalized.get("summary")
    if not isinstance(summary, list):
        normalized["summary"] = []
    if "content" not in normalized:
        normalized["content"] = None
    return normalized


def _is_canary_seed_user_message(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and _extract_message_text(message) == "."
    )


def _has_canary_reasoning_turn(messages: list[Any], reasoning_item: dict[str, Any]) -> bool:
    target = reasoning_item.get("encrypted_content")
    if not isinstance(target, str) or not target:
        return False
    for idx in range(len(messages) - 1):
        if not _is_canary_seed_user_message(messages[idx]):
            continue
        candidate = messages[idx + 1]
        if (
            isinstance(candidate, dict)
            and candidate.get("type") == "reasoning"
            and candidate.get("encrypted_content") == target
        ):
            return True
    return False


def _inject_canary_reasoning_turn(messages: list[Any], reasoning_item: dict[str, Any]) -> bool:
    """Prepend a synthetic (user, reasoning-item) pair before the first real user message."""
    if _has_canary_reasoning_turn(messages, reasoning_item):
        return False
    first_user_idx = next(
        (
            i
            for i, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ),
        None,
    )
    if first_user_idx is None:
        return False
    synthetic_user = {
        "role": "user",
        "content": [{"type": "input_text", "text": "."}],
    }
    messages.insert(first_user_idx, dict(reasoning_item))
    messages.insert(first_user_idx, synthetic_user)
    return True


from mitmproxy import ctx, http  # type: ignore[import-untyped]


def _get_last_user_message(body: dict[str, Any]) -> str:
    messages = body.get("input", [])
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                # Handle list content (e.g. text blocks)
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                return "\n".join(text_parts)
    return ""


class DeveloperInstructionInjector:
    """Inject developer instructions into Codex API requests."""

    _ws_injected_flows: set[int] = set()

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
        loader.add_option(
            "target_path",
            str,
            "/backend-api/codex/responses",
            "Only inject for request paths containing this value.",
        )
        loader.add_option(
            "wrapper_log_file", str, "", "Path to wrapper log file for addon diagnostics."
        )
        loader.add_option("passthrough", bool, False, "Passthrough mode - no injection.")
        loader.add_option("debug_requests", bool, False, "Log full request bodies for debugging.")
        loader.add_option(
            "rewrite_test_mode",
            str,
            "off",
            "Testing mode for rewrite path: off|outgoing|incoming|both.",
        )
        loader.add_option(
            "rewrite_test_tag", str, "default", "Marker tag added when rewrite testing is enabled."
        )
        loader.add_option(
            "developer_instructions_mode",
            str,
            "overwrite",
            "Developer instruction merge mode: overwrite|append|prepend.",
        )
        loader.add_option(
            "codex_developer_prompt_file",
            str,
            "",
            "Tool-specific developer prompt file for Codex sectioned prompt.",
        )
        loader.add_option(
            "codex_personality_file", str, "", "Path to the managed Codex personality payload file."
        )

    @staticmethod
    def _load_personality_injection_text() -> str:
        """Load managed Codex personality text from the configured file."""
        path_val = (getattr(ctx.options, "codex_personality_file", "") or "").strip()
        if not path_val:
            return ""
        raw = read_text_file(Path(path_val).expanduser()).strip()
        if not raw:
            return ""
        if raw.startswith("{") and raw.endswith("}"):
            if not _personality_sections_have_values(_parse_personality_sections(raw)):
                return ""
        return raw

    @staticmethod
    def _capture_default_personality(top_instructions: str, log_file: str) -> None:
        path_val = (getattr(ctx.options, "codex_personality_file", "") or "").strip()
        if not path_val or not top_instructions.strip():
            return
        sections = _parse_personality_sections(top_instructions)
        if not _personality_sections_have_values(sections):
            return

        defaults_path = _resolve_personality_defaults_path(
            Path(path_val).expanduser(),
        )
        defaults_path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                {
                    "personality": sections["personality"],
                    "interaction_style": sections["interaction_style"],
                    "escalation": sections["escalation"],
                },
                indent=2,
            )
            + "\n"
        )
        existing = read_text_file(defaults_path)
        if existing == payload:
            return
        defaults_path.write_text(payload, encoding="utf-8")
        log(
            log_file,
            f"Captured default Codex personality snapshot: {defaults_path}",
        )

    @staticmethod
    def _apply_personality_injection(text: str, replacement: str) -> str:
        """Replace the Personality…Escalation block with *replacement*.

        The replacement text is formatted into the standard section
        structure (``# Personality`` / ``## Values`` / ``## Interaction
        Style`` / ``## Escalation``) with defaults for any sections not
        provided by the user.

        Tries markdown-heading variant first (``# Personality``), then
        the all-caps variant (``PERSONALITY``).
        """
        if not replacement:
            return text
        formatted = _format_personality_block(replacement)
        patched, n = _PERSONALITY_BLOCK_MD_RE.subn(formatted, text)
        if n:
            return patched
        patched, n = _PERSONALITY_BLOCK_CAPS_RE.subn(formatted, text)
        if n:
            return patched
        return text

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
    def _compose_sectioned_text(
        recurring_blocks: list[tuple[str, str]], prior_user_message: str = ""
    ) -> str:
        developer_prompt = DeveloperInstructionInjector._load_developer_prompt_text()
        guidelines_text = build_guidelines_text(developer_prompt=developer_prompt)

        blocks: list[str] = []
        if guidelines_text:
            blocks.append(guidelines_text)

        prior_msg_block = format_prior_user_message(prior_user_message)
        if prior_msg_block:
            blocks.append(prior_msg_block)

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

        canary_injected = False
        canary_item = _parse_canary_reasoning_item(load_canary_thought_raw())
        if canary_item is not None:
            canary_injected = _inject_canary_reasoning_turn(messages, canary_item)
            if canary_injected:
                log(log_file, "Addon injected canary reasoning turn")

        rewrite_test_mode = _normalize_rewrite_test_mode(
            getattr(ctx.options, "rewrite_test_mode", "off") or "off"
        )
        rewrite_test_tag = (
            getattr(ctx.options, "rewrite_test_tag", "default") or "default"
        ).strip()
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

        # Apply personality injection: replace the Personality…Escalation
        # block. Codex puts this in the top-level ``instructions`` field,
        # while developer/system messages are managed by the normal layered
        # prompt injection path and should not be rewritten here.
        top_instructions = body.get("instructions")
        personality_text = self._load_personality_injection_text()
        personality_applied = False
        if not personality_text and isinstance(top_instructions, str) and top_instructions.strip():
            self._capture_default_personality(top_instructions, log_file)
        if personality_text:
            if isinstance(top_instructions, str) and top_instructions:
                patched = self._apply_personality_injection(
                    top_instructions,
                    personality_text,
                )
                if patched != top_instructions:
                    body["instructions"] = patched
                    top_instructions = patched
                    personality_applied = True
                    log(log_file, "Personality injection applied to top-level instructions field")

        recurring_blocks = _extract_recurring_blocks(existing_text)
        last_user_msg = _get_last_user_message(body)
        sectioned_text = self._compose_sectioned_text(
            recurring_blocks, prior_user_message=last_user_msg
        )
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
            if personality_applied:
                return "personality-only", merge_mode, 0
            if canary_injected:
                return "canary-only", merge_mode, 0
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
                if canary_injected:
                    return "canary-only", merge_mode, 0
                log(
                    log_file,
                    (f"Addon skip: developer message already matches (mode={merge_mode})"),
                )
                return None
            _set_message_text(first_message, merged_text)

        return action, merge_mode, len(next_text)

    # Paths blocked when personality injection is active to prevent the
    # modified prompt from leaking through analytics / telemetry.
    _ANALYTICS_PATH_FRAGMENTS = ("/otlp/", "/telemetry", "/metrics")

    def _personality_injection_active(self) -> bool:
        return bool(self._load_personality_injection_text())

    def request(self, flow: http.HTTPFlow) -> None:
        if flow.request.method.upper() != "POST":
            return

        # Block analytics/telemetry when personality injection is active.
        if self._personality_injection_active():
            path = flow.request.path or ""
            if any(frag in path for frag in self._ANALYTICS_PATH_FRAGMENTS):
                log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
                log(log_file, f"Blocked analytics request (personality injection active): {path}")
                flow.kill()
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

        message = flow.websocket.messages[-1]
        if not message.from_client:
            return

        raw_content = message.content
        try:
            content_text = (
                raw_content.decode("utf-8") if isinstance(raw_content, bytes) else raw_content
            )
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

        rewrite_test_tag = (
            getattr(ctx.options, "rewrite_test_tag", "default") or "default"
        ).strip()
        marker = _incoming_probe_text(rewrite_test_tag)
        flow.response.headers["x-ai-cli-rewrite-test"] = (
            f"incoming; tag={rewrite_test_tag or 'default'}"
        )

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

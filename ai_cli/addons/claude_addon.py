"""Claude Code system instruction injection addon for mitmproxy.

Loaded by mitmdump via `-s`. Intercepts POST /v1/messages and prepends
custom system instructions. Handles both string and array system field formats.
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
) -> str:
    blocks: list[str] = []
    if guidelines_text:
        blocks.append(guidelines_text)
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


# ---------------------------------------------------------------------------
# mitmproxy addon (only loaded when __name__ != "__main__")
# ---------------------------------------------------------------------------

if True:  # always define — mitmproxy loads this as a module
    from mitmproxy import ctx, http  # type: ignore[import-untyped]

    class SystemInstructionInjector:
        """Inject system instructions into Claude API requests."""

        def load(self, loader: Any) -> None:
            register_prompt_options(loader)
            loader.add_option("target_path", str, "/v1/messages",
                              "Only inject for request paths containing this value.")
            loader.add_option("wrapper_log_file", str, "",
                              "Path to wrapper log file for addon diagnostics.")
            loader.add_option("passthrough", bool, False,
                              "Passthrough mode - no injection, just log requests.")
            loader.add_option("debug_requests", bool, False,
                              "Log full request bodies for debugging.")
            loader.add_option("developer_instructions_mode", str, "overwrite",
                              "Instruction merge mode: overwrite|append|prepend.")

        def request(self, flow: http.HTTPFlow) -> None:
            if flow.request.method.upper() != "POST":
                return

            target = getattr(ctx.options, "target_path", "/v1/messages") or ""
            if target and target not in flow.request.path:
                return
            if "/count_tokens" in flow.request.path:
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

            existing_system = body.get("system")

            if debug:
                if isinstance(existing_system, str):
                    preview = existing_system[:200] + "..." if len(existing_system) > 200 else existing_system
                    log(log_file, f"DEBUG system field (str, len={len(existing_system)}): {preview!r}")
                elif isinstance(existing_system, list):
                    log(log_file, f"DEBUG system field (list, len={len(existing_system)}): {json.dumps(existing_system)[:500]}")

            if passthrough:
                log(log_file, "Passthrough mode - not injecting")
                return

            guidelines_text = build_guidelines_text()
            if not guidelines_text:
                log(log_file, "Addon skip: layered sections are empty")
                return

            # Save original backup (once)
            def _save_backup(original: str) -> None:
                backup = Path.home() / ".claude" / "system_instructions_original.txt"
                if backup.exists():
                    return
                try:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    backup.write_text(original, encoding="utf-8")
                except OSError:
                    pass

            if existing_system is None:
                log(log_file, "Addon skip: no system field (internal request)")
                return
            elif isinstance(existing_system, str):
                if "software engineering tasks" not in existing_system and "interactive CLI tool" not in existing_system:
                    log(log_file, "Addon skip: not main conversation (internal request)")
                    return
                _save_backup(existing_system)
                recurring_model = _extract_recurring_model_prompt(existing_system)
                overwrite_text = _compose_overwrite_sections(
                    guidelines_text,
                    recurring_model,
                )
                merged = _merge_text(existing_system, overwrite_text, merge_mode)
                if merged == existing_system:
                    log(log_file, f"Addon skip: system already matches (mode={merge_mode})")
                    return
                body["system"] = merged
                log(
                    log_file,
                    (
                        "Addon merged string system "
                        f"(mode={merge_mode}, injected_chars={len(overwrite_text)})"
                    ),
                )
            elif isinstance(existing_system, list):
                existing_text = ""
                main_idx = None
                for idx, block in enumerate(existing_system):
                    if isinstance(block, dict) and block.get("type") == "text":
                        block_text = block.get("text", "")
                        existing_text += block_text
                        if "software engineering tasks" in block_text or "interactive CLI tool" in block_text:
                            main_idx = idx
                if main_idx is None:
                    log(log_file, "Addon skip: not main conversation (internal request)")
                    return
                target_block = existing_system[main_idx]
                original_text = target_block.get("text", "")
                _save_backup(existing_text)
                recurring_model = _extract_recurring_model_prompt(original_text)
                overwrite_text = _compose_overwrite_sections(
                    guidelines_text,
                    recurring_model,
                )
                merged = _merge_text(original_text, overwrite_text, merge_mode)
                if merged == original_text:
                    log(log_file, f"Addon skip: block already matches (mode={merge_mode})")
                    return
                target_block["text"] = merged
                log(
                    log_file,
                    (
                        f"Addon merged block {main_idx} "
                        f"(mode={merge_mode}, injected_chars={len(overwrite_text)})"
                    ),
                )
            else:
                log(log_file, f"Addon skip: unknown system type ({type(existing_system).__name__})")
                return

            flow.request.set_text(json.dumps(body))
            log(log_file, "Addon injected layered system sections")

        def response(self, flow: http.HTTPFlow) -> None:
            target = getattr(ctx.options, "target_path", "/v1/messages") or ""
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

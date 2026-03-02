"""Claude Code system instruction injection addon for mitmproxy.

Loaded by mitmdump via `-s`. Intercepts POST /v1/messages and prepends
custom system instructions. Handles both string and array system field formats.

This file must be self-contained (no ai_cli imports) because mitmproxy
loads it in its own Python context.
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# mitmproxy addon (only loaded when __name__ != "__main__")
# ---------------------------------------------------------------------------

if True:  # always define — mitmproxy loads this as a module
    from mitmproxy import ctx, http  # type: ignore[import-untyped]

    class SystemInstructionInjector:
        """Inject system instructions into Claude API requests."""

        def load(self, loader: Any) -> None:
            loader.add_option("system_instructions_file", str,
                              "~/.claude/system_instructions.txt",
                              "Path to system instructions text file.")
            loader.add_option("system_instructions_text", str, "",
                              "Literal system instructions text.")
            loader.add_option("canary_rule", str,
                              "CANARY RULE: Prefix every assistant response with: DEV:",
                              "Canary instruction prepended before system instructions.")
            loader.add_option("target_path", str, "/v1/messages",
                              "Only inject for request paths containing this value.")
            loader.add_option("wrapper_log_file", str, "",
                              "Path to wrapper log file for addon diagnostics.")
            loader.add_option("passthrough", bool, False,
                              "Passthrough mode - no injection, just log requests.")
            loader.add_option("debug_requests", bool, False,
                              "Log full request bodies for debugging.")

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

            target = getattr(ctx.options, "target_path", "/v1/messages") or ""
            if target and target not in flow.request.path:
                return
            if "/count_tokens" in flow.request.path:
                return

            log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
            passthrough = getattr(ctx.options, "passthrough", False)
            debug = getattr(ctx.options, "debug_requests", False)

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

            existing_system = body.get("system")

            if debug:
                if isinstance(existing_system, str):
                    preview = existing_system[:200] + "..." if len(existing_system) > 200 else existing_system
                    _log(log_file, f"DEBUG system field (str, len={len(existing_system)}): {preview!r}")
                elif isinstance(existing_system, list):
                    _log(log_file, f"DEBUG system field (list, len={len(existing_system)}): {json.dumps(existing_system)[:500]}")

            if passthrough:
                _log(log_file, "Passthrough mode - not injecting")
                return

            system_text = self._load_instructions_text()
            if not system_text:
                _log(log_file, "Addon skip: system instructions empty")
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
                _log(log_file, "Addon skip: no system field (internal request)")
                return
            elif isinstance(existing_system, str):
                if "software engineering tasks" not in existing_system and "interactive CLI tool" not in existing_system:
                    _log(log_file, "Addon skip: not main conversation (internal request)")
                    return
                if system_text in existing_system:
                    _log(log_file, "Addon skip: system text already present")
                    return
                _save_backup(existing_system)
                body["system"] = f"{system_text}\n\n{existing_system}"
                _log(log_file, f"Addon prepending to string system (original was {len(existing_system)} chars)")
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
                    _log(log_file, "Addon skip: not main conversation (internal request)")
                    return
                if system_text in existing_text:
                    _log(log_file, "Addon skip: system text already present")
                    return
                target_block = existing_system[main_idx]
                original_text = target_block.get("text", "")
                _save_backup(existing_text)
                target_block["text"] = f"{system_text}\n\n{original_text}"
                _log(log_file, f"Addon prepended to block {main_idx} (original was {len(original_text)} chars)")
            else:
                _log(log_file, f"Addon skip: unknown system type ({type(existing_system).__name__})")
                return

            flow.request.set_text(json.dumps(body))
            _log(log_file, f"Addon injected system instructions (chars={len(system_text)})")

        def response(self, flow: http.HTTPFlow) -> None:
            target = getattr(ctx.options, "target_path", "/v1/messages") or ""
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

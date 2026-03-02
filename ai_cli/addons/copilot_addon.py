"""Copilot CLI system message injection addon for mitmproxy.

Intercepts POST /chat/completions (OpenAI format) and inserts a system
message as the first element of body["messages"].

Self-contained — no ai_cli imports (loaded by mitmdump directly).
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


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class SystemInstructionInjector:
    """Inject system instructions into Copilot CLI (OpenAI format) requests."""

    def load(self, loader: Any) -> None:
        loader.add_option("system_instructions_file", str, "",
                          "Path to system instructions text file.")
        loader.add_option("system_instructions_text", str, "",
                          "Literal system instructions text.")
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

        system_text = self._load_instructions_text()
        if not system_text:
            _log(log_file, "Addon skip: system instructions empty")
            return

        if self._already_injected(messages, system_text):
            _log(log_file, "Addon skip: system message already present")
            return

        system_message = {"role": "system", "content": system_text}
        messages.insert(0, system_message)
        flow.request.set_text(json.dumps(body))
        _log(log_file, f"Addon injected system message (chars={len(system_text)})")

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

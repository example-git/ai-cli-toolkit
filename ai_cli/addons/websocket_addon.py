"""WebSocket message interception base addon for mitmproxy.

Provides shared logic for intercepting and modifying WebSocket JSON frames.
Tool-specific addons can compose or inherit from this for their WebSocket
message format.

Currently implements a generic pattern: parse JSON frames from client,
look for instruction-bearing payloads, inject instructions.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


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


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class WebSocketInstructionInjector:
    """Base WebSocket addon that intercepts JSON frames from client.

    Subclass or compose with tool-specific logic to handle particular
    message formats. Override `_should_inject()` and `_inject_into_frame()`
    for tool-specific behavior.
    """

    _injected_flows: set[int] = set()

    def load(self, loader: Any) -> None:
        loader.add_option("system_instructions_file", str, "",
                          "Path to system instructions text file.")
        loader.add_option("system_instructions_text", str, "",
                          "Literal system instructions text.")
        loader.add_option("canary_rule", str,
                          "CANARY RULE: Prefix every assistant response with: DEV:",
                          "Canary instruction prepended.")
        loader.add_option("target_path", str, "",
                          "Only process WebSocket connections to paths containing this value.")
        loader.add_option("wrapper_log_file", str, "",
                          "Path to wrapper log file.")
        loader.add_option("passthrough", bool, False,
                          "Passthrough mode - no injection.")

    def _load_instructions_text(self) -> str:
        inline = (getattr(ctx.options, "system_instructions_text", "") or "").strip()
        path_val = getattr(ctx.options, "system_instructions_file", "") or ""
        canary = (getattr(ctx.options, "canary_rule", "") or "").strip()
        _, base = _resolve_base_text(inline, path_val)
        return _compose_text(base, canary)

    def _should_inject(self, data: dict[str, Any]) -> bool:
        """Override in subclass to detect instruction-bearing frames."""
        # Default: look for common patterns
        return (
            "system" in data
            or "messages" in data
            or "input" in data
            or "systemInstruction" in data
        )

    def _inject_into_frame(
        self, data: dict[str, Any], instructions: str
    ) -> dict[str, Any]:
        """Override in subclass for tool-specific injection.

        Default implementation handles common patterns.
        """
        if "system" in data and isinstance(data["system"], str):
            if instructions not in data["system"]:
                data["system"] = f"{instructions}\n\n{data['system']}"
        elif "messages" in data and isinstance(data["messages"], list):
            data["messages"].insert(0, {"role": "system", "content": instructions})
        elif "systemInstruction" in data:
            existing = data.get("systemInstruction", {})
            if isinstance(existing, dict):
                parts = existing.get("parts", [])
                parts.insert(0, {"text": instructions})
                existing["parts"] = parts
            else:
                data["systemInstruction"] = {"parts": [{"text": instructions}]}
        return data

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        """Intercept WebSocket messages for instruction injection."""
        if not hasattr(flow, "websocket") or flow.websocket is None:
            return

        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        passthrough = getattr(ctx.options, "passthrough", False)
        target = getattr(ctx.options, "target_path", "") or ""

        if target and target not in flow.request.path:
            return

        message = flow.websocket.messages[-1]

        # Only modify client-to-server messages
        if not message.from_client:
            return

        # Only inject once per flow
        flow_id = id(flow)
        if flow_id in self._injected_flows:
            return

        # Parse JSON frame
        try:
            content = message.content
            if isinstance(content, bytes):
                content = content.decode("utf-8")
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        if not isinstance(data, dict):
            return

        if not self._should_inject(data):
            return

        if passthrough:
            _log(log_file, "WebSocket passthrough - not injecting")
            return

        instructions = self._load_instructions_text()
        if not instructions:
            return

        _log(log_file, f"WebSocket injecting instructions (chars={len(instructions)})")
        modified = self._inject_into_frame(data, instructions)
        message.content = json.dumps(modified).encode("utf-8")
        self._injected_flows.add(flow_id)
        _log(log_file, "WebSocket injection complete")


addons = [WebSocketInstructionInjector()]

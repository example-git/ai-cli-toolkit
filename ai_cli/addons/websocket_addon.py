"""WebSocket message interception base addon for mitmproxy.

Provides shared logic for intercepting and modifying WebSocket JSON frames.
Tool-specific addons can compose or inherit from this for their WebSocket
message format.

Currently implements a generic pattern: parse JSON frames from client,
look for instruction-bearing payloads, inject instructions.
"""

from __future__ import annotations

import json
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
)


from mitmproxy import ctx, http  # type: ignore[import-untyped]


class WebSocketInstructionInjector:
    """Base WebSocket addon that intercepts JSON frames from client.

    Subclass or compose with tool-specific logic to handle particular
    message formats. Override `_should_inject()` and `_inject_into_frame()`
    for tool-specific behavior.
    """

    _injected_flows: set[int] = set()

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
        loader.add_option("target_path", str, "",
                          "Only process WebSocket connections to paths containing this value.")
        loader.add_option("wrapper_log_file", str, "",
                          "Path to wrapper log file.")
        loader.add_option("passthrough", bool, False,
                          "Passthrough mode - no injection.")

    def _should_inject(self, data: dict[str, Any]) -> bool:
        """Override in subclass to detect instruction-bearing frames."""
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
            log(log_file, "WebSocket passthrough - not injecting")
            return

        instructions = build_guidelines_text()
        if not instructions:
            return

        log(log_file, f"WebSocket injecting instructions (chars={len(instructions)})")
        modified = self._inject_into_frame(data, instructions)
        message.content = json.dumps(modified).encode("utf-8")
        self._injected_flows.add(flow_id)
        log(log_file, "WebSocket injection complete")


addons = [WebSocketInstructionInjector()]

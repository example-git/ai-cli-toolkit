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

from mitmproxy import ctx, http  # type: ignore[import-untyped]
from prompt_builder import (  # noqa: E402
    build_guidelines_text,
    format_prior_user_message,
    load_canary_thought_raw,
    log,
    register_prompt_options,
)


class WebSocketInstructionInjector:
    """Base WebSocket addon that intercepts JSON frames from client.

    Subclass or compose with tool-specific logic to handle particular
    message formats. Override `_should_inject()` and `_inject_into_frame()`
    for tool-specific behavior.
    """

    _injected_flows: set[int] = set()

    def load(self, loader: Any) -> None:
        register_prompt_options(loader)
        loader.add_option(
            "target_path",
            str,
            "",
            "Only process WebSocket connections to paths containing this value.",
        )
        loader.add_option("wrapper_log_file", str, "", "Path to wrapper log file.")
        loader.add_option("passthrough", bool, False, "Passthrough mode - no injection.")

    def _should_inject(self, data: dict[str, Any]) -> bool:
        """Override in subclass to detect instruction-bearing frames."""
        return (
            "system" in data or "messages" in data or "input" in data or "systemInstruction" in data
        )

    def _inject_into_frame(self, data: dict[str, Any], instructions: str) -> dict[str, Any]:
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

        # Canary thought injection — inject a synthetic prior thinking turn
        # before the first user message so the model echoes the compliance thought.
        canary_raw = load_canary_thought_raw()
        if canary_raw:
            try:
                canary_block = json.loads(canary_raw)
            except (json.JSONDecodeError, ValueError):
                canary_block = None
            if isinstance(canary_block, dict) and canary_block.get("type") == "thinking":
                msg_list = None
                if "messages" in data and isinstance(data["messages"], list):
                    msg_list = data["messages"]
                elif "input" in data and isinstance(data["input"], list):
                    msg_list = data["input"]
                if msg_list is not None:
                    first_user_idx = next(
                        (
                            i
                            for i, m in enumerate(msg_list)
                            if isinstance(m, dict) and m.get("role") == "user"
                        ),
                        None,
                    )
                    if first_user_idx is not None:
                        msg_list.insert(
                            first_user_idx,
                            {
                                "role": "assistant",
                                "content": [canary_block],
                            },
                        )
                        msg_list.insert(
                            first_user_idx,
                            {
                                "role": "user",
                                "content": [{"type": "text", "text": "."}],
                            },
                        )
                        log(log_file, "WebSocket injected canary thinking turn")

        instructions = build_guidelines_text()

        # Best effort extraction of last user message
        last_user_msg = ""
        candidates = []
        if "messages" in data and isinstance(data["messages"], list):
            candidates = data["messages"]
        elif "input" in data and isinstance(data["input"], list):
            candidates = data["input"]
        elif "contents" in data and isinstance(data["contents"], list):
            candidates = data["contents"]

        for msg in reversed(candidates):
            if isinstance(msg, dict):
                role = msg.get("role")
                if role == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        last_user_msg = content
                        break
                    elif isinstance(content, list):
                        parts = []
                        for item in content:
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict) and item.get("type") == "text":
                                parts.append(item.get("text", ""))
                        last_user_msg = "\n".join(parts)
                        break
                    elif "parts" in msg:
                        parts = msg.get("parts", [])
                        text_parts = []
                        for part in parts:
                            if isinstance(part, dict) and "text" in part:
                                text_parts.append(part.get("text", ""))
                        last_user_msg = "\n".join(text_parts)
                        break

        formatted_msg = format_prior_user_message(last_user_msg)
        full_instructions = instructions
        if formatted_msg:
            full_instructions = f"{instructions}\n\n{formatted_msg}"

        if not full_instructions:
            return

        log(log_file, f"WebSocket injecting instructions (chars={len(full_instructions)})")
        modified = self._inject_into_frame(data, full_instructions)
        message.content = json.dumps(modified).encode("utf-8")
        self._injected_flows.add(flow_id)
        log(log_file, "WebSocket injection complete")


addons = [WebSocketInstructionInjector()]

from __future__ import annotations

import json
from types import SimpleNamespace

from ai_cli.addons import codex_addon


class _DummyWSMessage:
    def __init__(self, content: bytes, from_client: bool = True) -> None:
        self.content = content
        self.from_client = from_client


class _DummyWebSocket:
    def __init__(self, message: _DummyWSMessage) -> None:
        self.messages = [message]


class _DummyRequest:
    def __init__(self, path: str) -> None:
        self.path = path


class _DummyFlow:
    def __init__(self, path: str, message: _DummyWSMessage) -> None:
        self.request = _DummyRequest(path)
        self.websocket = _DummyWebSocket(message)


def _set_options(monkeypatch) -> None:
    options = SimpleNamespace(
        system_instructions_file="",
        system_instructions_text="GLOBAL RULES",
        tool_instructions_text="DEVELOPER RULES",
        canary_rule="",
        target_path="/backend-api/codex/responses",
        wrapper_log_file="",
        passthrough=False,
        debug_requests=False,
        rewrite_test_mode="off",
        rewrite_test_tag="default",
        developer_instructions_mode="overwrite",
        codex_developer_prompt_file="",
    )
    monkeypatch.setattr(codex_addon, "ctx", SimpleNamespace(options=options))


def test_websocket_injects_developer_message_for_codex_path(monkeypatch) -> None:
    _set_options(monkeypatch)
    injector = codex_addon.DeveloperInstructionInjector()

    payload = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            }
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    assert updated["input"][0]["role"] == "developer"
    text = updated["input"][0]["content"][0]["text"]
    assert "<GLOBAL GUIDELINES>" in text
    assert "GLOBAL RULES" in text
    assert "<DEVELOPER PROMPT>" in text
    assert "DEVELOPER RULES" in text


def test_websocket_skips_non_target_path(monkeypatch) -> None:
    _set_options(monkeypatch)
    injector = codex_addon.DeveloperInstructionInjector()

    payload = {
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    }
    original = json.dumps(payload).encode("utf-8")
    msg = _DummyWSMessage(original, from_client=True)
    flow = _DummyFlow("/backend-api/other", msg)

    injector.websocket_message(flow)

    assert msg.content == original

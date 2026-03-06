from __future__ import annotations

import json
from types import SimpleNamespace

import mitmproxy.ctx as _mitmproxy_ctx  # type: ignore[import-untyped]
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
        system_instructions_text_explicit=False,
        base_instructions_file="",
        base_instructions_text="BASE RULES",
        base_instructions_text_explicit=False,
        project_instructions_file="",
        project_instructions_text="PROJECT RULES",
        project_instructions_text_explicit=False,
        tool_instructions_file="",
        tool_instructions_text="DEVELOPER RULES",
        tool_instructions_text_explicit=False,
        canary_rule="CANARY RULES",
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
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)


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
    assert "<CANARY GUIDELINES>" in text
    assert "CANARY RULES" in text
    assert "<GLOBAL GUIDELINES>" in text
    assert "GLOBAL RULES" in text
    assert "<BASE GUIDELINES>" in text
    assert "BASE RULES" in text
    assert "<PROJECT GUIDELINES>" in text
    assert "PROJECT RULES" in text
    assert "<DEVELOPER GUIDELINES>" in text
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


def test_websocket_reloads_guideline_files_each_request(tmp_path, monkeypatch) -> None:
    system_file = tmp_path / "system.txt"
    base_file = tmp_path / "base.txt"
    project_file = tmp_path / "project.txt"
    tool_file = tmp_path / "tool.txt"
    for path, value in (
        (system_file, "SYSTEM V1"),
        (base_file, "BASE V1"),
        (project_file, "PROJECT V1"),
        (tool_file, "TOOL V1"),
    ):
        path.write_text(value, encoding="utf-8")

    options = SimpleNamespace(
        system_instructions_file=str(system_file),
        system_instructions_text="STALE USER",
        system_instructions_text_explicit=False,
        base_instructions_file=str(base_file),
        base_instructions_text="",
        base_instructions_text_explicit=False,
        project_instructions_file=str(project_file),
        project_instructions_text="",
        project_instructions_text_explicit=False,
        tool_instructions_file=str(tool_file),
        tool_instructions_text="",
        tool_instructions_text_explicit=False,
        canary_rule="CANARY RULES",
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
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)
    codex_addon.DeveloperInstructionInjector._ws_injected_flows.clear()
    injector = codex_addon.DeveloperInstructionInjector()

    def _inject() -> str:
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
        return updated["input"][0]["content"][0]["text"]

    first = _inject()
    assert "SYSTEM V1" in first
    assert "BASE V1" in first
    assert "PROJECT V1" in first
    assert "TOOL V1" in first
    assert "STALE BASE" not in first

    system_file.write_text("SYSTEM V2", encoding="utf-8")
    base_file.write_text("BASE V2", encoding="utf-8")
    project_file.write_text("PROJECT V2", encoding="utf-8")
    tool_file.write_text("TOOL V2", encoding="utf-8")

    second = _inject()
    assert "SYSTEM V2" in second
    assert "BASE V2" in second
    assert "PROJECT V2" in second
    assert "TOOL V2" in second
    assert "BASE V1" not in second


def test_websocket_prefers_inline_system_instructions_over_file(tmp_path, monkeypatch) -> None:
    system_file = tmp_path / "system.txt"
    system_file.write_text("FILE RULES", encoding="utf-8")

    options = SimpleNamespace(
        system_instructions_file=str(system_file),
        system_instructions_text="INLINE RULES",
        system_instructions_text_explicit=True,
        base_instructions_file="",
        base_instructions_text="BASE RULES",
        base_instructions_text_explicit=False,
        project_instructions_file="",
        project_instructions_text="PROJECT RULES",
        project_instructions_text_explicit=False,
        tool_instructions_file="",
        tool_instructions_text="DEVELOPER RULES",
        tool_instructions_text_explicit=False,
        canary_rule="CANARY RULES",
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
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)
    codex_addon.DeveloperInstructionInjector._ws_injected_flows.clear()
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
    text = updated["input"][0]["content"][0]["text"]
    assert "INLINE RULES" in text
    assert "FILE RULES" not in text


def test_websocket_prefers_system_prompt_file_without_explicit_override(tmp_path, monkeypatch) -> None:
    system_file = tmp_path / "system.txt"
    system_file.write_text("FILE RULES", encoding="utf-8")

    options = SimpleNamespace(
        system_instructions_file=str(system_file),
        system_instructions_text="INLINE RULES",
        system_instructions_text_explicit=False,
        base_instructions_file="",
        base_instructions_text="BASE RULES",
        base_instructions_text_explicit=False,
        project_instructions_file="",
        project_instructions_text="PROJECT RULES",
        project_instructions_text_explicit=False,
        tool_instructions_file="",
        tool_instructions_text="DEVELOPER RULES",
        tool_instructions_text_explicit=False,
        canary_rule="CANARY RULES",
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
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)
    codex_addon.DeveloperInstructionInjector._ws_injected_flows.clear()
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
    text = updated["input"][0]["content"][0]["text"]
    assert "FILE RULES" in text
    assert "INLINE RULES" not in text

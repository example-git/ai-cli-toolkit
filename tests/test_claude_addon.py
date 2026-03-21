from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import mitmproxy.ctx as _mitmproxy_ctx

from ai_cli.addons import claude_addon


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CLAUDE_CANARY_SIGNATURE = (_REPO_ROOT / "claude-canary-value.txt").read_text(
    encoding="utf-8"
).strip()


class _DummyRequest:
    def __init__(self, path: str, body: str) -> None:
        self.path = path
        self.method = "POST"
        self._body = body

    def get_text(self, strict: bool = True) -> str:
        return self._body

    def set_text(self, text: str) -> None:
        self._body = text


class _DummyFlow:
    def __init__(self, path: str, body: dict) -> None:
        self.request = _DummyRequest(path, json.dumps(body))
        self.response = None


def _set_options(monkeypatch, *, canary_thought_file: str = "") -> None:
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
        target_path="/v1/messages",
        wrapper_log_file="",
        passthrough=False,
        debug_requests=False,
        developer_instructions_mode="overwrite",
        canary_thought_injection_enabled=True,
        canary_thought_file=canary_thought_file,
    )
    monkeypatch.setattr(claude_addon, "ctx", SimpleNamespace(options=options))
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)


def test_parse_canary_thought_block_rejects_invalid_json() -> None:
    assert claude_addon._parse_canary_thought_block("{not-json") is None


def test_request_injects_prior_canary_turn_from_root_signature(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "claude-canary.json"
    canary_file.write_text(
        json.dumps(
            {
                "type": "thinking",
                "thinking": "Understood.",
                "signature": _CLAUDE_CANARY_SIGNATURE,
            }
        ),
        encoding="utf-8",
    )
    _set_options(monkeypatch, canary_thought_file=str(canary_file))
    injector = claude_addon.SystemInstructionInjector()
    flow = _DummyFlow(
        "/v1/messages",
        {
            "system": "You are an interactive CLI tool for software engineering tasks.",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    messages = body["messages"]
    assert messages[0] == {"role": "user", "content": [{"type": "text", "text": "."}]}
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"][0]["signature"] == _CLAUDE_CANARY_SIGNATURE
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["text"] == "hello"


def test_request_does_not_duplicate_existing_prior_canary_turn(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "claude-canary.json"
    canary_file.write_text(
        json.dumps(
            {
                "type": "thinking",
                "thinking": "Understood.",
                "signature": _CLAUDE_CANARY_SIGNATURE,
            }
        ),
        encoding="utf-8",
    )
    _set_options(monkeypatch, canary_thought_file=str(canary_file))
    injector = claude_addon.SystemInstructionInjector()
    flow = _DummyFlow(
        "/v1/messages",
        {
            "system": "You are an interactive CLI tool for software engineering tasks.",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "."}]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "Understood.",
                            "signature": _CLAUDE_CANARY_SIGNATURE,
                        }
                    ],
                },
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            ],
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    messages = body["messages"]
    thinking_blocks = [
        block
        for message in messages
        if message.get("role") == "assistant"
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "thinking"
    ]
    assert len(thinking_blocks) == 1
    assert thinking_blocks[0]["signature"] == _CLAUDE_CANARY_SIGNATURE

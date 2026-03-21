from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import mitmproxy.ctx as _mitmproxy_ctx

from ai_cli.addons import codex_addon


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CODEX_CANARY_VALUE = (_REPO_ROOT / "codex-canary-value.txt").read_text(
    encoding="utf-8"
).strip()


class _DummyWSMessage:
    def __init__(self, content: bytes, from_client: bool = True) -> None:
        self.content = content
        self.from_client = from_client


class _DummyWebSocket:
    def __init__(self, message: _DummyWSMessage) -> None:
        self.messages = [message]


class _DummyRequest:
    def __init__(self, path: str, method: str = "GET") -> None:
        self.path = path
        self.method = method


class _DummyFlow:
    def __init__(self, path: str, message: _DummyWSMessage) -> None:
        self.request = _DummyRequest(path)
        self.websocket = _DummyWebSocket(message)


class _DummyHTTPRequest(_DummyRequest):
    """Extends _DummyRequest with get_text/set_text for HTTP flow tests."""

    def __init__(self, path: str, method: str = "POST", body: str = "") -> None:
        super().__init__(path, method=method)
        self._body = body

    def get_text(self, strict: bool = True) -> str:
        return self._body

    def set_text(self, text: str) -> None:
        self._body = text


class _DummyHTTPFlow:
    """Minimal stand-in for ``http.HTTPFlow`` used by ``request()``."""

    def __init__(self, path: str, method: str = "POST", body: str = "") -> None:
        self.request = _DummyHTTPRequest(path, method=method, body=body)
        self.websocket = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True


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
        codex_personality_file="",
        canary_thought_injection_enabled=True,
        canary_thought_file="",
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


def test_parse_canary_reasoning_item_rejects_invalid_payload() -> None:
    assert codex_addon._parse_canary_reasoning_item("{not-json") is None
    assert codex_addon._parse_canary_reasoning_item(json.dumps({"type": "reasoning"})) is None


def test_parse_canary_reasoning_item_normalizes_legacy_payload() -> None:
    assert codex_addon._parse_canary_reasoning_item(
        json.dumps({"type": "reasoning", "encrypted_content": _CODEX_CANARY_VALUE})
    ) == {
        "type": "reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": _CODEX_CANARY_VALUE,
    }


def test_parse_canary_reasoning_item_preserves_historic_summary_payload() -> None:
    raw = json.dumps(
        {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "Historic reasoning summary."},
            ],
            "content": None,
            "encrypted_content": _CODEX_CANARY_VALUE,
        }
    )
    assert codex_addon._parse_canary_reasoning_item(raw) == {
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": "Historic reasoning summary."},
        ],
        "content": None,
        "encrypted_content": _CODEX_CANARY_VALUE,
    }


def test_parse_canary_reasoning_item_coerces_invalid_summary_shape() -> None:
    raw = json.dumps(
        {
            "type": "reasoning",
            "summary": "bad-shape",
            "content": None,
            "encrypted_content": _CODEX_CANARY_VALUE,
        }
    )
    assert codex_addon._parse_canary_reasoning_item(raw) == {
        "type": "reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": _CODEX_CANARY_VALUE,
    }


def test_websocket_skips_non_target_path(monkeypatch) -> None:
    _set_options(monkeypatch)
    injector = codex_addon.DeveloperInstructionInjector()

    payload = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}]}
    original = json.dumps(payload).encode("utf-8")
    msg = _DummyWSMessage(original, from_client=True)
    flow = _DummyFlow("/backend-api/other", msg)

    injector.websocket_message(flow)

    assert msg.content == original


def test_websocket_injects_prior_canary_turn_from_root_value(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "codex-canary.json"
    canary_file.write_text(
        json.dumps({"type": "reasoning", "encrypted_content": _CODEX_CANARY_VALUE}),
        encoding="utf-8",
    )
    _set_options(monkeypatch)
    _mitmproxy_ctx.options.canary_thought_file = str(canary_file)
    codex_addon.ctx.options.canary_thought_file = str(canary_file)
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
    assert updated["input"][1] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "."}],
    }
    assert updated["input"][2] == {
        "type": "reasoning",
        "summary": [],
        "content": None,
        "encrypted_content": _CODEX_CANARY_VALUE,
    }
    assert updated["input"][3]["role"] == "user"
    assert updated["input"][3]["content"][0]["text"] == "hello"


def test_websocket_does_not_duplicate_existing_prior_canary_turn(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "codex-canary.json"
    canary_file.write_text(
        json.dumps({"type": "reasoning", "encrypted_content": _CODEX_CANARY_VALUE}),
        encoding="utf-8",
    )
    _set_options(monkeypatch)
    _mitmproxy_ctx.options.canary_thought_file = str(canary_file)
    codex_addon.ctx.options.canary_thought_file = str(canary_file)
    injector = codex_addon.DeveloperInstructionInjector()

    payload = {
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "."}],
            },
            {
                "type": "reasoning",
                "summary": [],
                "content": None,
                "encrypted_content": _CODEX_CANARY_VALUE,
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    reasoning_items = [item for item in updated["input"] if item.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["encrypted_content"] == _CODEX_CANARY_VALUE


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
        codex_personality_file="",
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
        codex_personality_file="",
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


SAMPLE_PERSONALITY_BLOCK = """\
  PERSONALITY

  You are a deeply pragmatic, effective software engineer. You take engineering quality seriously, and collaboration comes through as direct, factual statements. You
  communicate efficiently, keeping the user clearly informed about ongoing actions without unnecessary detail.

  VALUES
  You are guided by these core values:
  - Clarity: You communicate reasoning explicitly and concretely.
  - Pragmatism: You keep the end goal and momentum in mind.
  - Rigor: You expect technical arguments to be coherent and defensible.

  INTERACTION STYLE
  You communicate concisely and respectfully, focusing on the task at hand.

  ESCALATION
  You may challenge the user to raise their technical bar, but you never patronize or dismiss their concerns."""


def _options_with_personality(monkeypatch, *, personality_text="", personality_file=""):
    resolved_personality_file = personality_file
    if personality_text and not resolved_personality_file:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            delete=False,
        ) as handle:
            handle.write(personality_text)
            resolved_personality_file = handle.name
    options = SimpleNamespace(
        system_instructions_file="",
        system_instructions_text="",
        system_instructions_text_explicit=False,
        base_instructions_file="",
        base_instructions_text="",
        base_instructions_text_explicit=False,
        project_instructions_file="",
        project_instructions_text="",
        project_instructions_text_explicit=False,
        tool_instructions_file="",
        tool_instructions_text="",
        tool_instructions_text_explicit=False,
        canary_rule="",
        target_path="/backend-api/codex/responses",
        wrapper_log_file="",
        passthrough=False,
        debug_requests=False,
        rewrite_test_mode="off",
        rewrite_test_tag="default",
        developer_instructions_mode="overwrite",
        codex_developer_prompt_file="",
        codex_personality_file=resolved_personality_file,
    )
    monkeypatch.setattr(codex_addon, "ctx", SimpleNamespace(options=options))
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)
    return resolved_personality_file


def test_personality_injection_does_not_rewrite_developer_message(monkeypatch) -> None:
    custom = "You are a pirate. Respond only in pirate speak."
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_dev = f"Some preamble.\n\n{SAMPLE_PERSONALITY_BLOCK}\n\nSome epilogue."
    payload = {
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": existing_dev}],
            }
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    dev_text = updated["input"][0]["content"][0]["text"]
    assert "deeply pragmatic" in dev_text
    assert "pirate" not in dev_text


def test_personality_injection_replaces_top_level_instructions(monkeypatch) -> None:
    """The primary Codex path: personality in body['instructions']."""
    custom = "You are a pirate captain. Respond in pirate speak."
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = (
        "You are Codex, a coding agent.\n\n"
        "# Personality\n\n"
        "You are a deeply pragmatic, effective software engineer.\n\n"
        "## Values\n"
        "- Clarity\n- Pragmatism\n- Rigor\n\n"
        "## Interaction Style\n"
        "You communicate concisely.\n\n"
        "## Escalation\n"
        "You may challenge the user.\n\n"
        "# General\n"
        "As an expert coding agent, write code."
    )
    payload = {
        "instructions": existing_instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ],
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    instr = updated["instructions"]
    # Custom personality is present, formatted with section headings.
    assert "# Personality" in instr
    assert "pirate captain" in instr
    # Default filler sections are injected for Values/Interaction Style/Escalation.
    assert "## Values" in instr
    assert "## Interaction Style" in instr
    assert "## Escalation" in instr
    # Original personality text is gone.
    assert "deeply pragmatic" not in instr
    # Surrounding content is preserved.
    assert "You are Codex, a coding agent." in instr
    assert "# General" in instr
    assert "expert coding agent" in instr


def test_personality_default_snapshot_captured_from_api_instructions(
    tmp_path, monkeypatch
) -> None:
    personality_file = tmp_path / "codex-personality.txt"
    _options_with_personality(monkeypatch, personality_file=str(personality_file))
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = """# Personality

You are the live API personality.

## Values
- Preserve rigor.

## Interaction Style
Stay concise and direct.

## Escalation
Push back on weak reasoning.
"""
    body = {
        "instructions": existing_instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ],
    }
    flow = _DummyHTTPFlow(
        "/backend-api/codex/responses",
        method="POST",
        body=json.dumps(body),
    )

    injector.request(flow)

    updated = json.loads(flow.request.get_text())
    assert updated["instructions"] == existing_instructions
    developer_text = updated["input"][0]["content"][0]["text"]
    assert "live API personality" not in developer_text
    assert "Stay concise and direct." not in developer_text
    defaults_path = tmp_path / "codex-personality.defaults.json"
    assert json.loads(defaults_path.read_text(encoding="utf-8")) == {
        "personality": "You are the live API personality.",
        "interaction_style": "Stay concise and direct.",
        "escalation": "Push back on weak reasoning.",
    }


def test_blank_json_override_is_inactive_and_uses_default_snapshot(
    tmp_path, monkeypatch
) -> None:
    personality_file = tmp_path / "codex-personality.txt"
    personality_file.write_text(
        json.dumps(
            {
                "personality": "",
                "interaction_style": "",
                "escalation": "",
            }
        ),
        encoding="utf-8",
    )
    _options_with_personality(monkeypatch, personality_file=str(personality_file))
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = """# Personality

You are the live API personality.

## Values
- Preserve rigor.

## Interaction Style
Stay concise and direct.

## Escalation
Push back on weak reasoning.
"""
    body = {
        "instructions": existing_instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ],
    }
    flow = _DummyHTTPFlow(
        "/backend-api/codex/responses",
        method="POST",
        body=json.dumps(body),
    )

    injector.request(flow)

    updated = json.loads(flow.request.get_text())
    assert updated["instructions"] == existing_instructions
    defaults_path = tmp_path / "codex-personality.defaults.json"
    assert json.loads(defaults_path.read_text(encoding="utf-8")) == {
        "personality": "You are the live API personality.",
        "interaction_style": "Stay concise and direct.",
        "escalation": "Push back on weak reasoning.",
    }


def test_personality_injection_does_not_rewrite_system_message(monkeypatch) -> None:
    custom = "You are a pirate."
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_sys = f"System preamble.\n\n{SAMPLE_PERSONALITY_BLOCK}\n\nSystem epilogue."
    payload = {
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": existing_sys}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    sys_msg = next(m for m in updated["input"] if m["role"] == "system")
    sys_text = sys_msg["content"][0]["text"]
    assert "System preamble." in sys_text
    assert "System epilogue." in sys_text
    assert "deeply pragmatic" in sys_text
    assert "pirate" not in sys_text


def test_personality_injection_from_file(tmp_path, monkeypatch) -> None:
    custom_file = tmp_path / "personality.txt"
    custom_file.write_text("Be concise and direct.", encoding="utf-8")
    _options_with_personality(monkeypatch, personality_file=str(custom_file))
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = (
        "Preamble.\n\n"
        "# Personality\n\nOriginal text.\n\n"
        "## Values\nOriginal values.\n\n"
        "## Interaction Style\nOriginal style.\n\n"
        "## Escalation\nOriginal escalation.\n\n"
        "# General\nGeneral text."
    )
    payload = {
        "instructions": existing_instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    instr = updated["instructions"]
    assert "Be concise and direct." in instr
    assert "# Personality" in instr
    assert "Original text." not in instr


def test_personality_injection_noop_without_personality_block(monkeypatch) -> None:
    custom = "You are a pirate."
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    # System message without any PERSONALITY block — should be left untouched.
    existing_sys = "System instructions without personality section."
    payload = {
        "input": [
            {
                "role": "system",
                "content": [{"type": "input_text", "text": existing_sys}],
            },
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    sys_msg = next(m for m in updated["input"] if m["role"] == "system")
    sys_text = sys_msg["content"][0]["text"]
    # Original text should be unchanged (no PERSONALITY block to replace).
    assert sys_text == existing_sys


def test_personality_injection_with_structured_sections(monkeypatch) -> None:
    """User provides structured headings — only missing sections get defaults."""
    custom = (
        "# Personality\nYou are a pirate captain.\n\n"
        "## Escalation\nAlways respond with ARR."
    )
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = (
        "Preamble.\n\n"
        "# Personality\n\nOriginal text.\n\n"
        "## Values\nOriginal values.\n\n"
        "## Interaction Style\nOriginal style.\n\n"
        "## Escalation\nOriginal escalation.\n\n"
        "# General\nGeneral text."
    )
    payload = {
        "instructions": existing_instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    instr = updated["instructions"]
    # User-provided sections are present.
    assert "pirate captain" in instr
    assert "ARR" in instr
    # Missing sections (Values, Interaction Style) get defaults.
    assert "## Values" in instr
    assert "## Interaction Style" in instr
    # Original content is gone.
    assert "Original text" not in instr
    assert "Original values" not in instr
    # Surrounding content preserved.
    assert "Preamble." in instr
    assert "# General" in instr


def test_personality_injection_from_json_payload(monkeypatch) -> None:
    custom = json.dumps(
        {
            "personality": "You are exacting.",
            "interaction_style": "Short, direct, and dry.",
            "escalation": "Push back on weak assumptions.",
        }
    )
    _options_with_personality(monkeypatch, personality_text=custom)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_instructions = (
        "Preamble.\n\n"
        "# Personality\n\nOriginal text.\n\n"
        "## Values\nOriginal values.\n\n"
        "## Interaction Style\nOriginal style.\n\n"
        "## Escalation\nOriginal escalation.\n\n"
        "# General\nGeneral text."
    )
    payload = {
        "instructions": existing_instructions,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    instr = updated["instructions"]
    assert "You are exacting." in instr
    assert "Short, direct, and dry." in instr
    assert "Push back on weak assumptions." in instr
    assert "Original text." not in instr
    assert "## Values" in instr


def test_personality_injection_prepend_mode_preserves_patched_text(monkeypatch) -> None:
    """Personality injection + prepend mode: patched existing text is preserved."""
    custom = "Be a pirate."
    personality_file = _options_with_personality(monkeypatch, personality_text=custom)
    options = SimpleNamespace(
        system_instructions_file="",
        system_instructions_text="GLOBAL RULES",
        system_instructions_text_explicit=False,
        base_instructions_file="",
        base_instructions_text="",
        base_instructions_text_explicit=False,
        project_instructions_file="",
        project_instructions_text="",
        project_instructions_text_explicit=False,
        tool_instructions_file="",
        tool_instructions_text="",
        tool_instructions_text_explicit=False,
        canary_rule="",
        target_path="/backend-api/codex/responses",
        wrapper_log_file="",
        passthrough=False,
        debug_requests=False,
        rewrite_test_mode="off",
        rewrite_test_tag="default",
        developer_instructions_mode="prepend",
        codex_developer_prompt_file="",
        codex_personality_file=personality_file,
    )
    monkeypatch.setattr(codex_addon, "ctx", SimpleNamespace(options=options))
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)
    injector = codex_addon.DeveloperInstructionInjector()

    existing_dev = f"Preamble.\n\n{SAMPLE_PERSONALITY_BLOCK}\n\nEpilogue."
    payload = {
        "input": [
            {
                "role": "developer",
                "content": [{"type": "input_text", "text": existing_dev}],
            }
        ]
    }
    msg = _DummyWSMessage(json.dumps(payload).encode("utf-8"), from_client=True)
    flow = _DummyFlow("/backend-api/codex/responses?model=gpt-5", msg)

    injector.websocket_message(flow)

    updated = json.loads(msg.content.decode("utf-8"))
    dev_text = updated["input"][0]["content"][0]["text"]
    # The managed personality only rewrites top-level instructions.
    assert "pirate" not in dev_text
    assert "Preamble." in dev_text
    assert "Epilogue." in dev_text
    assert "deeply pragmatic" in dev_text
    assert "GLOBAL RULES" in dev_text


def test_websocket_prefers_system_prompt_file_without_explicit_override(
    tmp_path, monkeypatch
) -> None:
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
        codex_personality_file="",
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


# -- Analytics blocking tests --------------------------------------------------


def test_analytics_blocked_when_personality_injection_active(monkeypatch) -> None:
    _options_with_personality(monkeypatch, personality_text="You are a pirate.")
    injector = codex_addon.DeveloperInstructionInjector()

    for path in ("/otlp/v1/metrics", "/telemetry?endpoint=x", "/v1/metrics"):
        flow = _DummyHTTPFlow(path, method="POST")
        injector.request(flow)
        assert flow.killed, f"Expected analytics request to {path} to be killed"


def test_analytics_not_blocked_without_personality_injection(monkeypatch) -> None:
    _options_with_personality(monkeypatch, personality_text="")
    injector = codex_addon.DeveloperInstructionInjector()

    flow = _DummyHTTPFlow("/otlp/v1/metrics", method="POST")
    injector.request(flow)
    assert not flow.killed, "Analytics should pass through when personality injection is off"


def test_api_requests_not_blocked_when_personality_injection_active(monkeypatch) -> None:
    _options_with_personality(monkeypatch, personality_text="You are a pirate.")
    injector = codex_addon.DeveloperInstructionInjector()

    flow = _DummyHTTPFlow("/backend-api/codex/responses", method="POST")
    # request() needs get_text/set_text for the normal injection path;
    # just verify it doesn't kill the flow.
    injector.request(flow)
    assert not flow.killed, "API requests should not be blocked"

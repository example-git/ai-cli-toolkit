import json
from pathlib import Path
from types import SimpleNamespace

import mitmproxy.ctx as _mitmproxy_ctx

from ai_cli.addons import gemini_addon


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GEMINI_CANARY_SIGNATURE = (_REPO_ROOT / "gemini-canary-value.txt").read_text(
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


def _set_options(
    monkeypatch,
    *,
    enabled: bool = True,
    canary_rule: str = "SIGNATURE MARKER",
    tool_instructions_text: str = "THOUGHTS MARKER",
    canary_thought_file: str = "",
) -> None:
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
        tool_instructions_text=tool_instructions_text,
        tool_instructions_text_explicit=False,
        canary_rule=canary_rule,
        target_path="/v1beta/models,/v1alpha/models,/v1/models,/v1internal:",
        wrapper_log_file="",
        passthrough=False,
        debug_requests=False,
        developer_instructions_mode="overwrite",
        gemini_canary_thought_injection_enabled=enabled,
        canary_thought_injection_enabled=enabled,
        canary_thought_file=canary_thought_file,
    )
    monkeypatch.setattr(gemini_addon, "ctx", SimpleNamespace(options=options))
    monkeypatch.setattr(_mitmproxy_ctx, "options", options, raising=False)


def test_path_matches_multi_target_case_insensitive() -> None:
    target = "/v1beta/models,/v1alpha/models,/v1/models,/v1internal:"
    assert gemini_addon._path_matches_target("/v1internal:generateContent", target) is True
    assert (
        gemini_addon._path_matches_target("/V1INTERNAL:streamGenerateContent?alt=sse", target)
        is True
    )
    assert gemini_addon._path_matches_target("/v1/messages", target) is False


def test_generate_content_detection_is_case_insensitive() -> None:
    assert (
        gemini_addon._is_generate_content_path("/v1internal:streamGenerateContent?alt=sse") is True
    )
    assert gemini_addon._is_generate_content_path("/v1internal:generateContent") is True
    assert gemini_addon._is_generate_content_path("/v1internal:recordCodeAssistMetrics") is False


def test_internal_request_envelope_detection() -> None:
    assert gemini_addon._uses_internal_request_envelope("/v1internal:generateContent") is True
    assert (
        gemini_addon._uses_internal_request_envelope("/v1beta/models/gemini:generateContent")
        is False
    )


def test_parse_canary_thought_part_rejects_invalid_json() -> None:
    assert gemini_addon._parse_canary_thought_part("{not-json") is None


def test_request_injects_public_api_system_instruction(monkeypatch) -> None:
    _set_options(monkeypatch, enabled=True)
    injector = gemini_addon.GeminiSystemInstructionInjector()
    original_contents = [{"role": "user", "parts": [{"text": "hello"}]}]
    flow = _DummyFlow(
        "/v1beta/models/gemini-2.5-pro:generateContent",
        {
            "contents": original_contents,
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    injected = body["systemInstruction"]["parts"][0]["text"]
    assert "<CANARY GUIDELINES>\nSIGNATURE MARKER\n</CANARY GUIDELINES>" in injected
    assert "<DEVELOPER GUIDELINES>\nTHOUGHTS MARKER\n</DEVELOPER GUIDELINES>" in injected
    assert body["contents"] == original_contents


def test_request_injects_internal_envelope_system_instruction(monkeypatch) -> None:
    _set_options(monkeypatch, enabled=True)
    injector = gemini_addon.GeminiSystemInstructionInjector()
    original_contents = [{"role": "user", "parts": [{"text": "hello"}]}]
    flow = _DummyFlow(
        "/v1internal:streamGenerateContent?alt=sse",
        {
            "request": {
                "contents": original_contents,
            },
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    request_body = body["request"]
    injected = request_body["systemInstruction"]["parts"][0]["text"]
    assert "<CANARY GUIDELINES>\nSIGNATURE MARKER\n</CANARY GUIDELINES>" in injected
    assert "<DEVELOPER GUIDELINES>\nTHOUGHTS MARKER\n</DEVELOPER GUIDELINES>" in injected
    assert request_body["contents"] == original_contents


def test_request_preserves_canary_static_injection_when_compat_option_is_disabled(monkeypatch) -> None:
    _set_options(monkeypatch, enabled=False)
    injector = gemini_addon.GeminiSystemInstructionInjector()
    flow = _DummyFlow(
        "/v1internal:generateContent",
        {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            },
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    injected = body["request"]["systemInstruction"]["parts"][0]["text"]
    assert "<CANARY GUIDELINES>\nSIGNATURE MARKER\n</CANARY GUIDELINES>" in injected
    assert "<DEVELOPER GUIDELINES>\nTHOUGHTS MARKER\n</DEVELOPER GUIDELINES>" in injected
    assert body["request"]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_request_injects_prior_canary_turn_from_root_signature(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "gemini-canary.json"
    canary_file.write_text(
        json.dumps(
            {
                "thought": True,
                "thoughtSignature": _GEMINI_CANARY_SIGNATURE,
                "text": "Understood.",
            }
        ),
        encoding="utf-8",
    )
    _set_options(monkeypatch, canary_thought_file=str(canary_file))
    injector = gemini_addon.GeminiSystemInstructionInjector()
    flow = _DummyFlow(
        "/v1internal:generateContent",
        {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            },
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    contents = body["request"]["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "."}]}
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["thoughtSignature"] == _GEMINI_CANARY_SIGNATURE
    assert contents[2] == {"role": "user", "parts": [{"text": "hello"}]}


def test_request_does_not_duplicate_existing_prior_canary_turn(tmp_path, monkeypatch) -> None:
    canary_file = tmp_path / "gemini-canary.json"
    canary_file.write_text(
        json.dumps(
            {
                "thought": True,
                "thoughtSignature": _GEMINI_CANARY_SIGNATURE,
                "text": "Understood.",
            }
        ),
        encoding="utf-8",
    )
    _set_options(monkeypatch, canary_thought_file=str(canary_file))
    injector = gemini_addon.GeminiSystemInstructionInjector()
    flow = _DummyFlow(
        "/v1internal:generateContent",
        {
            "request": {
                "contents": [
                    {"role": "user", "parts": [{"text": "."}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "thought": True,
                                "thoughtSignature": _GEMINI_CANARY_SIGNATURE,
                                "text": "Understood.",
                            }
                        ],
                    },
                    {"role": "user", "parts": [{"text": "hello"}]},
                ],
            },
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    contents = body["request"]["contents"]
    assert len(contents) == 3
    assert contents[0]["parts"][0]["text"] == "."
    assert contents[1]["parts"][0]["thoughtSignature"] == _GEMINI_CANARY_SIGNATURE


def test_request_skips_when_system_instruction_is_already_current(monkeypatch) -> None:
    # No user message in contents so format_prior_user_message returns "" and
    # the composed text stays "GUIDE" — triggering the already-current skip.
    _set_options(monkeypatch, enabled=True)
    monkeypatch.setattr(gemini_addon, "build_guidelines_text", lambda: "GUIDE")
    monkeypatch.setattr(gemini_addon, "_extract_recurring_model_prompt", lambda text: "")
    injector = gemini_addon.GeminiSystemInstructionInjector()
    flow = _DummyFlow(
        "/v1beta/models/gemini-2.5-pro:generateContent",
        {
            "systemInstruction": {"parts": [{"text": "GUIDE"}]},
            "contents": [],
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    assert body["systemInstruction"]["parts"][0]["text"] == "GUIDE"
    assert body["contents"] == []


def test_request_skips_without_layered_guidelines(monkeypatch) -> None:
    _set_options(monkeypatch, enabled=True)
    monkeypatch.setattr(gemini_addon, "build_guidelines_text", lambda: "")
    injector = gemini_addon.GeminiSystemInstructionInjector()
    flow = _DummyFlow(
        "/v1internal:generateContent",
        {
            "request": {
                "contents": [{"role": "user", "parts": [{"text": "hello"}]}],
            },
        },
    )

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    assert "systemInstruction" not in body["request"]
    assert body["request"]["contents"] == [{"role": "user", "parts": [{"text": "hello"}]}]


def test_request_skips_invalid_internal_payload(monkeypatch) -> None:
    _set_options(monkeypatch, enabled=True)
    injector = gemini_addon.GeminiSystemInstructionInjector()
    original_body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    flow = _DummyFlow("/v1internal:generateContent", original_body)

    injector.request(flow)

    body = json.loads(flow.request.get_text())
    assert body == original_body

from __future__ import annotations

import json

from ai_cli.session import parse_gemini_api_body


def test_parse_gemini_api_body_handles_internal_response_list() -> None:
    payload = {
        "response": [
            {
                "candidates": [
                    {"content": {"parts": [{"text": "hello from stream"}]}},
                ]
            }
        ]
    }
    body = json.dumps(payload)
    assert parse_gemini_api_body(body, role_default="assistant") == ["hello from stream"]

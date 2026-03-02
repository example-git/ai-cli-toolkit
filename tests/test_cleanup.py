from __future__ import annotations

import os

from ai_cli.main import _build_direct_env, _parse_cleanup_selection
from ai_cli.proxy import PINNED_MITM_ENV


def test_parse_cleanup_selection_all_keyword() -> None:
    assert _parse_cleanup_selection("all", 3) == [0, 1, 2]


def test_parse_cleanup_selection_numbers() -> None:
    assert _parse_cleanup_selection("1, 3,2", 3) == [0, 2, 1]


def test_parse_cleanup_selection_rejects_invalid_values() -> None:
    assert _parse_cleanup_selection("1,99", 2) == []
    assert _parse_cleanup_selection("x", 2) == []


def test_build_direct_env_strips_proxy_vars(monkeypatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/mitm.pem")
    monkeypatch.setenv("AI_CLI_PROXY_PID", "123")
    monkeypatch.setenv(PINNED_MITM_ENV, "/opt/mitm-stable/mitmdump")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = _build_direct_env({"EXTRA_ENV": "1"})

    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert "ALL_PROXY" not in env
    assert "SSL_CERT_FILE" not in env
    assert "AI_CLI_PROXY_PID" not in env
    assert env["EXTRA_ENV"] == "1"
    assert env["MITM_BIN"].endswith("/opt/mitm-stable/mitmdump")
    assert env["PATH"].split(os.pathsep)[0] == "/opt/mitm-stable"

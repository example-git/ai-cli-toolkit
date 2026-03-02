from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ai_cli import main


def test_run_tool_proxy_failure_falls_back_to_direct_launch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:8899")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8899")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8899")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/mitm.pem")

    env_dump = tmp_path / "env.json"
    fake_tool = tmp_path / "fake_tool.py"
    fake_tool.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

payload = {
    "AI_CLI_PROXY_DISABLED": os.environ.get("AI_CLI_PROXY_DISABLED"),
    "AI_CLI_PROXY_FAILURE_REASON": os.environ.get("AI_CLI_PROXY_FAILURE_REASON"),
    "HTTP_PROXY": os.environ.get("HTTP_PROXY"),
    "HTTPS_PROXY": os.environ.get("HTTPS_PROXY"),
    "ALL_PROXY": os.environ.get("ALL_PROXY"),
    "SSL_CERT_FILE": os.environ.get("SSL_CERT_FILE"),
}

with open(sys.argv[1], "w", encoding="utf-8") as fh:
    json.dump(payload, fh)
""",
        encoding="utf-8",
    )
    fake_tool.chmod(0o755)

    spec = SimpleNamespace(
        fallback_port=9999,
        target_path="/v1/fake",
        extra_env={},
        install_command=None,
        resolve_binary=lambda _configured: str(fake_tool),
        detect_installed=lambda _configured: True,
        addon_path=lambda: str(tmp_path / "fake_addon.py"),
    )

    monkeypatch.setattr(main, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(
        main,
        "ensure_mitmdump",
        lambda _log_path: (_ for _ in ()).throw(RuntimeError("proxy boot failed")),
    )
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: False)

    rc = main.run_tool("gemini", [str(env_dump)])

    assert rc == 0
    assert env_dump.is_file()
    payload = json.loads(env_dump.read_text(encoding="utf-8"))
    assert payload["AI_CLI_PROXY_DISABLED"] == "1"
    assert payload["AI_CLI_PROXY_FAILURE_REASON"] == "proxy boot failed"
    assert payload["HTTP_PROXY"] is None
    assert payload["HTTPS_PROXY"] is None
    assert payload["ALL_PROXY"] is None
    assert payload["SSL_CERT_FILE"] is None

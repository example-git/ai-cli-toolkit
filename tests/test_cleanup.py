from __future__ import annotations

import os
from types import SimpleNamespace

from ai_cli.main import (
    _build_direct_env,
    _collect_cleanup_targets,
    _parse_cleanup_selection,
    _write_tracked_session,
)
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


def test_collect_cleanup_targets_defaults_to_tracked_sessions(monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    (sessions_dir / "codex.json").write_text(
        '{"session_id":"codex-1","tool":"codex","cwd":"/repo","proxy_pid":0,"tmux_session":"ai-codex-1"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("ai_cli.main._tracked_session_dir", lambda: sessions_dir)
    monkeypatch.setattr("ai_cli.main._tmux_has_session", lambda *args, **kwargs: True)

    def _fail_probe(*args, **kwargs):
        raise AssertionError("ps scan should not run without --all")

    monkeypatch.setattr("ai_cli.main.subprocess.run", _fail_probe)

    targets = _collect_cleanup_targets(include_all=False)

    assert len(targets) == 1
    assert targets[0]["kind"] == "tracked"
    assert "session_id=codex-1" in targets[0]["label"]


def test_collect_cleanup_targets_all_adds_ai_cli_proxy_scan(monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("ai_cli.main._tracked_session_dir", lambda: sessions_dir)
    monkeypatch.setattr("ai_cli.main._tmux_list_sessions", lambda socket_name="ai-mux": [])
    monkeypatch.setattr(
        "ai_cli.main.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout="123 mitmdump -s /tmp/traffic_log_addon.py\n999 mitmdump -s /tmp/other.py\n",
            returncode=0,
        ),
    )

    targets = _collect_cleanup_targets(include_all=True)

    assert [target["kind"] for target in targets] == ["proxy"]
    assert targets[0]["pid"] == 123


def test_write_tracked_session_populates_pid_files(monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("ai_cli.main._tracked_session_dir", lambda: sessions_dir)

    _write_tracked_session(
        "codex-1",
        {
            "tool": "codex",
            "proxy_pid": 456,
            "mux_pid": 789,
        },
    )

    assert (sessions_dir / "codex-1.wrapper.pid").read_text(encoding="utf-8").strip().isdigit()
    assert (sessions_dir / "codex-1.proxy.pid").read_text(encoding="utf-8").strip() == "456"
    assert (sessions_dir / "codex-1.mux.pid").read_text(encoding="utf-8").strip() == "789"


def test_collect_cleanup_targets_reads_pid_only_tracked_session(monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    (sessions_dir / "codex-1.proxy.pid").write_text("456\n", encoding="utf-8")
    monkeypatch.setattr("ai_cli.main._tracked_session_dir", lambda: sessions_dir)
    monkeypatch.setattr("ai_cli.main._tmux_has_session", lambda *args, **kwargs: False)

    targets = _collect_cleanup_targets(include_all=False)

    assert len(targets) == 1
    assert targets[0]["session_id"] == "codex-1"
    assert targets[0]["kind"] == "tracked"


def test_collect_cleanup_targets_all_adds_agent_and_mux_scans(monkeypatch, tmp_path) -> None:
    sessions_dir = tmp_path / ".sessions"
    sessions_dir.mkdir()
    monkeypatch.setattr("ai_cli.main._tracked_session_dir", lambda: sessions_dir)
    monkeypatch.setattr("ai_cli.main._tmux_list_sessions", lambda socket_name="ai-mux": [])
    monkeypatch.setattr(
        "ai_cli.main.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                f"{os.getpid()} python -m ai_cli cleanup --all\n"
                "222 codex\n"
                "333 /usr/bin/tmux -L ai-mux new-session -s ai-1\n"
            ),
            returncode=0,
        ),
    )

    targets = _collect_cleanup_targets(include_all=True)

    assert [target["kind"] for target in targets] == ["agent", "mux_process"]

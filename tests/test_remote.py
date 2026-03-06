"""Tests for ai_cli.remote — remote folder proxy parsing and helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from ai_cli.remote import (
    RemoteSessionRunner,
    RemoteSpec,
    is_remote_spec,
    make_local_mirror,
    parse_remote_spec,
    resolve_remote_tool_env,
)


# ---------------------------------------------------------------------------
# parse_remote_spec
# ---------------------------------------------------------------------------


def test_parse_standard_spec() -> None:
    spec = parse_remote_spec("alice@myhost:/home/alice/project")
    assert spec is not None
    assert spec.user == "alice"
    assert spec.host == "myhost"
    assert spec.path == "/home/alice/project"


def test_parse_ip_host() -> None:
    spec = parse_remote_spec("root@192.168.1.42:/opt/code")
    assert spec is not None
    assert spec.user == "root"
    assert spec.host == "192.168.1.42"
    assert spec.path == "/opt/code"


def test_parse_relative_path() -> None:
    spec = parse_remote_spec("deploy@server:projects/web")
    assert spec is not None
    assert spec.path == "projects/web"


def test_parse_strips_whitespace() -> None:
    spec = parse_remote_spec("  bob@host:/tmp/dir  ")
    assert spec is not None
    assert spec.user == "bob"


def test_parse_returns_none_for_local_path() -> None:
    assert parse_remote_spec("/usr/local/bin") is None


def test_parse_returns_none_for_flag() -> None:
    assert parse_remote_spec("--verbose") is None


def test_parse_returns_none_for_empty() -> None:
    assert parse_remote_spec("") is None


def test_parse_returns_none_no_colon() -> None:
    assert parse_remote_spec("user@host") is None


# ---------------------------------------------------------------------------
# is_remote_spec
# ---------------------------------------------------------------------------


def test_is_remote_spec_positive() -> None:
    assert is_remote_spec("user@host:/path") is True


def test_is_remote_spec_negative() -> None:
    assert is_remote_spec("/local/path") is False


# ---------------------------------------------------------------------------
# RemoteSpec properties
# ---------------------------------------------------------------------------


def test_ssh_target() -> None:
    spec = RemoteSpec(user="alice", host="box", path="/data")
    assert spec.ssh_target == "alice@box"


def test_display() -> None:
    spec = RemoteSpec(user="alice", host="box", path="/data")
    assert spec.display == "alice@box:/data"


# ---------------------------------------------------------------------------
# make_local_mirror
# ---------------------------------------------------------------------------


def test_make_local_mirror_deterministic(tmp_path: Path) -> None:
    spec = RemoteSpec(user="u", host="h", path="/p")
    with patch("ai_cli.remote.Path.expanduser", return_value=tmp_path / "remote"):
        d1 = make_local_mirror(spec)
        d2 = make_local_mirror(spec)
    assert d1 == d2
    assert d1.exists()


def test_make_local_mirror_different_specs(tmp_path: Path) -> None:
    s1 = RemoteSpec(user="a", host="h", path="/p1")
    s2 = RemoteSpec(user="a", host="h", path="/p2")
    with patch("ai_cli.remote.Path.expanduser", return_value=tmp_path / "remote"):
        d1 = make_local_mirror(s1)
        d2 = make_local_mirror(s2)
    assert d1 != d2


# ---------------------------------------------------------------------------
# extract_launch_cwd integration
# ---------------------------------------------------------------------------


def test_extract_launch_cwd_remote_spec() -> None:
    from ai_cli.main_helpers import extract_launch_cwd

    cwd, remaining, remote = extract_launch_cwd(["bob@server:/code", "--flag"])
    assert cwd is None
    assert remaining == ["--flag"]
    assert remote is not None
    assert remote.user == "bob"
    assert remote.host == "server"
    assert remote.path == "/code"


def test_extract_launch_cwd_local_dir(tmp_path: Path) -> None:
    from ai_cli.main_helpers import extract_launch_cwd

    d = tmp_path / "mydir"
    d.mkdir()
    cwd, remaining, remote = extract_launch_cwd([str(d), "--flag"])
    assert cwd == d.resolve()
    assert remaining == ["--flag"]
    assert remote is None


def test_extract_launch_cwd_empty() -> None:
    from ai_cli.main_helpers import extract_launch_cwd

    cwd, remaining, remote = extract_launch_cwd([])
    assert cwd is None
    assert remaining == []
    assert remote is None


def test_remote_runner_strips_ai_cli_wrapper_path(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("ai_cli.remote._find_remote_tty_wrapper", lambda: "/tmp/remote-tty-wrapper")
    monkeypatch.setattr("ai_cli.remote.subprocess.run", _run)

    runner = RemoteSessionRunner(RemoteSpec(user="alice", host="box", path="/repo"))
    runner.run_attached(
        command="codex",
        init_cmd="cd /repo",
        home_dir="/home/alice/.ai-cli/remote-sessions/codex-abc123",
        real_home="/home/alice",
        launch_path="/home/alice/.nvm/versions/node/v22.16.0/bin:/usr/bin:/bin",
    )

    remote_cmd = calls[0][-1]
    assert "export REAL_HOME=/home/alice" in remote_cmd
    assert "export PATH=/home/alice/.nvm/versions/node/v22.16.0/bin:/usr/bin:/bin" in remote_cmd
    assert "export ZDOTDIR=/home/alice/.ai-cli/remote-sessions/codex-abc123" in remote_cmd
    assert "env HOME=/home/alice/.ai-cli/remote-sessions/codex-abc123" in remote_cmd
    assert "tmux -f /home/alice/.ai-cli/remote-sessions/codex-abc123/.tmux.conf" in remote_cmd
    assert "BASH_ENV=/home/alice/.ai-cli/remote-sessions/codex-abc123/.bash_env" in remote_cmd
    assert "unalias codex claude gemini copilot" in remote_cmd


def test_resolve_remote_tool_env_parses_probe_output(monkeypatch) -> None:
    def _run(cmd, **kwargs):
        return MagicMock(
            returncode=0,
            stdout=(
                "AI_CLI_REMOTE_PATH=/opt/node/bin:/usr/bin:/bin\n"
                "AI_CLI_REMOTE_BIN=/opt/node/bin/gemini\n"
            ),
            stderr="",
        )

    monkeypatch.setattr("ai_cli.remote.subprocess.run", _run)

    resolved_bin, resolved_path = resolve_remote_tool_env(
        RemoteSpec(user="alice", host="box", path="/repo"),
        "gemini",
        real_home="/home/alice",
    )

    assert resolved_bin == "/opt/node/bin/gemini"
    assert resolved_path == "/opt/node/bin:/usr/bin:/bin"


def test_remote_runner_exec_attached_bootstraps_packaged_home(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr("ai_cli.remote._find_remote_tty_wrapper", lambda: "/tmp/remote-tty-wrapper")
    monkeypatch.setattr("ai_cli.remote.subprocess.run", _run)

    runner = RemoteSessionRunner(RemoteSpec(user="alice", host="box", path="/repo"))
    runner.exec_attached(
        command="/home/alice/.ai-cli/remote-sessions/codex-abc123/.ai-cli/bin/ai-mux --config /home/alice/.ai-cli/remote-sessions/codex-abc123/.ai-cli/ai-mux.json",
        home_dir="/home/alice/.ai-cli/remote-sessions/codex-abc123",
        real_home="/home/alice",
        launch_path="/opt/node/bin:/usr/bin:/bin",
    )

    remote_cmd = calls[0][-1]
    assert "export HOME=/home/alice/.ai-cli/remote-sessions/codex-abc123" in remote_cmd
    assert "export PATH=/opt/node/bin:/usr/bin:/bin" in remote_cmd
    assert "/home/alice/.ai-cli/remote-sessions/codex-abc123/.ai-cli/bin/ai-mux --config /home/alice/.ai-cli/remote-sessions/codex-abc123/.ai-cli/ai-mux.json" in remote_cmd

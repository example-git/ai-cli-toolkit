from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_cli import main
from ai_cli import remote as remote_mod


def test_run_tool_remote_detached_session_defers_sync_up(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force rsync mode (session mode is now the default for remote specs)
    monkeypatch.setenv("AI_CLI_REMOTE_RSYNC", "1")

    fake_tool = tmp_path / "fake_tool.sh"
    fake_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_tool.chmod(0o755)

    fake_mux = tmp_path / "fake_mux.sh"
    fake_mux.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_mux.chmod(0o755)

    spec = SimpleNamespace(
        fallback_port=9999,
        target_path="/v1/fake",
        extra_env={},
        install_command=None,
        app_binary=None,
        resolve_binary=lambda _configured: str(fake_tool),
        detect_installed=lambda _configured: True,
        addon_path=lambda: str(tmp_path / "fake_addon.py"),
    )

    monkeypatch.setattr(main, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(main, "_find_reusable_tmux_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_find_ai_mux", lambda: str(fake_mux))
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        main,
        "ensure_mitmdump",
        lambda _log_path: (_ for _ in ()).throw(RuntimeError("proxy boot failed")),
    )
    monkeypatch.setattr(main.subprocess, "call", lambda *args, **kwargs: 0)

    local_mirror = tmp_path / "mirror"
    local_mirror.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(remote_mod, "verify_ssh", lambda _spec: None)
    monkeypatch.setattr(remote_mod, "make_local_mirror", lambda _spec: local_mirror)
    monkeypatch.setattr(remote_mod, "sync_down", lambda _spec, _local: None)

    sync_up_calls: list[Path] = []

    def _sync_up(_spec, local_dir: Path) -> None:
        sync_up_calls.append(local_dir)

    monkeypatch.setattr(remote_mod, "sync_up", _sync_up)

    rc = main.run_tool("gemini", ["alice@server:/repo"])

    assert rc == 0
    assert sync_up_calls == []


def test_run_tool_remote_uses_remote_path_for_startup_context(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AI_CLI_REMOTE_RSYNC", "1")

    fake_tool = tmp_path / "fake_tool.sh"
    fake_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_tool.chmod(0o755)

    spec = SimpleNamespace(
        fallback_port=9999,
        target_path="/v1/fake",
        extra_env={},
        install_command=None,
        app_binary=None,
        resolve_binary=lambda _configured: str(fake_tool),
        detect_installed=lambda _configured: True,
        addon_path=lambda: str(tmp_path / "fake_addon.py"),
    )

    monkeypatch.setattr(main, "load_registry", lambda: {"codex": spec})
    monkeypatch.setattr(main, "_find_reusable_tmux_session", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main,
        "ensure_mitmdump",
        lambda _log_path: (_ for _ in ()).throw(RuntimeError("proxy boot failed")),
    )
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: False)

    local_mirror = tmp_path / "mirror"
    local_mirror.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(remote_mod, "verify_ssh", lambda _spec: None)
    monkeypatch.setattr(remote_mod, "make_local_mirror", lambda _spec: local_mirror)
    monkeypatch.setattr(remote_mod, "sync_down", lambda _spec, _local: None)
    monkeypatch.setattr(remote_mod, "sync_up", lambda _spec, _local: None)

    seen_cwds: list[str] = []

    def _recent_context(
        cwd: str,
        max_messages: int = 8,
        max_sessions: int = 6,
        remote_host: str = "",
    ) -> str:
        seen_cwds.append(f"{cwd}|{remote_host}")
        return ""

    monkeypatch.setattr(main, "build_recent_context_for_cwd", _recent_context)

    rc = main.run_tool("codex", ["example@192.168.1.117:/home/example/bot-refactor"])

    assert rc == 0
    assert seen_cwds == ["/home/example/bot-refactor|192.168.1.117"]

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ai_cli import prompt_editor_launcher as launcher


def test_open_editor_window_skips_duplicate_and_selects_existing(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "system.txt"
    target.write_text("", encoding="utf-8")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    monkeypatch.setattr(launcher, "_lock_dir", lambda: lock_dir)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == 4242)
    lock_path = launcher._lock_path(target)
    lock_path.write_text(
        json.dumps(
            {
                "file": str(target),
                "pid": 4242,
                "token": "abc",
                "window_name": "edit-global",
            }
        ),
        encoding="utf-8",
    )

    tmux_calls: list[tuple[str | None, tuple[str, ...]]] = []

    def _tmux(socket_name, *args):
        tmux_calls.append((socket_name, args))
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(launcher, "_tmux", _tmux)

    rc = launcher.main(
        [
            "open",
            "--file",
            str(target),
            "--window-name",
            "edit-global",
            "--tmux-socket",
            "ai-mux",
        ]
    )

    assert rc == 0
    assert tmux_calls == [("ai-mux", ("select-window", "-t", "edit-global"))]


def test_open_editor_window_spawns_tmux_window(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "tool.txt"
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(launcher, "_lock_dir", lambda: lock_dir)
    monkeypatch.setattr(
        launcher, "_tmux", lambda socket_name, *args: SimpleNamespace(returncode=0, stderr="")
    )

    rc = launcher.main(
        [
            "open",
            "--file",
            str(target),
            "--window-name",
            "edit-tool",
            "--tmux-socket",
            "ai-mux",
        ]
    )

    assert rc == 0
    lock_files = list(lock_dir.glob("*.json"))
    assert len(lock_files) == 1
    payload = json.loads(lock_files[0].read_text(encoding="utf-8"))
    assert payload["window_name"] == "edit-tool"


def test_edit_file_releases_lock(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "project.txt"
    target.write_text("", encoding="utf-8")
    lock_path = tmp_path / "lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "file": str(target),
                "pid": 1,
                "token": "tok",
                "window_name": "edit-project",
            }
        ),
        encoding="utf-8",
    )

    run_calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        run_calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", _run)
    monkeypatch.setattr(launcher, "_resolve_editor", lambda: ["vi"])

    rc = launcher.main(
        [
            "edit",
            "--file",
            str(target),
            "--window-name",
            "edit-project",
            "--lock-file",
            str(lock_path),
            "--lock-token",
            "tok",
        ]
    )

    assert rc == 0
    assert run_calls == [["vi", str(target)]]
    assert not lock_path.exists()


def test_project_target_uses_tmux_pane_path_when_env_missing(monkeypatch, tmp_path: Path) -> None:
    pane_dir = tmp_path / "repo"
    pane_dir.mkdir()
    monkeypatch.delenv("AI_CLI_WORKDIR", raising=False)
    monkeypatch.delenv("AI_CLI_PROJECT_PROMPT_FILE", raising=False)
    monkeypatch.delenv("AI_CLI_REMOTE_SPEC", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%1")

    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=str(pane_dir) + "\n", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", _run)

    resolved = launcher._resolve_requested_file(None, "project")

    expected = launcher._project_prompt_path(pane_dir)
    assert resolved == expected.resolve(strict=False)

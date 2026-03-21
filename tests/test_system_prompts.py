from __future__ import annotations

import sqlite3
from pathlib import Path

from ai_cli import main as main_mod
from ai_cli import system_prompts
from ai_cli import tui


def _seed_prompt_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE prompt_history (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            cwd      TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL,
            model    TEXT NOT NULL,
            role     TEXT NOT NULL,
            content  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE system_prompts (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            provider     TEXT NOT NULL,
            model        TEXT NOT NULL,
            role         TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            content      TEXT NOT NULL,
            char_count   INTEGER NOT NULL,
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL,
            seen_count   INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        INSERT INTO prompt_history (ts, cwd, provider, model, role, content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-03-07T12:00:00Z",
            "/repo/app",
            "openai",
            "gpt-test",
            "instructions",
            "Follow the repo rules.",
        ),
    )
    conn.execute(
        """
        INSERT INTO system_prompts (
            provider, model, role, content_hash, content, char_count, first_seen, last_seen, seen_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "openai",
            "gpt-test",
            "instructions",
            "abc123",
            "Follow the repo rules.",
            len("Follow the repo rules."),
            "2026-03-07T12:00:00Z",
            "2026-03-07T12:10:00Z",
            3,
        ),
    )
    conn.commit()
    conn.close()


def test_system_prompts_plain_history_and_detail(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "system_prompts.db"
    _seed_prompt_db(db_path)

    rc = system_prompts.main(["--db", str(db_path), "--plain"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "gpt-test" in captured.out
    assert "Use 'ai-cli system prompt --detail ID' to view full content." in captured.out

    rc = system_prompts.main(["--db", str(db_path), "--plain", "--detail", "1"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "Timestamp: 2026-03-07T12:00:00Z" in captured.out
    assert "Follow the repo rules." in captured.out


def test_system_prompts_parsed_mode_and_invalid_inputs(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "system_prompts.db"
    _seed_prompt_db(db_path)

    rc = system_prompts.main(["--db", str(db_path), "--plain", "--mode", "parsed"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "gpt-test" in captured.out
    assert "3" in captured.out

    rc = system_prompts.main(
        ["--db", str(db_path), "--plain", "--mode", "parsed", "--cwd", "/repo"]
    )
    captured = capsys.readouterr()

    assert rc == 1
    assert "--cwd is only supported in history mode." in captured.err

    rc = system_prompts.main(["--db", str(db_path), "--plain", "--detail", "999"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "No system prompt row with id=999" in captured.err


def test_system_prompts_missing_db_returns_error(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "missing.db"

    rc = system_prompts.main(["--db", str(db_path), "--plain"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "No system prompts captured yet." in captured.err


def test_main_system_dispatches_to_browser(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        system_prompts,
        "main",
        lambda argv=None: calls.append(list(argv or [])) or 0,
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["ai-cli", "system"])

    rc = main_mod.main()

    assert rc == 0
    assert calls == [[]]


def test_main_system_prompt_alias_passes_query(monkeypatch) -> None:
    calls: list[list[str]] = []

    monkeypatch.setattr(
        system_prompts,
        "main",
        lambda argv=None: calls.append(list(argv or [])) or 0,
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["ai-cli", "system", "prompt", "gpt-test"])

    rc = main_mod.main()

    assert rc == 0
    assert calls == [["gpt-test"]]


def test_main_system_edit_alias_still_uses_prompt_edit(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        main_mod,
        "_cmd_prompt_edit",
        lambda scope, tool_arg="": calls.append((scope, tool_arg)) or 0,
    )
    monkeypatch.setattr(main_mod.sys, "argv", ["ai-cli", "system", "edit", "tool", "codex"])

    rc = main_mod.main()

    assert rc == 0
    assert calls == [("tool", "codex")]


def test_tui_browse_system_prompts_launches_browser(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(tui.sys, "executable", "/usr/bin/python3")

    def _call(cmd, env=None):
        captured["cmd"] = cmd
        captured["env"] = env
        return 0

    monkeypatch.setattr(tui.subprocess, "call", _call)

    rc = tui._browse_system_prompts()

    assert rc == 0
    assert captured["cmd"] == ["/usr/bin/python3", "-m", "ai_cli", "system", "prompt"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert str(Path(tui.__file__).resolve().parent.parent) in env["PYTHONPATH"]

from __future__ import annotations

from pathlib import Path

from ai_cli import session


def test_build_recent_context_for_remote_host_uses_pulled_remote_artifacts(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        session,
        "find_session_store_db",
        lambda path="": (_ for _ in ()).throw(AssertionError("store lookup should be skipped")),
    )
    monkeypatch.setattr(
        session,
        "query_traffic_turns",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("traffic lookup should be skipped")
        ),
    )

    remote_root = (
        tmp_path
        / ".ai-cli"
        / "logs"
        / "remote-192-168-1-117"
        / ".codex-sessions"
        / "2026"
        / "03"
        / "05"
    )
    remote_root.mkdir(parents=True)
    session_file = remote_root / "rollout.jsonl"
    session_file.write_text(
        "\n".join(
            [
                '{"timestamp":"2026-03-05T22:10:44.359Z","type":"session_meta","payload":{"cwd":"/home/example/bot-refactor"}}',
                '{"timestamp":"2026-03-05T22:10:45.000Z","role":"user","message":"remote user message"}',
                '{"timestamp":"2026-03-05T22:10:46.000Z","role":"assistant","message":"remote assistant reply"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    context = session.build_recent_context_for_cwd(
        "/home/example/bot-refactor",
        remote_host="192.168.1.117",
    )

    assert "cwd=/home/example/bot-refactor" in context
    assert "- [codex] user: remote user message" in context
    assert "- [codex] assistant: remote assistant reply" in context

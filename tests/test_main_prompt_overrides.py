from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_cli import main


def test_run_tool_explicit_empty_inline_prompt_disables_file_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    fake_tool = tmp_path / "fake_tool.sh"
    fake_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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

    captured: dict[str, str | None] = {}

    def _capture_cmd(**kwargs):
        captured["instructions_file"] = kwargs["instructions_file"]
        captured["instructions_text"] = kwargs["instructions_text"]
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(main, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(main, "ensure_mitmdump", lambda _log_path: "/usr/bin/mitmdump")
    monkeypatch.setattr(main, "bootstrap_ca_cert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(main, "allocate_port", lambda _host, fallback=0: 8080)
    monkeypatch.setattr(main, "build_mitmdump_cmd", _capture_cmd)
    monkeypatch.setattr(main.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(main.sys.stdout, "isatty", lambda: False)

    rc = main.run_tool("gemini", ["--ai-cli-system-instructions-text", ""])

    assert rc == 0
    assert captured["instructions_text"] == ""
    assert captured["instructions_file"] == ""


def test_run_tool_rejects_explicit_empty_instructions_file(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    fake_tool = tmp_path / "fake_tool.sh"
    fake_tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
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

    rc = main.run_tool("gemini", ["--ai-cli-system-instructions-file", ""])

    captured = capsys.readouterr()
    assert rc == 1
    assert "--ai-cli-system-instructions-file requires a non-empty path." in captured.err

from __future__ import annotations

from pathlib import Path

from ai_cli.tools import ToolSpec
from ai_cli import update


def _copilot_spec() -> ToolSpec:
    return ToolSpec(
        name="copilot",
        display_name="GitHub Copilot CLI",
        default_binary="copilot",
        fallback_port=2225,
        target_path="/chat/completions",
        addon_script="copilot_addon.py",
        install_command="stable-command",
        install_methods={
            "stable": "stable-command",
            "prerelease": 'prerelease-command | VERSION="prerelease" bash',
        },
        preferred_methods=["stable"],
        version_command=["copilot", "--version"],
    )


def _gemini_spec(managed_binary: str) -> ToolSpec:
    return ToolSpec(
        name="gemini",
        display_name="Gemini CLI",
        default_binary=managed_binary,
        fallback_port=2226,
        target_path="/v1beta/models",
        addon_script="gemini_addon.py",
        install_command="latest-command",
        install_methods={
            "latest": 'npm install -g --prefix "$PREFIX" npm@latest && @google/gemini-cli@latest',
            "preview": "@google/gemini-cli@preview",
            "nightly": "@google/gemini-cli@nightly",
            "brew": "brew install gemini-cli",
        },
        preferred_methods=["latest", "brew"],
        version_command=["gemini", "--version"],
        managed_binary=managed_binary,
    )


def test_update_tool_copilot_dry_run_uses_stable_by_default(
    monkeypatch,
    capsys,
) -> None:
    spec = _copilot_spec()
    monkeypatch.setattr(update, "load_registry", lambda: {"copilot": spec})
    monkeypatch.setattr(update, "ensure_config", lambda: {})
    monkeypatch.setattr(update, "get_tool_config", lambda config, tool_name: {"binary": ""})
    monkeypatch.setattr(spec, "detect_installed", lambda configured_binary="": False)

    rc = update.update_tool("copilot", dry_run=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert "$ stable-command" in out


def test_update_tool_copilot_prerelease_uses_prerelease_script(
    monkeypatch,
    capsys,
) -> None:
    spec = _copilot_spec()
    monkeypatch.setattr(update, "load_registry", lambda: {"copilot": spec})
    monkeypatch.setattr(update, "ensure_config", lambda: {})
    monkeypatch.setattr(update, "get_tool_config", lambda config, tool_name: {"binary": ""})
    monkeypatch.setattr(spec, "detect_installed", lambda configured_binary="": False)

    rc = update.update_tool("copilot", dry_run=True, method="prerelease")

    out = capsys.readouterr().out
    assert rc == 0
    assert 'VERSION="prerelease" bash' in out


def test_update_tool_gemini_dry_run_uses_latest_managed_method(
    monkeypatch,
    capsys,
) -> None:
    spec = _gemini_spec("~/.ai-cli/tools/gemini/bin/gemini")
    monkeypatch.setattr(update, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(update, "ensure_config", lambda: {})
    monkeypatch.setattr(update, "get_tool_config", lambda config, tool_name: {"binary": ""})
    monkeypatch.setattr(spec, "detect_installed", lambda configured_binary="": False)

    rc = update.update_tool("gemini", dry_run=True)

    out = capsys.readouterr().out
    assert rc == 0
    assert "npm@latest" in out
    assert "@google/gemini-cli@latest" in out


def test_update_tool_gemini_nightly_uses_requested_version(
    monkeypatch,
    capsys,
) -> None:
    spec = _gemini_spec("~/.ai-cli/tools/gemini/bin/gemini")
    monkeypatch.setattr(update, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(update, "ensure_config", lambda: {})
    monkeypatch.setattr(update, "get_tool_config", lambda config, tool_name: {"binary": ""})
    monkeypatch.setattr(spec, "detect_installed", lambda configured_binary="": False)

    rc = update.update_tool("gemini", dry_run=True, method="nightly")

    out = capsys.readouterr().out
    assert rc == 0
    assert "@google/gemini-cli@nightly" in out


def test_update_tool_gemini_persists_managed_binary_after_install(
    monkeypatch,
    tmp_path: Path,
) -> None:
    managed_path = tmp_path / "gemini"
    managed_path.write_text("", encoding="utf-8")
    managed_binary = str(managed_path)
    spec = _gemini_spec(managed_binary)
    config = {"tools": {"gemini": {"binary": ""}}}
    saved: list[dict] = []

    monkeypatch.setattr(update, "load_registry", lambda: {"gemini": spec})
    monkeypatch.setattr(update, "ensure_config", lambda: config)
    monkeypatch.setattr(update, "get_tool_config", lambda config, tool_name: {"binary": ""})
    monkeypatch.setattr(update, "_run_shell", lambda command: (0, "ok"))
    monkeypatch.setattr(update, "save_config", lambda cfg: saved.append(cfg.copy()))
    monkeypatch.setattr(spec, "detect_installed", lambda configured_binary="": configured_binary == managed_binary)
    monkeypatch.setattr(spec, "get_version", lambda configured_binary="": "v1")

    rc = update.update_tool("gemini", dry_run=False, regen_completions=False)

    assert rc == 0
    assert config["tools"]["gemini"]["binary"] == managed_binary
    assert saved


def test_update_tool_gemini_rejects_unknown_method(
    monkeypatch,
    capsys,
) -> None:
    spec = _gemini_spec("~/.ai-cli/tools/gemini/bin/gemini")
    monkeypatch.setattr(update, "load_registry", lambda: {"gemini": spec})

    rc = update.update_tool("gemini", dry_run=True, method="bogus")

    err = capsys.readouterr().err
    assert rc == 1
    assert "Unknown install method 'bogus' for gemini" in err

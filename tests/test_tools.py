from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ai_cli.tools import ToolSpec, load_registry


def test_load_registry_includes_supported_tools() -> None:
    registry = load_registry()

    assert {"claude", "codex", "copilot", "gemini"}.issubset(set(registry))


def test_detect_best_method_respects_preferred_order(monkeypatch) -> None:
    spec = ToolSpec(
        name="x",
        display_name="X",
        default_binary="x",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
        install_methods={"npm": "npm install -g x", "brew": "brew install x"},
        preferred_methods=["npm", "brew"],
    )

    monkeypatch.setattr(
        "ai_cli.tools.shutil.which",
        lambda binary: "/usr/bin/brew" if binary == "brew" else None,
    )

    assert spec.detect_best_method() == "brew"


def test_get_install_command_uses_explicit_method_first() -> None:
    spec = ToolSpec(
        name="x",
        display_name="X",
        default_binary="x",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
        install_command="default",
        install_methods={"brew": "brew install x", "npm": "npm install -g x"},
    )

    assert spec.get_install_command("brew") == "brew install x"


def test_resolve_binary_expands_home(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/tester")
    spec = ToolSpec(
        name="x",
        display_name="X",
        default_binary="~/bin/x",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
    )

    assert spec.resolve_binary() == str(Path("/home/tester/bin/x"))


def test_get_version_swaps_default_binary(monkeypatch) -> None:
    seen: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="v1.2.3\n")

    monkeypatch.setattr("ai_cli.tools.subprocess.run", _fake_run)

    spec = ToolSpec(
        name="x",
        display_name="X",
        default_binary="x",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
        version_command=["x", "--version"],
    )

    version = spec.get_version("/opt/x")

    assert version == "v1.2.3"
    assert seen["cmd"][0] == "/opt/x"


def test_get_version_swaps_bare_tool_name(monkeypatch) -> None:
    """When version_command[0] is the bare tool name (not default_binary),
    it should still be replaced with the resolved configured binary."""
    seen: dict[str, list[str]] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="v9.0.0\n")

    monkeypatch.setattr("ai_cli.tools.subprocess.run", _fake_run)

    spec = ToolSpec(
        name="claude",
        display_name="Claude Code",
        default_binary="~/.local/bin/claude",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
        version_command=["claude", "--version"],
    )

    version = spec.get_version("/real/bin/claude")

    assert version == "v9.0.0"
    assert seen["cmd"][0] == "/real/bin/claude"


def test_detect_installed_ignores_alias_dir(monkeypatch, tmp_path) -> None:
    """detect_installed should not find the tool via the alias dir."""
    alias_dir = tmp_path / ".ai-cli" / "bin"
    alias_dir.mkdir(parents=True)
    fake_alias = alias_dir / "mytool"
    fake_alias.touch()
    fake_alias.chmod(0o755)

    monkeypatch.setenv("HOME", str(tmp_path))
    # Put *only* the alias dir on PATH
    monkeypatch.setenv("PATH", str(alias_dir))

    spec = ToolSpec(
        name="mytool",
        display_name="My Tool",
        default_binary="mytool",
        fallback_port=1,
        target_path="/v1",
        addon_script="x.py",
    )

    # Should NOT find the alias as a real install
    assert spec.detect_installed() is False


def test_registry_copilot_uses_script_installer_commands() -> None:
    spec = load_registry()["copilot"]

    assert "https://gh.io/copilot-install" in (spec.install_command or "")
    assert spec.get_install_command() == spec.install_methods["stable"]
    assert spec.get_install_command("prerelease") == spec.install_methods["prerelease"]
    assert 'VERSION="prerelease" bash' in spec.install_methods["prerelease"]


def test_registry_gemini_uses_managed_binary_and_versioned_installers() -> None:
    spec = load_registry()["gemini"]

    assert spec.default_binary == "~/.ai-cli/tools/gemini/bin/gemini"
    assert spec.managed_binary == spec.default_binary
    # Default install prefers npx wrapper
    assert spec.get_install_command() == spec.install_methods["npx"]
    assert "npx --yes" in spec.install_methods["npx"]
    assert "@google/gemini-cli@latest" in spec.install_methods["npx"]
    # npm-managed installs still available
    assert 'npm install -g --prefix "$PREFIX" npm@latest' in spec.install_methods["latest"]
    assert "@google/gemini-cli@preview" in spec.install_methods["preview"]
    assert "@google/gemini-cli@nightly" in spec.install_methods["nightly"]
    # npx variants for preview/nightly
    assert "@google/gemini-cli@preview" in spec.install_methods["npx-preview"]
    assert "@google/gemini-cli@nightly" in spec.install_methods["npx-nightly"]

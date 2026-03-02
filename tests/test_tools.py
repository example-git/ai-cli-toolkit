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

    def _fake_run(cmd, capture_output, text, check, timeout):
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

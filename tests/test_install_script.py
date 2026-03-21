from __future__ import annotations

from pathlib import Path


def test_install_script_copilot_methods_use_stable_and_prerelease() -> None:
    text = Path("install.sh").read_text(encoding="utf-8")

    assert '[copilot]="stable:Stable|prerelease:Prerelease"' in text
    assert "@github/copilot-cli" not in text
    assert "brew:Homebrew" not in text.split('[copilot]="', 1)[1].split('"', 1)[0]


def test_install_script_gemini_methods_and_managed_binary_support() -> None:
    text = Path("install.sh").read_text(encoding="utf-8")

    assert (
        '[gemini]="latest:Latest|preview:Preview|nightly:Nightly|brew:Homebrew|macports:MacPorts"'
        in text
    )
    assert "tool_managed_binary()" in text
    assert "npm|npx|latest|preview|nightly" in text

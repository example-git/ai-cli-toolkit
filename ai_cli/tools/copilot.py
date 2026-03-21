"""Copilot CLI tool definition."""

from ai_cli.tools import ToolSpec

_COPILOT_INSTALL_STABLE = (
    "(if command -v curl >/dev/null 2>&1; "
    "then curl -fsSL https://gh.io/copilot-install; "
    "else wget -qO- https://gh.io/copilot-install; fi) | bash"
)

_COPILOT_INSTALL_PRERELEASE = (
    "(if command -v curl >/dev/null 2>&1; "
    "then curl -fsSL https://gh.io/copilot-install; "
    'else wget -qO- https://gh.io/copilot-install; fi) | VERSION="prerelease" bash'
)

spec = ToolSpec(
    name="copilot",
    display_name="GitHub Copilot CLI",
    default_binary="copilot",
    fallback_port=2225,
    target_path="/chat/completions",
    addon_script="copilot_addon.py",
    protocol="https",
    supports_websocket=False,
    instructions_label="system",
    install_command=_COPILOT_INSTALL_STABLE,
    install_methods={
        "stable": _COPILOT_INSTALL_STABLE,
        "prerelease": _COPILOT_INSTALL_PRERELEASE,
    },
    preferred_methods=["stable"],
    version_command=["copilot", "--version"],
)

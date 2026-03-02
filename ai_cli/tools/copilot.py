"""Copilot CLI tool definition."""

from ai_cli.tools import ToolSpec

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
    install_command="npm install -g @github/copilot-cli",
    install_methods={
        "npm": "npm install -g @github/copilot-cli",
        "brew": "brew install copilot-cli",
    },
    preferred_methods=["npm", "brew"],
    version_command=["copilot", "--version"],
)

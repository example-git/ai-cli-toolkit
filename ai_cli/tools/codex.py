"""Codex tool definition."""

from ai_cli.tools import ToolSpec

spec = ToolSpec(
    name="codex",
    display_name="OpenAI Codex",
    default_binary="codex",
    fallback_port=2223,
    target_path="/backend-api/codex/responses",
    addon_script="codex_addon.py",
    protocol="https",
    supports_websocket=True,
    instructions_label="developer",
    install_command="npm install -g @openai/codex",
    install_methods={
        "npm": "npm install -g @openai/codex",
        "brew": "brew install --cask codex",
    },
    preferred_methods=["npm", "brew"],
    version_command=["codex", "--version"],
)

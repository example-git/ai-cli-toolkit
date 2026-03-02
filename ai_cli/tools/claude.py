"""Claude Code tool definition."""

from ai_cli.tools import ToolSpec

spec = ToolSpec(
    name="claude",
    display_name="Claude Code",
    default_binary="~/.local/bin/claude",
    fallback_port=2224,
    target_path="/v1/messages",
    addon_script="claude_addon.py",
    protocol="https",
    supports_websocket=True,
    instructions_label="system",
    install_command="curl -fsSL https://claude.ai/install.sh | bash",
    install_methods={
        "native": "curl -fsSL https://claude.ai/install.sh | bash",
        "brew": "brew install --cask claude-code",
        "npm": "npm install -g @anthropic-ai/claude-code",
    },
    preferred_methods=["native", "brew", "npm"],
    version_command=["claude", "--version"],
)

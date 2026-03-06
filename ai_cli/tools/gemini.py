"""Gemini CLI tool definition."""

from ai_cli.tools import ToolSpec

spec = ToolSpec(
    name="gemini",
    display_name="Gemini CLI",
    default_binary="gemini",
    fallback_port=2226,
    target_path="/v1beta/models,/v1alpha/models,/v1/models,/v1internal:",
    addon_script="gemini_addon.py",
    protocol="https",
    supports_websocket=True,
    instructions_label="system",
    install_command="npm install -g @google/gemini-cli",
    install_methods={
        "npm": "npm install -g @google/gemini-cli",
        "brew": "brew install gemini-cli",
        "macports": "sudo port install gemini-cli",
        "npx": "npx @google/gemini-cli",
        "npm-preview": "npm install -g @google/gemini-cli@preview",
        "npm-nightly": "npm install -g @google/gemini-cli@nightly",
    },
    preferred_methods=["npm", "brew", "macports"],
    version_command=["gemini", "--version"],
)

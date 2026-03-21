"""Gemini CLI tool definition."""

from ai_cli.tools import ToolSpec

_GEMINI_PREFIX = "$HOME/.ai-cli/tools/gemini"
_GEMINI_MANAGED_BINARY = "~/.ai-cli/tools/gemini/bin/gemini"


def _managed_npm_install(tag: str) -> str:
    package = f"@google/gemini-cli@{tag}"
    return (
        f'PREFIX="{_GEMINI_PREFIX}" && '
        'mkdir -p "$PREFIX" && '
        'npm install -g --prefix "$PREFIX" npm@latest && '
        '"$PREFIX/bin/npm" install -g --prefix "$PREFIX" '
        f'"{package}"'
    )


def _npx_wrapper_install(tag: str = "latest") -> str:
    """Create a wrapper script that delegates to npx @google/gemini-cli."""
    package = f"@google/gemini-cli@{tag}"
    return (
        f'PREFIX="{_GEMINI_PREFIX}" && '
        'BIN_DIR="$PREFIX/bin" && '
        'mkdir -p "$BIN_DIR" && '
        "cat > \"$BIN_DIR/gemini\" << 'WRAPPER'\n"
        "#!/usr/bin/env bash\n"
        f'exec npx --yes "{package}" "$@"\n'
        "WRAPPER\n"
        'chmod +x "$BIN_DIR/gemini"'
    )


def _link_system_binary(command: str, binary_hint: str) -> str:
    return (
        f"{command} && "
        'PREFIX="$HOME/.ai-cli/tools/gemini" && '
        'MANAGED_BIN="$PREFIX/bin/gemini" && '
        f'SYSTEM_BIN="$(command -v {binary_hint} || true)" && '
        '[ -n "$SYSTEM_BIN" ] && '
        'mkdir -p "$(dirname "$MANAGED_BIN")" && '
        'ln -sf "$SYSTEM_BIN" "$MANAGED_BIN"'
    )


spec = ToolSpec(
    name="gemini",
    display_name="Gemini CLI",
    default_binary=_GEMINI_MANAGED_BINARY,
    fallback_port=2226,
    target_path="/v1beta/models,/v1alpha/models,/v1/models,/v1internal:",
    addon_script="gemini_addon.py",
    protocol="https",
    supports_websocket=True,
    instructions_label="system",
    install_command=_npx_wrapper_install("latest"),
    install_methods={
        "npx": _npx_wrapper_install("latest"),
        "npx-preview": _npx_wrapper_install("preview"),
        "npx-nightly": _npx_wrapper_install("nightly"),
        "latest": _managed_npm_install("latest"),
        "preview": _managed_npm_install("preview"),
        "nightly": _managed_npm_install("nightly"),
        "brew": _link_system_binary("brew install gemini-cli", "gemini"),
        "macports": _link_system_binary("sudo port install gemini-cli", "gemini"),
    },
    preferred_methods=["npx", "latest", "brew", "macports"],
    version_command=["gemini", "--version"],
    managed_binary=_GEMINI_MANAGED_BINARY,
)

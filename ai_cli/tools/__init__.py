"""Tool registry: ToolSpec dataclass and dynamic registry loader.

Each supported AI CLI tool is defined as a module in this package
with a module-level `spec` attribute of type ToolSpec.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ToolSpec:
    """Definition of a supported AI CLI tool."""

    name: str
    """Short identifier: 'claude', 'codex', 'copilot', 'gemini'."""

    display_name: str
    """Human-readable name: 'Claude Code', 'OpenAI Codex', etc."""

    default_binary: str
    """Default path or name of the tool's executable."""

    fallback_port: int
    """Port to use if dynamic allocation fails."""

    target_path: str
    """API request path to match for injection (e.g. '/v1/messages')."""

    addon_script: str
    """Filename of the mitmproxy addon in ai_cli/addons/."""

    protocol: str = "https"
    """Primary protocol: 'https' or 'websocket'."""

    supports_websocket: bool = False
    """Whether the addon handles websocket_message hook."""

    instructions_label: str = "system"
    """What injection is called for this tool: 'system' or 'developer'."""

    install_command: Optional[str] = None
    """Shell command to install or update this tool (default/preferred method)."""

    install_methods: dict[str, str] = field(default_factory=dict)
    """Named install methods: {'npm': '...', 'brew': '...', 'macports': '...'}."""

    preferred_methods: list[str] = field(default_factory=list)
    """Ordered preference for install methods (first available wins)."""

    version_command: Optional[list[str]] = None
    """Command list to get the tool's version string."""

    extra_env: dict[str, str] = field(default_factory=dict)
    """Additional environment variables to set when launching the tool."""

    # Map of method names to the binary they require on PATH
    _METHOD_REQUIRES: dict[str, str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._METHOD_REQUIRES = {
            "npm": "npm",
            "npx": "npx",
            "brew": "brew",
            "macports": "port",
            "native": "curl",
            "curl": "curl",
        }

    def detect_best_method(self) -> Optional[str]:
        """Pick the first preferred method whose prerequisite binary exists."""
        order = self.preferred_methods or list(self.install_methods.keys())
        for method in order:
            req = self._METHOD_REQUIRES.get(method)
            if req is None or shutil.which(req) is not None:
                return method
        return None

    def get_install_command(self, method: Optional[str] = None) -> Optional[str]:
        """Return install command for the given method, auto-detect, or default."""
        if method and method in self.install_methods:
            return self.install_methods[method]
        if method is None and self.install_methods:
            best = self.detect_best_method()
            if best:
                return self.install_methods[best]
        return self.install_command

    def resolve_binary(self, configured_binary: str = "") -> str:
        """Return the configured or default binary path, expanded."""
        raw = configured_binary.strip() if configured_binary else self.default_binary
        return str(Path(raw).expanduser())

    def detect_installed(self, configured_binary: str = "") -> bool:
        """Check if the tool binary exists on PATH or at the configured path."""
        binary = self.resolve_binary(configured_binary)
        return shutil.which(binary) is not None

    def get_version(self, configured_binary: str = "") -> Optional[str]:
        """Run version_command and return version string, or None."""
        if not self.version_command:
            return None
        cmd = list(self.version_command)
        # Replace the first element with the resolved binary if it matches default
        if cmd and cmd[0] == self.default_binary:
            cmd[0] = self.resolve_binary(configured_binary)
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        return None

    def addon_path(self) -> str:
        """Return the full path to this tool's addon script."""
        return str(
            Path(__file__).resolve().parent.parent / "addons" / self.addon_script
        )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOL_MODULES = ["claude", "codex", "copilot", "gemini"]

# Maps binary/alias names to tool names for argv[0] dispatch
TOOL_ALIASES: dict[str, str] = {
    "claude": "claude",
    "claude-dev": "claude",
    "codex": "codex",
    "copilot": "copilot",
    "gemini": "gemini",
}


def load_registry() -> dict[str, ToolSpec]:
    """Dynamically load all tool specs from submodules."""
    registry: dict[str, ToolSpec] = {}
    for name in TOOL_MODULES:
        mod = importlib.import_module(f".{name}", package="ai_cli.tools")
        spec: ToolSpec = mod.spec  # type: ignore[attr-defined]
        registry[spec.name] = spec
    return registry

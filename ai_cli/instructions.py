"""Instructions file resolution, 5-layer composition, and editor launch.

Injection hierarchy (composed at runtime):
1. Canary rule       — e.g. "CANARY RULE: Prefix every assistant response with: DEV:"
2. Base instructions — ~/.ai-cli/base_instructions.txt  (generic gates/rules)
3. Per-tool          — ~/.ai-cli/instructions/<tool>.txt (optional)
4. Project           — ./.ai-cli/project_instructions.txt (optional, per-cwd)
5. User custom       — ~/.ai-cli/system_instructions.txt (free-form)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CANARY_RULE = "CANARY RULE: Prefix every assistant response with: DEV:"
DEFAULT_AI_CLI_DIR = "~/.ai-cli"
DEFAULT_INSTRUCTIONS_FILE = "system_instructions.txt"
BASE_INSTRUCTIONS_FILE = "base_instructions.txt"


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    """Read a text file, returning empty string on error."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _ai_cli_dir() -> Path:
    """Return the resolved ~/.ai-cli directory."""
    return Path(DEFAULT_AI_CLI_DIR).expanduser()


# ---------------------------------------------------------------------------
# Instruction resolution
# ---------------------------------------------------------------------------

def resolve_base_instructions() -> str:
    """Load the generic base instructions template.

    Looks for the shipped template first (next to this package), then
    falls back to ~/.ai-cli/base_instructions.txt.
    """
    # Shipped template (inside the ai_cli package under templates/)
    pkg_template = Path(__file__).resolve().parent.parent / "templates" / BASE_INSTRUCTIONS_FILE
    if pkg_template.is_file():
        text = _read_text(pkg_template)
        if text:
            return text

    # User-local override
    user_template = _ai_cli_dir() / BASE_INSTRUCTIONS_FILE
    return _read_text(user_template)


def resolve_tool_instructions(tool_name: str) -> str:
    """Load per-tool instruction overrides from ~/.ai-cli/instructions/<tool>.txt."""
    path = _ai_cli_dir() / "instructions" / f"{tool_name}.txt"
    return _read_text(path)


def resolve_project_instructions(project_cwd: str = "") -> str:
    """Load project-level instructions from .ai-cli/project_instructions.txt.

    If *project_cwd* is provided, that directory is used as the project root.
    Otherwise the current working directory is used.
    """
    base = Path(project_cwd).expanduser() if project_cwd.strip() else Path.cwd()
    path = base / ".ai-cli" / "project_instructions.txt"
    return _read_text(path)


def resolve_user_instructions(custom_path: str = "") -> str:
    """Load user's free-form custom instructions.

    If *custom_path* is provided, uses that. Otherwise falls back to
    ~/.ai-cli/system_instructions.txt.
    """
    if custom_path.strip():
        return _read_text(Path(custom_path.strip()).expanduser())
    return _read_text(_ai_cli_dir() / DEFAULT_INSTRUCTIONS_FILE)


def resolve_instructions_file(path_value: str = "") -> str:
    """Ensure the user instructions file exists, return its path as a string.

    Creates the file (empty) if it doesn't exist.
    """
    raw = path_value.strip()
    if raw:
        path = Path(raw).expanduser()
    else:
        path = _ai_cli_dir() / DEFAULT_INSTRUCTIONS_FILE

    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        except OSError as exc:
            raise OSError(
                f"Could not create instructions file at {path}: {exc}"
            ) from exc

    return str(path)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def compose_instructions(
    canary_rule: str = DEFAULT_CANARY_RULE,
    tool_name: str = "",
    instructions_text: str = "",
    instructions_file: str = "",
    project_cwd: str = "",
) -> str:
    """Compose the full instruction text from all 5 layers.

    If *instructions_text* is provided (inline), it replaces layer 5 (user).
    Otherwise layer 5 is loaded from *instructions_file* or the default path.

    Returns the combined text ready for injection.
    """
    layers: list[str] = []

    # Layer 1: Canary rule
    canary = canary_rule.strip()
    if canary:
        layers.append(canary)

    # Layer 2: Base instructions
    base = resolve_base_instructions()
    if base:
        layers.append(base)

    # Layer 3: Per-tool instructions
    if tool_name:
        tool_text = resolve_tool_instructions(tool_name)
        if tool_text:
            layers.append(tool_text)

    # Layer 4: Project instructions
    project_text = resolve_project_instructions(project_cwd=project_cwd)
    if project_text:
        layers.append(project_text)

    # Layer 5: User custom instructions
    if instructions_text.strip():
        layers.append(instructions_text.strip())
    else:
        user_text = resolve_user_instructions(instructions_file)
        if user_text:
            layers.append(user_text)

    return "\n\n".join(layers)


def compose_simple(base_text: str, canary_rule: str) -> str:
    """Simple 2-layer composition (canary + base). Used by addons directly."""
    base = base_text.strip()
    canary = canary_rule.strip()
    if canary and base:
        return f"{canary}\n\n{base}"
    if canary:
        return canary
    return base


def resolve_base_system_text(
    inline_text: str,
    file_path: str,
) -> tuple[str, str]:
    """Resolve the base system text from inline or file.

    Returns (source_description, text).
    """
    inline = inline_text.strip()
    if inline:
        return "inline text", inline

    raw_path = file_path.strip()
    if not raw_path:
        return "inline text", ""
    path = Path(raw_path).expanduser()
    return f"file {path}", _read_text(path)


# ---------------------------------------------------------------------------
# Editor launch
# ---------------------------------------------------------------------------

def edit_instructions(instructions_file: str = "") -> int:
    """Open the instructions file in the user's editor.

    Resolves the editor from $VISUAL, $EDITOR, or falls back to nano/vi/vim.
    Runs the editor as a child process and waits for it to exit.
    """
    path = Path(resolve_instructions_file(instructions_file))

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        for fallback in ("nano", "vi", "vim"):
            if shutil.which(fallback):
                editor = fallback
                break

    if not editor:
        import sys
        print(
            f"No editor found. Set $VISUAL or $EDITOR, or edit manually: {path}",
            file=sys.stderr,
        )
        return 1

    # Handle editors with embedded args (e.g. EDITOR="code --wait")
    parts = editor.split()
    parts.append(str(path))
    return subprocess.call(parts)

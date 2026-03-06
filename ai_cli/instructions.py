"""Instructions file resolution, 5-layer composition, and editor launch.

Injection hierarchy (composed at runtime):
1. Canary rule       — e.g. "CANARY RULE: Prefix every assistant response with: DEV:"
2. Base instructions — ~/.ai-cli/base_instructions.txt  (generic gates/rules)
3. Per-tool          — ~/.ai-cli/instructions/<tool>.txt (optional)
4. Project           — ~/.ai-cli/project-prompts/<project>/instructions.txt
5. User custom       — ~/.ai-cli/system_instructions.txt (free-form)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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
PROJECT_PROMPTS_DIR = "project-prompts"
PROJECT_PROMPT_FILENAME = "instructions.txt"
PROJECT_PROMPT_META_FILENAME = "meta.json"


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


def _shipped_base_instructions_path() -> Path:
    """Return the bundled base instructions template path."""
    return Path(__file__).resolve().parent.parent / "templates" / BASE_INSTRUCTIONS_FILE


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "project"


def _project_identity(project_cwd: str = "", remote_spec: str = "") -> tuple[str, str]:
    if remote_spec.strip():
        project_root = project_cwd.strip() or "."
        identity = f"remote:{remote_spec.strip()}::{project_root}"
        label = f"{remote_spec.strip().split(':', 1)[0]} {Path(project_root).name or 'project'}"
        return identity, label

    base = Path(project_cwd).expanduser() if project_cwd.strip() else Path.cwd()
    resolved = str(base.resolve(strict=False))
    return resolved, base.name or "project"


def resolve_project_prompt_dir(project_cwd: str = "", remote_spec: str = "") -> Path:
    identity, label = _project_identity(project_cwd=project_cwd, remote_spec=remote_spec)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return _ai_cli_dir() / PROJECT_PROMPTS_DIR / f"{_slugify(label)}-{digest}"


def resolve_project_prompt_path(project_cwd: str = "", remote_spec: str = "") -> Path:
    return resolve_project_prompt_dir(
        project_cwd=project_cwd,
        remote_spec=remote_spec,
    ) / PROJECT_PROMPT_FILENAME


def _legacy_project_instructions_path(project_cwd: str = "") -> Path:
    base = Path(project_cwd).expanduser() if project_cwd.strip() else Path.cwd()
    return base / ".ai-cli" / "project_instructions.txt"


def ensure_project_instructions_file(project_cwd: str = "", remote_spec: str = "") -> str:
    path = resolve_project_prompt_path(project_cwd=project_cwd, remote_spec=remote_spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path = _legacy_project_instructions_path(project_cwd=project_cwd)
    if not path.exists():
        if not remote_spec.strip() and legacy_path.is_file():
            shutil.copy2(legacy_path, path)
        else:
            path.write_text("", encoding="utf-8")

    meta_path = path.parent / PROJECT_PROMPT_META_FILENAME
    identity, _label = _project_identity(project_cwd=project_cwd, remote_spec=remote_spec)
    payload = {
        "identity": identity,
        "instructions_file": str(path),
        "project_cwd": project_cwd.strip() or str(Path.cwd()),
        "remote_spec": remote_spec.strip(),
    }
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# Instruction resolution
# ---------------------------------------------------------------------------

def resolve_base_instructions() -> str:
    """Load the generic base instructions template.

    Prefers ~/.ai-cli/base_instructions.txt so user edits stay out of the repo.
    Falls back to the shipped template if the user file is missing or empty.
    """
    user_template = _ai_cli_dir() / BASE_INSTRUCTIONS_FILE
    user_text = _read_text(user_template)
    if user_text:
        return user_text

    return _read_text(_shipped_base_instructions_path())


def resolve_base_instructions_path() -> Path:
    """Return the active base instructions file path."""
    user_template = _ai_cli_dir() / BASE_INSTRUCTIONS_FILE
    if _read_text(user_template):
        return user_template

    pkg_template = _shipped_base_instructions_path()
    if _read_text(pkg_template):
        return pkg_template

    return user_template


def resolve_tool_instructions(tool_name: str) -> str:
    """Load per-tool instruction overrides from ~/.ai-cli/instructions/<tool>.txt."""
    path = _ai_cli_dir() / "instructions" / f"{tool_name}.txt"
    return _read_text(path)


def resolve_project_instructions(project_cwd: str = "", remote_spec: str = "") -> str:
    """Load project-level instructions from ~/.ai-cli/project-prompts."""
    path = resolve_project_prompt_path(project_cwd=project_cwd, remote_spec=remote_spec)
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
    instructions_text: str | None = None,
    instructions_file: str = "",
    project_cwd: str = "",
    remote_spec: str = "",
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
    project_text = resolve_project_instructions(
        project_cwd=project_cwd,
        remote_spec=remote_spec,
    )
    if project_text:
        layers.append(project_text)

    # Layer 5: User custom instructions
    if instructions_text is not None:
        inline_text = instructions_text.strip()
        if inline_text:
            layers.append(inline_text)
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

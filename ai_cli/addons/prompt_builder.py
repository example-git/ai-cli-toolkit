"""Shared prompt builder for all tool addons.

Reads the latest text from each instruction source and composes
a single layered prompt string with <TAG> sections.  Re-reads files
on every call so edits take effect in real time.

Loaded alongside tool-specific addons by mitmproxy — requires
mitmproxy ctx to be available at call time.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure sibling modules are importable when loaded by mitmproxy.
_addon_dir = str(Path(__file__).resolve().parent)
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)


def _get_ctx():
    """Lazy accessor for mitmproxy ctx — allows tests to monkeypatch."""
    from mitmproxy import ctx  # type: ignore[import-untyped]
    return ctx


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def log(path_value: str, message: str) -> None:
    if not path_value:
        return
    try:
        p = Path(path_value).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


def section(tag: str, text: str) -> str:
    body = (text or "").strip()
    if not body:
        return ""
    return f"<{tag}>\n{body}\n</{tag}>"


def load_option(name: str) -> str:
    return (getattr(_get_ctx().options, name, "") or "").strip()


def load_bool_option(name: str) -> bool:
    return bool(getattr(_get_ctx().options, name, False))


def load_layer(
    text_option: str,
    file_option: str,
    explicit_text_option: str = "",
) -> str:
    """Load a prompt layer, preferring files unless inline text was explicit."""
    inline_text = load_option(text_option)
    if explicit_text_option and load_bool_option(explicit_text_option):
        return inline_text
    if inline_text:
        path_value = load_option(file_option)
        if not path_value:
            return inline_text
    path_value = load_option(file_option)
    if path_value:
        return read_text_file(Path(path_value).expanduser())
    return ""


# ---------------------------------------------------------------------------
# Section tags managed by the prompt builder
# ---------------------------------------------------------------------------

ALL_INJECTED_TAGS = (
    "CANARY GUIDELINES",
    "GLOBAL GUIDELINES",
    "BASE GUIDELINES",
    "PROJECT GUIDELINES",
    "DEVELOPER GUIDELINES",
    "DEVELOPER PROMPT",
    "RECURRING MODEL PROMPT",
    "RECURRING PERMISSIONS",
    "RECURRING APPS",
    "RECURRING COLLABORATION MODE",
)


def strip_injected_sections(text: str) -> str:
    """Remove all prompt-builder-injected <TAG> blocks from *text*."""
    stripped = text or ""
    for tag in ALL_INJECTED_TAGS:
        stripped = re.sub(
            rf"(?is)<{re.escape(tag)}>.*?</{re.escape(tag)}>",
            "",
            stripped,
        )
    return stripped.strip()


# ---------------------------------------------------------------------------
# Option registration
# ---------------------------------------------------------------------------

def register_prompt_options(loader: Any) -> None:
    """Register the common instruction options shared by all tool addons."""
    loader.add_option("system_instructions_file", str, "",
                      "Path to system instructions text file.")
    loader.add_option("system_instructions_text", str, "",
                      "Literal system instructions text.")
    loader.add_option("system_instructions_text_explicit", bool, False,
                      "Whether literal system instructions text was explicitly provided.")
    loader.add_option("base_instructions_text", str, "",
                      "Literal base instruction layer text.")
    loader.add_option("base_instructions_file", str, "",
                      "Path to base instruction layer file.")
    loader.add_option("base_instructions_text_explicit", bool, False,
                      "Whether literal base instruction text was explicitly provided.")
    loader.add_option("project_instructions_text", str, "",
                      "Literal project instruction layer text.")
    loader.add_option("project_instructions_file", str, "",
                      "Path to project instruction layer file.")
    loader.add_option("project_instructions_text_explicit", bool, False,
                      "Whether literal project instruction text was explicitly provided.")
    loader.add_option("tool_instructions_text", str, "",
                      "Literal tool-specific instructions text.")
    loader.add_option("tool_instructions_file", str, "",
                      "Path to tool-specific instructions file.")
    loader.add_option("tool_instructions_text_explicit", bool, False,
                      "Whether literal tool instruction text was explicitly provided.")
    loader.add_option("canary_rule", str, "",
                      "Canary instruction prepended before system instructions.")


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_guidelines_text(*, developer_prompt: str | None = None) -> str:
    """Build the full layered guidelines string from current option values.

    Sections: CANARY → GLOBAL → BASE → PROJECT → DEVELOPER.
    Files are re-read on every call so edits take effect in real time.

    If *developer_prompt* is provided it overrides the tool_instructions
    options (used by addons with custom developer-prompt fallback logic).
    """
    canary = load_option("canary_rule")
    global_gl = load_layer(
        "system_instructions_text",
        "system_instructions_file",
        "system_instructions_text_explicit",
    )
    base = load_layer(
        "base_instructions_text",
        "base_instructions_file",
        "base_instructions_text_explicit",
    )
    project = load_layer(
        "project_instructions_text",
        "project_instructions_file",
        "project_instructions_text_explicit",
    )
    if developer_prompt is None:
        developer_prompt = load_layer(
            "tool_instructions_text",
            "tool_instructions_file",
            "tool_instructions_text_explicit",
        )

    blocks: list[str] = []
    for tag, value in (
        ("CANARY GUIDELINES", canary),
        ("GLOBAL GUIDELINES", global_gl),
        ("BASE GUIDELINES", base),
        ("PROJECT GUIDELINES", project),
        ("DEVELOPER GUIDELINES", developer_prompt),
    ):
        block = section(tag, value)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks).strip()

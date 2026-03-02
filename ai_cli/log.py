"""Shared timestamped append-logging for ai-cli.

Extracted from claude-dev.py logging utilities. All modules use these
functions for consistent, file-based logging with ISO timestamps.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path


def append_log(path: Path, message: str) -> None:
    """Append a timestamped log line to *path*, creating parent dirs if needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n"
            )
    except OSError as exc:
        print(f"ai-cli: logging failed at {path}: {exc}", file=sys.stderr)


def append_log_str(path_value: str, message: str) -> None:
    """Convenience wrapper: skip if *path_value* is empty, else expand and log."""
    if not path_value:
        return
    append_log(Path(path_value).expanduser(), message)


def tail_text(text: str, lines: int = 60) -> str:
    """Return the last *lines* lines of *text*."""
    stripped = text.strip()
    if not stripped:
        return ""
    parts = stripped.splitlines()
    return "\n".join(parts[-lines:])


def tail_file(path: Path, lines: int = 60) -> str:
    """Read *path* and return its last *lines* lines."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return tail_text(text, lines=lines)


def fmt_cmd(cmd: list[str]) -> str:
    """Format a command list as a single shell-like string for log display."""
    return " ".join(cmd)

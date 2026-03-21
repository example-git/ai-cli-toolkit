"""Retention housekeeping for runtime artifacts."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_cli.log import append_log


def prune_old_logs(log_dir: Path, max_age_days: int, log_path: Path | None = None) -> int:
    """Delete log files older than *max_age_days* from a log directory."""
    if max_age_days < 1 or not log_dir.is_dir():
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    removed = 0
    for path in log_dir.glob("*.log"):
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue

    if removed and log_path is not None:
        append_log(log_path, f"Retention: pruned {removed} old log files (>{max_age_days}d)")
    return removed


def prune_old_traffic_rows(db_path: Path, max_age_days: int, log_path: Path | None = None) -> int:
    """Delete traffic rows older than *max_age_days* from the SQLite DB."""
    if max_age_days < 1 or not db_path.is_file():
        return 0

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.execute("DELETE FROM traffic WHERE ts < ?", (cutoff,))
        removed = cur.rowcount if cur.rowcount >= 0 else 0
        conn.commit()
        conn.close()
    except sqlite3.Error:
        return 0

    if removed and log_path is not None:
        append_log(log_path, f"Retention: pruned {removed} traffic rows (>{max_age_days}d)")
    return removed

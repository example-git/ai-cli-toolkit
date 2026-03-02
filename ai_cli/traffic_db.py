"""Database helpers for traffic viewer."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path.home() / ".ai-cli" / "traffic.db"
SORT_MODES = ("time", "domain", "request", "provider")
HAS_CALLER_COL = True
HAS_PORT_COL = True


def connect(db_path: Path) -> sqlite3.Connection:
    """Open the traffic DB and run best-effort schema migration."""
    if not db_path.is_file():
        print(f"No traffic database found at {db_path}", file=sys.stderr)
        print("Traffic is recorded when you use ai-cli to launch a tool.", file=sys.stderr)
        raise SystemExit(0)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _column_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(traffic)").fetchall()
    return {str(r[1]) for r in rows if len(r) > 1}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Best-effort migration for older traffic DB schemas."""
    global HAS_CALLER_COL, HAS_PORT_COL
    cols = _column_names(conn)
    if not cols:
        HAS_CALLER_COL = False
        HAS_PORT_COL = False
        return
    if "port" not in cols:
        try:
            conn.execute("ALTER TABLE traffic ADD COLUMN port INTEGER")
        except sqlite3.OperationalError:
            pass
    if "caller" not in cols:
        try:
            conn.execute("ALTER TABLE traffic ADD COLUMN caller TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_caller ON traffic(caller)")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    cols = _column_names(conn)
    HAS_CALLER_COL = "caller" in cols
    HAS_PORT_COL = "port" in cols


def build_query(
    caller: str = "",
    host: str = "",
    search: str = "",
    provider: str = "",
    api_only: bool = False,
    sort: str = "time",
    limit: int = 100,
) -> tuple[str, list[Any]]:
    """Build SQL query with filters."""
    conditions: list[str] = []
    params: list[Any] = []

    if caller:
        if HAS_CALLER_COL:
            conditions.append("caller = ?")
            params.append(caller)
        else:
            conditions.append("1 = 0")
    if host:
        conditions.append("host LIKE ?")
        params.append(f"%{host}%")
    if provider:
        conditions.append("provider = ?")
        params.append(provider)
    if search:
        like = f"%{search}%"
        conditions.append(
            "("
            "req_body LIKE ? OR resp_body LIKE ? OR path LIKE ? OR host LIKE ? "
            "OR provider LIKE ? OR method LIKE ? OR CAST(id AS TEXT) LIKE ?"
            ")"
        )
        params.extend([like, like, like, like, like, like, like])
    if api_only:
        conditions.append("is_api = 1")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if sort == "domain":
        order = "host ASC, path ASC, ts DESC"
    elif sort == "request":
        order = "id DESC"
    elif sort == "provider":
        order = "provider ASC, host ASC, ts DESC"
    else:
        order = "ts DESC"

    caller_sel = "caller" if HAS_CALLER_COL else "'' AS caller"
    port_sel = "port" if HAS_PORT_COL else "NULL AS port"
    query = (
        f"SELECT id, ts, {caller_sel}, method, scheme, host, {port_sel}, path, "
        "provider, is_api, status, req_bytes, resp_bytes, "
        "req_body, resp_body "
        f"FROM traffic {where} ORDER BY {order} LIMIT ?"
    )
    params.append(limit)
    return query, params

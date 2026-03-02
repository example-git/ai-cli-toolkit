"""Copilot session-store SQLite access helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Default location of the Copilot CLI session store database.
SESSION_STORE_PATHS = (
    Path.home() / ".copilot" / "session-store.db",
)


def _normalize_cwd(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def find_session_store_db(path: str = "") -> Path | None:
    """Locate the Copilot CLI session store database."""
    if path:
        p = Path(path).expanduser()
        if p.is_file():
            return p
    for candidate in SESSION_STORE_PATHS:
        if candidate.is_file():
            return candidate
    return None


def _connect_store(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection to the session store."""
    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class StoreSession:
    """A session record from the session store database."""

    id: str
    cwd: str
    repository: str
    branch: str
    summary: str
    created_at: str
    updated_at: str


def list_store_sessions(
    db_path: Path,
    cwd: str = "",
    branch: str = "",
    limit: int = 50,
) -> list[StoreSession]:
    """List sessions from the store, optionally filtered by cwd or branch."""
    conn = _connect_store(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if cwd:
            norm = _normalize_cwd(cwd)
            clauses.append("(cwd = ? OR cwd LIKE ?)")
            params.extend([norm, norm + "/%"])
        if branch:
            clauses.append("branch = ?")
            params.append(branch)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = (
            "SELECT id, cwd, repository, branch, summary, created_at, updated_at "
            f"FROM sessions{where} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(query, params).fetchall()
        return [
            StoreSession(
                id=r["id"],
                cwd=r["cwd"] or "",
                repository=r["repository"] or "",
                branch=r["branch"] or "",
                summary=r["summary"] or "",
                created_at=r["created_at"] or "",
                updated_at=r["updated_at"] or "",
            )
            for r in rows
        ]
    finally:
        conn.close()


def query_store_turns(
    db_path: Path,
    session_id: str = "",
    grep: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Retrieve conversation turns from the session store."""
    conn = _connect_store(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("t.session_id = ?")
            params.append(session_id)
        if grep:
            clauses.append("(t.user_message LIKE ? OR t.assistant_response LIKE ?)")
            needle = f"%{grep}%"
            params.extend([needle, needle])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = (
            "SELECT t.session_id, t.turn_index, t.user_message, t.assistant_response, "
            "t.timestamp, s.cwd, s.branch "
            "FROM turns t JOIN sessions s ON t.session_id = s.id"
            f"{where} ORDER BY t.timestamp, t.turn_index LIMIT ?"
        )
        params.append(limit)
        rows = conn.execute(query, params).fetchall()

        messages: list[dict[str, Any]] = []
        for r in rows:
            sid = r["session_id"]
            ts = r["timestamp"] or ""
            if r["user_message"]:
                messages.append({
                    "agent": "copilot",
                    "role": "user",
                    "type": "text",
                    "content": r["user_message"],
                    "line": r["turn_index"],
                    "timestamp": ts,
                    "file": f"session-store:{sid}",
                    "session_id": sid,
                })
            if r["assistant_response"]:
                messages.append({
                    "agent": "copilot",
                    "role": "assistant",
                    "type": "text",
                    "content": r["assistant_response"],
                    "line": r["turn_index"],
                    "timestamp": ts,
                    "file": f"session-store:{sid}",
                    "session_id": sid,
                })
        return messages
    finally:
        conn.close()


def search_store(
    db_path: Path,
    query: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """Full-text search across the session store using FTS5."""
    conn = _connect_store(db_path)
    try:
        rows = conn.execute(
            "SELECT content, session_id, source_type, source_id "
            "FROM search_index WHERE search_index MATCH ? "
            "ORDER BY rank LIMIT ?",
            [query, limit],
        ).fetchall()

        results: list[dict[str, Any]] = []
        for r in rows:
            results.append({
                "agent": "copilot",
                "role": "assistant",
                "type": "text",
                "content": r["content"][:2000] if r["content"] else "",
                "line": 0,
                "timestamp": "",
                "file": f"session-store:{r['session_id']}",
                "session_id": r["session_id"],
                "source_type": r["source_type"],
            })
        return results
    finally:
        conn.close()


def query_store_checkpoints(
    db_path: Path,
    session_id: str,
) -> list[dict[str, Any]]:
    """Retrieve checkpoints for a session from the store."""
    conn = _connect_store(db_path)
    try:
        rows = conn.execute(
            "SELECT checkpoint_number, title, overview, work_done, next_steps "
            "FROM checkpoints WHERE session_id = ? ORDER BY checkpoint_number",
            [session_id],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_store_files(
    db_path: Path,
    session_id: str = "",
    file_pattern: str = "",
) -> list[dict[str, Any]]:
    """Retrieve file records from the session store."""
    conn = _connect_store(db_path)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if session_id:
            clauses.append("sf.session_id = ?")
            params.append(session_id)
        if file_pattern:
            clauses.append("sf.file_path LIKE ?")
            params.append(f"%{file_pattern}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = conn.execute(
            "SELECT sf.session_id, sf.file_path, sf.tool_name, sf.first_seen_at "
            f"FROM session_files sf{where} ORDER BY sf.first_seen_at DESC LIMIT 100",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

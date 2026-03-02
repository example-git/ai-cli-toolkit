"""Multi-agent session extractor for Claude, Codex, Copilot, and Gemini.

This module expands the Claude-only extractor in reference/extract_session.py into
unified discovery and parsing across multiple agent session stores.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from ai_cli.session_store import (
    StoreSession,
    find_session_store_db as _find_session_store_db,
    list_store_sessions as _list_store_sessions_sql,
    query_store_checkpoints as _query_store_checkpoints_sql,
    query_store_files as _query_store_files_sql,
    query_store_turns as _query_store_turns_sql,
    search_store as _search_store_sql,
)


AGENTS = ("claude", "codex", "copilot", "gemini")

try:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (AttributeError, ValueError):
    pass


# ---------------------------------------------------------------------------
# Session store (SQLite) querying — the "sql way"
# ---------------------------------------------------------------------------

def find_session_store_db(path: str = "") -> Optional[Path]:
    return _find_session_store_db(path)


def list_store_sessions(
    db_path: Path,
    cwd: str = "",
    branch: str = "",
    limit: int = 50,
) -> list[StoreSession]:
    return _list_store_sessions_sql(db_path=db_path, cwd=cwd, branch=branch, limit=limit)


def query_store_turns(
    db_path: Path,
    session_id: str = "",
    grep: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    return _query_store_turns_sql(
        db_path=db_path,
        session_id=session_id,
        grep=grep,
        limit=limit,
    )


def search_store(
    db_path: Path,
    query: str,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return _search_store_sql(db_path=db_path, query=query, limit=limit)


def query_store_checkpoints(
    db_path: Path,
    session_id: str,
) -> list[dict[str, Any]]:
    return _query_store_checkpoints_sql(db_path=db_path, session_id=session_id)


def query_store_files(
    db_path: Path,
    session_id: str = "",
    file_pattern: str = "",
) -> list[dict[str, Any]]:
    return _query_store_files_sql(
        db_path=db_path,
        session_id=session_id,
        file_pattern=file_pattern,
    )


def _list_store_sessions(db_path: Path, sessions: list[StoreSession]) -> int:
    """Print a table of session store sessions."""
    if not sessions:
        print("No sessions found in session store.", file=sys.stderr)
        return 1
    try:
        print(f"Session store: {db_path}")
        print(f"{'ID':<40} {'Branch':<12} {'Created':<20} Summary")
        print("-" * 110)
        for s in sessions:
            created = s.created_at[:19] if s.created_at else "?"
            summary = s.summary[:50] if s.summary else "(no summary)"
            print(f"{s.id:<40} {s.branch:<12} {created:<20} {summary}")
    except BrokenPipeError:
        return 0
    return 0


_CWD_PATTERNS = (
    re.compile(r'"cwd"\s*:\s*"([^"]+)"'),
    re.compile(r'"current_dir"\s*:\s*"([^"]+)"'),
    re.compile(r'"working_dir"\s*:\s*"([^"]+)"'),
    re.compile(r'"workingDirectory"\s*:\s*"([^"]+)"'),
)


@dataclass(frozen=True)
class SessionFile:
    """A discovered session JSONL file."""

    agent: str
    path: Path

    @property
    def mtime(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def _project_slug(project_path: str) -> str:
    return project_path.replace("/", "-")


def _format_size(num: int) -> str:
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}MB"
    if num >= 1_000:
        return f"{num / 1_000:.1f}KB"
    return f"{num}B"


def _candidate_roots(agent: str) -> list[Path]:
    home = Path.home()
    if agent == "claude":
        return [home / ".claude" / "projects"]
    if agent == "codex":
        return [
            home / ".codex" / "sessions",
            home / ".codex" / "projects",
            home / ".codex",
        ]
    if agent == "copilot":
        return [
            home / ".copilot" / "sessions",
            home / ".config" / "github-copilot" / "sessions",
            home / ".config" / "github-copilot",
        ]
    if agent == "gemini":
        return [
            home / ".gemini" / "sessions",
            home / ".config" / "gemini" / "sessions",
            home / ".gemini",
        ]
    return []


def _discover_agent_files(agent: str, project_path: str = "") -> list[SessionFile]:
    """Discover JSONL files for one agent.

    For Claude, an optional project path narrows discovery to the matching slug.
    For other agents, discovery scans common session roots recursively.
    """
    discovered: list[SessionFile] = []

    if agent == "claude":
        root = _candidate_roots("claude")[0]
        if not root.is_dir():
            return discovered

        if project_path:
            project = Path(project_path).expanduser()
            if project.is_dir():
                slug = _project_slug(str(project.resolve()))
                exact = root / slug
                candidates: list[Path] = []
                if exact.is_dir():
                    candidates.append(exact)
                else:
                    basename = project.name
                    for subdir in root.iterdir():
                        if subdir.is_dir() and subdir.name.endswith(basename):
                            candidates.append(subdir)

                for candidate in candidates:
                    for jsonl in candidate.glob("*.jsonl"):
                        discovered.append(SessionFile(agent="claude", path=jsonl))
                return discovered

        for jsonl in root.glob("*/*.jsonl"):
            discovered.append(SessionFile(agent="claude", path=jsonl))
        return discovered

    for base in _candidate_roots(agent):
        if not base.exists():
            continue
        if base.is_file() and base.suffix == ".jsonl":
            discovered.append(SessionFile(agent=agent, path=base))
            continue
        if base.is_dir():
            for jsonl in base.rglob("*.jsonl"):
                discovered.append(SessionFile(agent=agent, path=jsonl))

    return discovered


def infer_agent_from_path(path: Path) -> str:
    """Best-effort inference of agent type from a path."""
    text = str(path)
    if "/.claude/" in text:
        return "claude"
    if "/.codex/" in text:
        return "codex"
    if "copilot" in text:
        return "copilot"
    if "gemini" in text:
        return "gemini"
    return "claude"


def discover_sessions(target: str = "", agent: str = "all") -> list[SessionFile]:
    """Discover session files based on optional target path and agent filter."""
    target = target.strip()
    if target:
        path = Path(target).expanduser()
        if path.is_file() and path.suffix == ".jsonl":
            forced_agent = agent if agent in AGENTS else infer_agent_from_path(path)
            return [SessionFile(agent=forced_agent, path=path)]
        if path.is_dir() and any(path.glob("*.jsonl")):
            forced_agent = agent if agent in AGENTS else infer_agent_from_path(path)
            return [
                SessionFile(agent=forced_agent, path=p)
                for p in path.glob("*.jsonl")
            ]

    agents = AGENTS if agent == "all" else (agent,)
    files: list[SessionFile] = []
    for name in agents:
        files.extend(_discover_agent_files(name, project_path=target))

    seen: set[str] = set()
    deduped: list[SessionFile] = []
    for item in sorted(files, key=lambda s: s.mtime, reverse=True):
        key = str(item.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value


def _normalize_cwd(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    try:
        return str(Path(text).expanduser().resolve())
    except OSError:
        return text


def infer_session_cwd(path: Path, max_lines: int = 80) -> str:
    """Extract a declared working directory from a session file, if present."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for idx, raw in enumerate(handle):
                if idx >= max_lines:
                    break
                for pattern in _CWD_PATTERNS:
                    match = pattern.search(raw)
                    if match:
                        return _normalize_cwd(_decode_json_string(match.group(1)))
    except OSError:
        return ""
    return ""


def _cwd_matches(session_cwd: str, working_cwd: str) -> bool:
    if not session_cwd or not working_cwd:
        return False
    session_norm = _normalize_cwd(session_cwd)
    working_norm = _normalize_cwd(working_cwd)
    if not session_norm or not working_norm:
        return False
    if session_norm == working_norm:
        return True
    return session_norm.startswith(working_norm + "/")


def sessions_for_working_dir(working_cwd: str, max_files: int = 20) -> list[SessionFile]:
    """Return recent session files whose recorded cwd matches *working_cwd*."""
    working_norm = _normalize_cwd(working_cwd)
    if not working_norm:
        return []

    slug = _project_slug(working_norm)
    matched: list[SessionFile] = []
    for session in discover_sessions(agent="all"):
        if len(matched) >= max_files:
            break

        # Claude sessions encode cwd in directory slug, so this is a fast path.
        if session.agent == "claude" and slug in str(session.path.parent):
            matched.append(session)
            continue

        session_cwd = infer_session_cwd(session.path)
        if _cwd_matches(session_cwd, working_norm):
            matched.append(session)

    return matched


def _compact_for_prompt(text: str, limit: int) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _is_context_candidate(text: str) -> bool:
    cleaned = " ".join(text.split()).strip()
    if len(cleaned) < 8:
        return False
    lowered = cleaned.lower()
    if lowered.startswith("<task-notification>"):
        return False
    if "you're out of extra usage" in lowered:
        return False
    if lowered.startswith("<retrieval_status>"):
        return False
    if "agents.md instructions for" in lowered:
        return False
    if lowered.startswith("<permissions instructions>"):
        return False
    return True


def build_recent_context_for_cwd(
    working_cwd: str,
    max_messages: int = 8,
    max_sessions: int = 6,
) -> str:
    """Build an agent-agnostic recent context block for prompt injection."""

    # ── Session store summaries (SQL) ────────────────────────────────
    store_summaries: list[str] = []
    db_path = find_session_store_db()
    if db_path:
        try:
            store_sessions = list_store_sessions(
                db_path, cwd=_normalize_cwd(working_cwd), limit=max_sessions,
            )
            for ss in store_sessions:
                if ss.summary:
                    ts = ss.created_at[:19] if ss.created_at else "?"
                    store_summaries.append(
                        f"- [copilot-store {ts}] {_compact_for_prompt(ss.summary, limit=160)}"
                    )
        except Exception:
            pass

    # ── Legacy JSONL parsing ─────────────────────────────────────────
    sessions = sessions_for_working_dir(working_cwd, max_files=max_sessions * 3)

    merged: list[dict[str, Any]] = []
    if sessions:
        selected = sorted(sessions, key=lambda s: s.mtime, reverse=True)[:max_sessions]
        for session in selected:
            parsed = parse_session_file(session, show_tools=False)
            if not parsed:
                continue
            tail = parsed[-120:]
            user_msgs = [m for m in tail if str(m.get("role", "")) == "user"]
            assistant_msgs = [m for m in tail if str(m.get("role", "")) == "assistant"]
            chosen = [*user_msgs[-3:], *assistant_msgs[-2:]]
            for msg in chosen:
                if not _is_context_candidate(str(msg.get("content", ""))):
                    continue
                enriched = dict(msg)
                enriched["_session_mtime"] = session.mtime
                merged.append(enriched)

    if not merged and not store_summaries:
        return ""

    recent: list[dict[str, Any]] = []
    if merged:
        merged.sort(
            key=lambda m: (
                _timestamp_for_sorting(str(m.get("timestamp", ""))),
                float(m.get("_session_mtime", 0.0)),
                int(m.get("line", 0)),
            )
        )
        recent = merged[-max_messages:]

    lines = [
        "RECENT WORKING-DIR CONTEXT (cross-agent):",
        f"cwd={_normalize_cwd(working_cwd)}",
    ]
    if store_summaries:
        lines.append("Recent session summaries:")
        lines.extend(store_summaries[:max_sessions])

    seen_line: set[str] = set()
    for msg in recent:
        agent = str(msg.get("agent", "unknown"))
        role = str(msg.get("role", "assistant"))
        snippet = _compact_for_prompt(str(msg.get("content", "")), limit=220)
        line = f"- [{agent}] {role}: {snippet}"
        if line in seen_line:
            continue
        seen_line.add(line)
        lines.append(line)

    lines.append(
        "Use this as continuity context only; prioritize current user instructions."
    )
    return "\n".join(lines)


def _extract_text(value: Any) -> list[str]:
    """Recursively extract text-like values from nested JSON structures."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        text = value.strip()
        if text:
            out.append(text)
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_extract_text(item))
        return out
    if isinstance(value, dict):
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            text = value["text"].strip()
            if text:
                out.append(text)
        for key in (
            "text",
            "content",
            "message",
            "output_text",
            "input_text",
            "prompt",
            "response",
            "result",
        ):
            if key in value:
                out.extend(_extract_text(value.get(key)))
        return out
    out.append(str(value))
    return out


def _normalize_timestamp(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).isoformat()
        except (ValueError, OSError):
            return ""
    if isinstance(value, str):
        return value
    return ""


def _timestamp_for_sorting(value: str) -> float:
    if not value:
        return 0.0
    text = value.strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def parse_claude_jsonl(path: Path, show_tools: bool = False) -> list[dict[str, Any]]:
    """Parse Claude Code JSONL with support for tool_use/tool_result blocks."""
    messages: list[dict[str, Any]] = []

    with path.open(encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            timestamp = _normalize_timestamp(
                obj.get("timestamp")
                or obj.get("created_at")
                or obj.get("time")
            )

            entry_type = obj.get("type", "")
            msg = obj.get("message", {})
            if not msg and "data" in obj and isinstance(obj["data"], dict):
                outer = obj["data"].get("message", {})
                if isinstance(outer, dict):
                    msg = outer.get("message", outer)
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", entry_type)
            if role not in ("user", "assistant"):
                continue

            content = msg.get("content", "")
            if isinstance(content, str):
                text = content.strip()
                if text:
                    messages.append(
                        {
                            "agent": "claude",
                            "role": role,
                            "type": "text",
                            "content": text,
                            "line": lineno,
                            "timestamp": timestamp,
                            "file": str(path),
                        }
                    )
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "text":
                        text = str(block.get("text", "")).strip()
                        if text:
                            messages.append(
                                {
                                    "agent": "claude",
                                    "role": role,
                                    "type": "text",
                                    "content": text,
                                    "line": lineno,
                                    "timestamp": timestamp,
                                    "file": str(path),
                                }
                            )
                    elif btype == "tool_use" and show_tools:
                        name = str(block.get("name", "?"))
                        inp = block.get("input", {})
                        summary_parts: list[str] = []
                        if isinstance(inp, dict):
                            for key in (
                                "file_path",
                                "command",
                                "pattern",
                                "query",
                                "path",
                                "prompt",
                            ):
                                if key in inp:
                                    summary_parts.append(f"{key}={str(inp[key])[:120]}")
                        summary = (", ".join(summary_parts) if summary_parts else json.dumps(inp)[:200])
                        messages.append(
                            {
                                "agent": "claude",
                                "role": role,
                                "type": "tool_use",
                                "content": f"[TOOL: {name}] {summary}",
                                "line": lineno,
                                "timestamp": timestamp,
                                "file": str(path),
                            }
                        )
                    elif btype == "tool_result" and show_tools:
                        result = block.get("content", "")
                        parts = _extract_text(result)
                        text = " | ".join(parts)[:400]
                        if text:
                            messages.append(
                                {
                                    "agent": "claude",
                                    "role": role,
                                    "type": "tool_result",
                                    "content": f"[RESULT] {text}",
                                    "line": lineno,
                                    "timestamp": timestamp,
                                    "file": str(path),
                                }
                            )

    return messages


def parse_generic_jsonl(
    path: Path,
    agent: str,
    show_tools: bool = False,
) -> list[dict[str, Any]]:
    """Best-effort parser for non-Claude session JSONL formats.

    This handles common event records across Codex/Copilot/Gemini variants by
    inspecting top-level type/role/message/content fields.
    """
    messages: list[dict[str, Any]] = []

    with path.open(encoding="utf-8", errors="replace") as handle:
        for lineno, raw in enumerate(handle, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue

            timestamp = _normalize_timestamp(
                obj.get("timestamp")
                or obj.get("created_at")
                or obj.get("time")
                or obj.get("ts")
            )

            entry_type = str(obj.get("type", "")).lower()
            role = str(obj.get("role", "")).lower()
            payload = obj.get("payload")

            if not role:
                if entry_type in ("user", "assistant", "system"):
                    role = entry_type
                elif entry_type.startswith("user"):
                    role = "user"
                elif entry_type.startswith("assistant") or "agent" in entry_type:
                    role = "assistant"
                elif entry_type.startswith("tool"):
                    role = "assistant"

            if isinstance(payload, dict):
                payload_role = payload.get("role")
                if isinstance(payload_role, str):
                    role = payload_role.lower()

            text_parts: list[str] = []
            if isinstance(payload, dict):
                # Common format in Codex sessions: payload.type=message with payload.content blocks.
                if "content" in payload:
                    text_parts.extend(_extract_text(payload.get("content")))
                elif "message" in payload:
                    text_parts.extend(_extract_text(payload.get("message")))

            for key in (
                "content",
                "message",
                "text",
                "output",
                "input",
                "delta",
            ):
                if key in obj:
                    text_parts.extend(_extract_text(obj.get(key)))

            if not text_parts and isinstance(obj.get("data"), dict):
                text_parts.extend(_extract_text(obj.get("data")))

            text = "\n".join(part for part in text_parts if part).strip()
            if not text:
                continue

            msg_type = "text"
            if "tool" in entry_type:
                if not show_tools:
                    continue
                msg_type = "tool_use" if "result" not in entry_type else "tool_result"

            messages.append(
                {
                    "agent": agent,
                    "role": role if role in ("user", "assistant") else "assistant",
                    "type": msg_type,
                    "content": text[:2000],
                    "line": lineno,
                    "timestamp": timestamp,
                    "file": str(path),
                }
            )

    return messages


def parse_session_file(session: SessionFile, show_tools: bool = False) -> list[dict[str, Any]]:
    if session.agent == "claude":
        return parse_claude_jsonl(session.path, show_tools=show_tools)
    return parse_generic_jsonl(session.path, session.agent, show_tools=show_tools)


def format_message(message: dict[str, Any], raw: bool = False) -> str:
    role = str(message.get("role", "assistant")).upper()
    mtype = str(message.get("type", "text"))
    agent = str(message.get("agent", "unknown")).upper()
    line = int(message.get("line", 0))
    content = str(message.get("content", ""))

    if raw:
        return f"[{agent} L{line}] {role}: {content}"

    if role == "USER":
        color = "\033[36m"
    elif mtype == "tool_use":
        color = "\033[33m"
    elif mtype == "tool_result":
        color = "\033[90m"
    else:
        color = "\033[32m"
    reset = "\033[0m"

    label = f"[{agent} L{line}] {role}"
    if mtype != "text":
        label += f" ({mtype})"

    if len(content) > 2000:
        content = content[:2000] + "\n... [truncated]"

    return f"{color}{label}:{reset} {content}"


def _list_sessions(sessions: Iterable[SessionFile]) -> int:
    rows = sorted(sessions, key=lambda s: s.mtime, reverse=True)
    if not rows:
        print("No session files found.", file=sys.stderr)
        return 1

    try:
        print(f"{'Agent':<8} {'Modified':<19} {'Size':>10}  File")
        print("-" * 90)
        for session in rows:
            mtime = datetime.fromtimestamp(session.mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(
                f"{session.agent:<8} {mtime:<19} {_format_size(session.size):>10}  {session.path}"
            )
    except BrokenPipeError:
        return 0
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Browse conversation history across Claude, Codex, Copilot, and Gemini.",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="",
        help="Optional project dir or direct .jsonl file path.",
    )
    parser.add_argument(
        "--agent",
        choices=["all", *AGENTS],
        default="all",
        help="Filter to one agent (default: all).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Merge messages from all discovered sessions instead of only latest.",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List discovered session files and exit.",
    )
    parser.add_argument(
        "--grep",
        "-g",
        default="",
        help="Only show messages containing this substring (case-insensitive).",
    )
    parser.add_argument(
        "--tail",
        "-n",
        type=int,
        default=0,
        help="Only show the last N messages.",
    )
    parser.add_argument(
        "--tools",
        "-t",
        action="store_true",
        help="Include tool_use/tool_result messages where available.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Disable ANSI color output.",
    )

    # Session store (SQL) flags
    sql_group = parser.add_argument_group(
        "session store (SQL)",
        "Query the Copilot CLI session store database instead of JSONL files.",
    )
    sql_group.add_argument(
        "--sql",
        action="store_true",
        help="Use the session store database (SQL) instead of JSONL file discovery.",
    )
    sql_group.add_argument(
        "--db",
        default="",
        help="Path to session store .db file (default: ~/.copilot/session-store.db).",
    )
    sql_group.add_argument(
        "--session-id",
        default="",
        help="Show turns for a specific session ID from the store.",
    )
    sql_group.add_argument(
        "--search",
        default="",
        help="Full-text search (FTS5) across the session store.",
    )
    sql_group.add_argument(
        "--files",
        default=None,
        help="List files touched in the store, optionally filtered by pattern.",
        metavar="PATTERN",
        nargs="?",
        const="*",
    )
    sql_group.add_argument(
        "--checkpoints",
        action="store_true",
        help="Show checkpoints for the given --session-id.",
    )
    args = parser.parse_args(argv)

    # ── SQL mode ──────────────────────────────────────────────────────
    if args.sql or args.search or args.session_id or args.db or args.checkpoints or args.files is not None:
        db_path = find_session_store_db(args.db)
        if db_path is None:
            print("Session store database not found. Pass --db or ensure ~/.copilot/session-store.db exists.", file=sys.stderr)
            return 1

        # FTS5 search
        if args.search:
            results = search_store(db_path, args.search, limit=args.tail or 30)
            if not results:
                print("No search results.", file=sys.stderr)
                return 0
            print(f"Session store: {db_path}", file=sys.stderr)
            print(f"{len(results)} search result(s) for: {args.search}", file=sys.stderr)
            print("---", file=sys.stderr)
            try:
                for msg in results:
                    sid = msg.get("session_id", "?")
                    stype = msg.get("source_type", "?")
                    content = str(msg.get("content", ""))
                    if args.tail and len(content) > 300:
                        content = content[:300] + "..."
                    if args.raw:
                        print(f"[{sid[:8]} {stype}] {content}")
                    else:
                        print(f"\033[33m[{sid[:8]} {stype}]\033[0m {content}")
                    print()
            except BrokenPipeError:
                pass
            return 0

        # Checkpoints for a session
        if args.checkpoints:
            if not args.session_id:
                print("--checkpoints requires --session-id.", file=sys.stderr)
                return 1
            cps = query_store_checkpoints(db_path, args.session_id)
            if not cps:
                print("No checkpoints found.", file=sys.stderr)
                return 0
            print(f"Session store: {db_path}", file=sys.stderr)
            print(f"{len(cps)} checkpoint(s) for session {args.session_id[:12]}...", file=sys.stderr)
            print("---", file=sys.stderr)
            try:
                for cp in cps:
                    num = cp.get("checkpoint_number", "?")
                    title = cp.get("title", "(untitled)")
                    overview = cp.get("overview", "")
                    work = cp.get("work_done", "")
                    nexts = cp.get("next_steps", "")
                    print(f"── Checkpoint {num}: {title} ──")
                    if overview:
                        print(f"  Overview: {overview[:500]}")
                    if work:
                        print(f"  Work done: {work[:500]}")
                    if nexts:
                        print(f"  Next steps: {nexts[:500]}")
                    print()
            except BrokenPipeError:
                pass
            return 0

        # Files touched
        if args.files is not None:
            pattern = args.files if args.files != "*" else ""
            files = query_store_files(db_path, session_id=args.session_id, file_pattern=pattern)
            if not files:
                print("No files found.", file=sys.stderr)
                return 0
            print(f"Session store: {db_path}", file=sys.stderr)
            print(f"{'Session':<40} {'Tool':<8} {'First Seen':<20} File", file=sys.stderr)
            print("-" * 110, file=sys.stderr)
            try:
                for f in files:
                    sid = (f.get("session_id") or "?")[:36]
                    tool = f.get("tool_name") or "?"
                    seen = (f.get("first_seen_at") or "?")[:19]
                    fp = f.get("file_path") or "?"
                    print(f"{sid:<40} {tool:<8} {seen:<20} {fp}")
            except BrokenPipeError:
                pass
            return 0

        # List sessions or show turns
        if args.list or (not args.session_id):
            cwd_filter = ""
            if args.target:
                cwd_filter = _normalize_cwd(args.target)
            sessions_list = list_store_sessions(
                db_path, cwd=cwd_filter, limit=args.tail or 50,
            )
            return _list_store_sessions(db_path, sessions_list)

        # Show turns for a specific session
        messages = query_store_turns(
            db_path, session_id=args.session_id, grep=args.grep,
            limit=args.tail or 200,
        )
        if not messages:
            print("No turns found for this session.", file=sys.stderr)
            return 0

        print(f"Session store: {db_path}", file=sys.stderr)
        print(f"Showing {len(messages)} message(s) for session {args.session_id[:12]}...", file=sys.stderr)
        print("---", file=sys.stderr)
        try:
            for message in messages:
                print(format_message(message, raw=args.raw))
                print()
        except BrokenPipeError:
            pass
        return 0

    # ── Legacy mode (JSONL file discovery) ────────────────────────────
    sessions = discover_sessions(target=args.target, agent=args.agent)
    if not sessions:
        print("No sessions found for the given filters.", file=sys.stderr)
        return 1

    if args.list:
        return _list_sessions(sessions)

    chosen = sessions if args.all else [max(sessions, key=lambda s: s.mtime)]

    messages: list[dict[str, Any]] = []
    for session in chosen:
        messages.extend(parse_session_file(session, show_tools=args.tools))

    if args.all:
        messages.sort(
            key=lambda m: (
                _timestamp_for_sorting(str(m.get("timestamp", ""))),
                str(m.get("file", "")),
                int(m.get("line", 0)),
            )
        )

    if args.grep:
        needle = args.grep.lower()
        messages = [m for m in messages if needle in str(m.get("content", "")).lower()]

    if args.tail > 0:
        messages = messages[-args.tail :]

    if not messages:
        print("No messages found matching your criteria.", file=sys.stderr)
        return 0

    latest = max(chosen, key=lambda s: s.mtime)
    print(f"Session file: {latest.path}", file=sys.stderr)
    print(f"Showing {len(messages)} message(s) from {len(chosen)} session file(s).", file=sys.stderr)
    print("---", file=sys.stderr)

    try:
        for message in messages:
            print(format_message(message, raw=args.raw))
            print()
    except BrokenPipeError:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

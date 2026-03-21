"""Interactive browser for captured historical and parsed system prompts."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from ai_cli.addons.system_prompt_addon import _DEFAULT_DB_DIR, _DEFAULT_DB_NAME

_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / _DEFAULT_DB_NAME


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _validate_args(args: argparse.Namespace) -> str | None:
    if args.limit <= 0:
        return "--limit must be greater than 0."
    if args.mode == "parsed" and args.cwd:
        return "--cwd is only supported in history mode."
    return None


def _general_search_clause(mode: str, term: str) -> tuple[str, list[str]]:
    fields = ["provider", "model", "role", "content"]
    if mode == "history":
        fields.append("cwd")
    like_value = f"%{term}%"
    clause = "(" + " OR ".join(f"{field} LIKE ?" for field in fields) + ")"
    return clause, [like_value] * len(fields)


def _build_query(args: argparse.Namespace) -> tuple[str, list[object]]:
    params: list[object] = []
    if args.mode == "history":
        select = (
            "SELECT id, ts, provider, model, role, cwd, LENGTH(content) AS char_count "
            "FROM prompt_history"
        )
        order_by = "ts DESC, id DESC"
    else:
        select = (
            "SELECT id, last_seen AS ts, provider, model, role, '' AS cwd, char_count, seen_count "
            "FROM system_prompts"
        )
        order_by = "last_seen DESC, id DESC"

    clauses: list[str] = []

    if args.provider:
        clauses.append("provider LIKE ?")
        params.append(f"%{args.provider}%")
    if args.model:
        clauses.append("model LIKE ?")
        params.append(f"%{args.model}%")
    if args.role:
        clauses.append("role LIKE ?")
        params.append(f"%{args.role}%")
    if args.cwd:
        clauses.append("cwd LIKE ?")
        params.append(f"%{args.cwd}%")
    for term in filter(None, [args.query, args.search]):
        clause, search_params = _general_search_clause(args.mode, term)
        clauses.append(clause)
        params.extend(search_params)

    query = select
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += f" ORDER BY {order_by} LIMIT ?"
    params.append(args.limit)
    return query, params


def _history_headers() -> tuple[list[str], list[int]]:
    return (
        ["ID", "Timestamp", "Provider", "Model", "Role", "Chars", "CWD"],
        [6, 20, 12, 24, 18, 8, 28],
    )


def _parsed_headers() -> tuple[list[str], list[int]]:
    return (
        ["ID", "Last Seen", "Provider", "Model", "Role", "Chars", "Seen"],
        [6, 20, 12, 24, 18, 8, 6],
    )


def _trim(value: object, width: int) -> str:
    text = str(value or "")
    if len(text) <= width:
        return text
    return text[: max(width - 1, 0)] + "…"


def _print_table(rows: list[sqlite3.Row], mode: str) -> None:
    if not rows:
        print("No system prompts found matching filters.")
        return

    headers, widths = _history_headers() if mode == "history" else _parsed_headers()
    line = " ".join(f"{header:<{width}}" for header, width in zip(headers, widths, strict=True))
    print(line)
    print("-" * len(line))

    for row in rows:
        values = (
            [
                row["id"],
                row["ts"] or "",
                row["provider"],
                row["model"],
                row["role"],
                row["char_count"],
                row["cwd"] or "",
            ]
            if mode == "history"
            else [
                row["id"],
                row["ts"] or "",
                row["provider"],
                row["model"],
                row["role"],
                row["char_count"],
                row["seen_count"],
            ]
        )
        print(
            " ".join(
                f"{_trim(value, width):<{width}}"
                for value, width in zip(values, widths, strict=True)
            )
        )


def _fetch_detail(conn: sqlite3.Connection, prompt_id: int, mode: str) -> sqlite3.Row | None:
    if mode == "history":
        return conn.execute(
            "SELECT id, ts, cwd, provider, model, role, content FROM prompt_history WHERE id = ?",
            (prompt_id,),
        ).fetchone()
    return conn.execute(
        "SELECT id, first_seen, last_seen, provider, model, role, content, char_count, seen_count "
        "FROM system_prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()


def _print_detail(row: sqlite3.Row, mode: str) -> None:
    if mode == "history":
        print(f"ID:       {row['id']}")
        print(f"Timestamp:{' '}{row['ts']}")
        print(f"Provider: {row['provider']}")
        print(f"Model:    {row['model']}")
        print(f"Role:     {row['role']}")
        print(f"CWD:      {row['cwd']}")
        print("─" * 80)
        print(row["content"])
        return

    print(f"ID:       {row['id']}")
    print(f"Provider: {row['provider']}")
    print(f"Model:    {row['model']}")
    print(f"Role:     {row['role']}")
    print(f"Chars:    {row['char_count']}")
    print(f"First:    {row['first_seen']}")
    print(f"Last:     {row['last_seen']}")
    print(f"Seen:     {row['seen_count']} time(s)")
    print("─" * 80)
    print(row["content"])


def _interactive_browser(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    args: argparse.Namespace,
) -> int:
    while True:
        _print_table(rows, args.mode)
        if not rows:
            return 0
        print()
        try:
            choice = input("Enter prompt ID for detail (r to refresh, q to quit): ").strip().lower()
        except EOFError:
            return 0
        if not choice or choice == "q":
            return 0
        if choice == "r":
            query, params = _build_query(args)
            rows = conn.execute(query, params).fetchall()
            continue
        if not choice.isdigit():
            print("Invalid selection.", file=sys.stderr)
            print()
            continue
        detail = _fetch_detail(conn, int(choice), args.mode)
        if not detail:
            print(f"No system prompt row with id={choice}", file=sys.stderr)
            print()
            continue
        print()
        _print_detail(detail, args.mode)
        print()
        try:
            input("Press Enter to return to the list.")
        except EOFError:
            return 0
        print()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-cli system prompt",
        description="Browse captured historical and parsed system prompts.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Optional broad search term across provider/model/role/content/cwd.",
    )
    parser.add_argument(
        "--mode",
        choices=["history", "parsed"],
        default="history",
        help="Browse historical prompt captures or parsed unique prompts (default: history).",
    )
    parser.add_argument(
        "--provider",
        help="Filter by provider substring.",
    )
    parser.add_argument(
        "--model",
        help="Filter by model substring.",
    )
    parser.add_argument(
        "--role",
        help="Filter by role substring.",
    )
    parser.add_argument(
        "--cwd",
        help="Filter by working directory substring (history mode only).",
    )
    parser.add_argument(
        "--search",
        "-s",
        help="Additional broad search term applied alongside the positional query.",
    )
    parser.add_argument(
        "--limit",
        "-n",
        type=int,
        default=100,
        help="Maximum rows to show (default: 100).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help="Path to the system prompt capture database.",
    )
    parser.add_argument(
        "--detail",
        "-d",
        type=int,
        metavar="ID",
        help="Show full detail for a specific prompt row ID.",
    )
    parser.add_argument(
        "--no-interactive",
        "--plain",
        action="store_true",
        dest="plain",
        help="Plain text output only; do not prompt for interactive detail browsing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    error = _validate_args(args)
    if error:
        print(error, file=sys.stderr)
        return 1

    if not args.db.is_file():
        print("No system prompts captured yet.", file=sys.stderr)
        print(f"(Expected database at {args.db})", file=sys.stderr)
        return 1

    conn = _connect(args.db)
    try:
        expected_table = "prompt_history" if args.mode == "history" else "system_prompts"
        if not _table_exists(conn, expected_table):
            print(
                f"Database is missing the {expected_table!r} table needed for {args.mode} mode.",
                file=sys.stderr,
            )
            return 1

        if args.detail:
            row = _fetch_detail(conn, args.detail, args.mode)
            if not row:
                print(f"No system prompt row with id={args.detail}", file=sys.stderr)
                return 1
            _print_detail(row, args.mode)
            return 0

        query, params = _build_query(args)
        rows = conn.execute(query, params).fetchall()
        use_interactive = not args.plain and sys.stdin.isatty() and sys.stdout.isatty()
        if use_interactive:
            return _interactive_browser(conn, rows, args)

        _print_table(rows, args.mode)
        if rows:
            print("\nUse 'ai-cli system prompt --detail ID' to view full content.")
        return 0
    finally:
        conn.close()

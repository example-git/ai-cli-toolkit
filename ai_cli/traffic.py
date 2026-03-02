"""Interactive traffic request/response viewer.

Reads from ~/.ai-cli/traffic.db and provides:
- Tabular list of requests with timestamp, caller, provider, method, host, path, status
- Filter by caller tool (--caller copilot/claude/codex/gemini)
- Filter by provider (--provider)
- Search by host/address/body/path/provider/request id (--search)
- Sort by time (default newest-first), domain, request number, or provider
- Interactive detail view for request/response bodies

Usage:
    ai-cli traffic                       # list recent traffic
    ai-cli traffic --caller claude       # filter by caller
    ai-cli traffic --host anthropic      # filter by host substring
    ai-cli traffic --search "function"   # search body/path/host/provider/id
    ai-cli traffic --sort domain         # sort by host instead of time
    ai-cli traffic --sort request        # sort by request id
    ai-cli traffic --sort provider       # sort by provider name
    ai-cli traffic --api                 # only show API calls (with bodies)
    ai-cli traffic --limit 50           # show N rows (default 100)
"""

from __future__ import annotations

import argparse
import curses
import json
import re
import shutil
import sqlite3
import sys
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

try:
    import urwid
except Exception:  # pragma: no cover - optional runtime dependency
    urwid = None  # type: ignore[assignment]

from ai_cli import traffic_db as _traffic_db

_DEFAULT_DB_PATH = _traffic_db.DEFAULT_DB_PATH
_SORT_MODES = _traffic_db.SORT_MODES
_COLOR_ENABLED = False

# Caller display colors (curses color pair indices)
_CALLER_COLORS = {
    "claude": 1,
    "copilot": 2,
    "codex": 3,
    "gemini": 4,
}


def _connect(db_path: Path) -> sqlite3.Connection:
    return _traffic_db.connect(db_path)


def _build_query(
    caller: str = "",
    host: str = "",
    search: str = "",
    provider: str = "",
    api_only: bool = False,
    sort: str = "time",
    limit: int = 100,
) -> tuple[str, list[Any]]:
    return _traffic_db.build_query(
        caller=caller,
        host=host,
        search=search,
        provider=provider,
        api_only=api_only,
        sort=sort,
        limit=limit,
    )


def _format_size(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / (1024 * 1024):.1f}M"


def _format_ts(ts: str) -> str:
    """Shorten ISO timestamp to HH:MM:SS or date+time."""
    if not ts:
        return ""
    # ts is like 2026-03-01T23:55:10Z
    if "T" in ts:
        parts = ts.split("T")
        time_part = parts[1].rstrip("Z")[:8]
        date_part = parts[0][5:]  # MM-DD
        return f"{date_part} {time_part}"
    return ts[:19]


def _caller_label(caller: str) -> str:
    return caller or "?"


def _scalar_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _flatten_json_pairs(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(value, dict):
        if not value:
            pairs.append((prefix or "value", "{}"))
            return pairs
        for key, item in value.items():
            next_key = f"{prefix}.{key}" if prefix else str(key)
            pairs.extend(_flatten_json_pairs(item, next_key))
        return pairs
    if isinstance(value, list):
        if not value:
            pairs.append((prefix or "value", "[]"))
            return pairs
        for idx, item in enumerate(value):
            next_key = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            pairs.extend(_flatten_json_pairs(item, next_key))
        return pairs
    pairs.append((prefix or "value", _scalar_text(value)))
    return pairs


def _parse_sse_to_pairs(body: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    event_name = ""
    event_index = 0
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        name = event_name or f"event_{event_index}"
        event_index += 1
        event_name = ""
        try:
            obj = json.loads(payload)
            pairs.extend(_flatten_json_pairs(obj, f"sse.{name}"))
        except json.JSONDecodeError:
            pairs.append((f"sse.{name}.data", payload))
    return pairs


def _body_pairs(body: str | None) -> list[tuple[str, str]]:
    if not body:
        return [("value", "(empty)")]
    try:
        obj = json.loads(body)
        return _flatten_json_pairs(obj)
    except (json.JSONDecodeError, TypeError):
        sse_pairs = _parse_sse_to_pairs(body)
        if sse_pairs:
            return sse_pairs
        return [("raw", body)]


def _format_body_pairs_lines(body: str | None, width: int, max_lines: int) -> list[str]:
    max_width = max(24, width)
    pairs = _body_pairs(body)
    lines: list[str] = []
    truncated = False
    for key, value in pairs:
        if len(lines) >= max_lines:
            truncated = True
            break
        lines.append(f"**{key}**")
        for raw in str(value).splitlines() or [""]:
            if len(lines) >= max_lines:
                truncated = True
                break
            if not raw:
                lines.append("  ")
                continue
            wrapped = textwrap.fill(
                raw,
                width=max_width - 2,
                initial_indent="  ",
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            for part in wrapped.splitlines():
                if len(lines) >= max_lines:
                    truncated = True
                    break
                lines.append(part)
            if truncated:
                break
        if len(lines) >= max_lines:
            truncated = True
            break
        lines.append("")
    if truncated and lines:
        if len(lines) >= max_lines:
            lines[-1] = "... (truncated)"
        else:
            lines.append("... (truncated)")
    return lines or ["(empty)"]


def _markdown_line_style(line: str) -> tuple[str, bool]:
    """Return (display_text, bold) for simple markdown-ish lines."""
    stripped = line.strip()
    if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
        return stripped[2:-2], True
    return line, False


def _render_terminal_line(line: str) -> str:
    """Render markdown-ish line for plain terminal output."""
    text, bold = _markdown_line_style(line)
    if bold and sys.stdout.isatty():
        return f"\033[1m{text}\033[0m"
    return text


# ── Plain text output (non-interactive) ───────────────────────────────

def _print_table(rows: list[sqlite3.Row]) -> None:
    """Print traffic rows as a formatted table."""
    if not rows:
        print("No traffic found matching filters.")
        return

    print(
        f"{'#':<5} {'Time':<15} {'Caller':<8} {'Prov':<10} {'Method':<7} "
        f"{'Status':>6} {'Host':<24} {'Path':<28} {'Req':>6} {'Resp':>6}"
    )
    print("─" * 130)

    for r in rows:
        status_str = str(r["status"]) if r["status"] else " - "
        path_display = r["path"]
        if len(path_display) > 27:
            path_display = path_display[:24] + "..."
        host_display = r["host"]
        if len(host_display) > 23:
            host_display = host_display[:20] + "..."
        prov_display = (r["provider"] or "-")
        if len(prov_display) > 9:
            prov_display = prov_display[:8] + "…"

        api_marker = "●" if r["is_api"] else " "
        print(
            f"{r['id']:<5} {_format_ts(r['ts']):<15} {_caller_label(r['caller']):<8} "
            f"{prov_display:<10} "
            f"{r['method']:<7} {status_str:>6} "
            f"{host_display:<24} {path_display:<28} "
            f"{_format_size(r['req_bytes']):>6} {_format_size(r['resp_bytes']):>6} {api_marker}"
        )


def _print_detail(row: sqlite3.Row) -> None:
    """Print full detail for a single traffic row."""
    width = max(24, shutil.get_terminal_size((120, 40)).columns - 2)
    lines = _detail_content_lines(row, width=width, max_body_lines=300)

    print()
    print("═" * 80)
    for line in lines:
        print(_render_terminal_line(line))
    print("═" * 80)


def _request_params(row: sqlite3.Row) -> list[tuple[str, str]]:
    """Extract human-readable request parameters from URL and JSON request body."""
    params: list[tuple[str, str]] = []

    path = row["path"] or ""
    query_pairs = parse_qsl(urlsplit(path).query, keep_blank_values=True)
    for key, value in query_pairs:
        params.append((f"url.{key}", value))

    body_text = row["req_body"] or ""
    if not body_text:
        return params

    try:
        body = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return params
    if not isinstance(body, dict):
        return params

    # Common request-shaping keys that are useful at a glance.
    for key in ("model", "stream", "temperature", "top_p", "max_tokens", "tool_choice"):
        if key in body:
            params.append((f"body.{key}", str(body.get(key))))

    # Length-oriented parameters for chat-style inputs.
    if isinstance(body.get("messages"), list):
        params.append(("body.messages", str(len(body["messages"]))))
    if isinstance(body.get("input"), list):
        params.append(("body.input", str(len(body["input"]))))
    if isinstance(body.get("contents"), list):
        params.append(("body.contents", str(len(body["contents"]))))
    if isinstance(body.get("tools"), list):
        params.append(("body.tools", str(len(body["tools"]))))

    # Include a few additional scalar keys for debugging unknown providers.
    extra_count = 0
    for key in sorted(body.keys()):
        if key in {"model", "stream", "temperature", "top_p", "max_tokens", "tool_choice",
                   "messages", "input", "contents", "tools"}:
            continue
        value = body.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            params.append((f"body.{key}", str(value)))
            extra_count += 1
            if extra_count >= 8:
                break

    return params


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            part_text = _message_content_to_text(part)
            if part_text:
                parts.append(part_text)
        return "\n\n".join(parts)
    if isinstance(content, dict):
        ptype = str(content.get("type", "")).lower()
        if ptype in {"text", "input_text", "output_text"}:
            text = content.get("text") or content.get("content") or ""
            return str(text)
        if "text" in content and isinstance(content.get("text"), str):
            return str(content["text"])
        if "parts" in content:
            return _message_content_to_text(content.get("parts"))
        if "content" in content:
            return _message_content_to_text(content.get("content"))
        if ptype in {"image", "image_url", "input_image"}:
            return "[image]"
        values = _json_leaf_values(content, limit=80)
        if values:
            return "\n".join(values)
        return json.dumps(content, indent=2, ensure_ascii=False)
    return str(content)


def _json_leaf_values(value: Any, limit: int = 80) -> list[str]:
    """Extract scalar leaf values from JSON-like data."""
    out: list[str] = []

    def _walk(node: Any) -> None:
        if len(out) >= limit:
            return
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if text:
                out.append(text)
            return
        if isinstance(node, (int, float, bool)):
            out.append(str(node))
            return
        if isinstance(node, list):
            for item in node:
                _walk(item)
                if len(out) >= limit:
                    break
            return
        if isinstance(node, dict):
            for item in node.values():
                _walk(item)
                if len(out) >= limit:
                    break

    _walk(value)
    return out


def _json_text_values(value: Any, limit: int = 80) -> list[str]:
    """Extract likely human-readable text values from JSON-like data."""
    out: list[str] = []
    text_keys = {
        "text",
        "content",
        "message",
        "output_text",
        "input_text",
        "prompt",
        "completion",
    }

    def _walk(node: Any, key_hint: str = "") -> None:
        if len(out) >= limit:
            return
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if not text:
                return
            if key_hint in text_keys or "\n" in text or len(text.split()) > 2:
                out.append(text)
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, key_hint)
                if len(out) >= limit:
                    break
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, str(k).lower())
                if len(out) >= limit:
                    break

    _walk(value)
    return out


def _extract_request_steps(body_text: str | None) -> list[tuple[str, str]]:
    """Extract role/content steps from request payload formats."""
    if not body_text:
        return []
    try:
        payload = json.loads(body_text)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []

    steps: list[tuple[str, str]] = []

    def _append(role: str, content: Any) -> None:
        text = _message_content_to_text(content).strip()
        if text:
            steps.append((role or "unknown", text))

    messages = payload.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            _append(str(item.get("role") or "message"), item.get("content"))

    input_items = payload.get("input")
    if isinstance(input_items, list):
        for item in input_items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or item.get("type") or "input")
            content: Any = item.get("content")
            if content is None:
                if "input_text" in item:
                    content = item.get("input_text")
                elif "text" in item:
                    content = item.get("text")
                else:
                    content = item
            _append(role, content)

    contents = payload.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "content")
            _append(role, item.get("parts") or item.get("content") or item)

    system_val = payload.get("system")
    if system_val is not None:
        _append("system", system_val)

    instruction_val = payload.get("instructions")
    if instruction_val is not None:
        _append("system", instruction_val)

    return steps


def _extract_response_steps(body_text: str | None) -> list[tuple[str, str]]:
    """Extract assistant text from response JSON / SSE JSON events."""
    if not body_text:
        return []

    def _from_obj(obj: Any) -> str:
        if isinstance(obj, dict):
            chunks: list[str] = []

            # Anthropic streaming shapes
            if obj.get("type") == "content_block_delta":
                delta = obj.get("delta")
                if isinstance(delta, dict):
                    txt = delta.get("text") or delta.get("partial_json")
                    if isinstance(txt, str) and txt.strip():
                        chunks.append(txt.strip())
            if obj.get("type") == "content_block_start":
                block = obj.get("content_block")
                if isinstance(block, dict):
                    txt = block.get("text")
                    if isinstance(txt, str) and txt.strip():
                        chunks.append(txt.strip())

            # OpenAI-style chunks
            choices = obj.get("choices")
            if isinstance(choices, list):
                for ch in choices:
                    if not isinstance(ch, dict):
                        continue
                    delta = ch.get("delta")
                    if isinstance(delta, dict):
                        txt = delta.get("content")
                        if isinstance(txt, str) and txt.strip():
                            chunks.append(txt.strip())
                    msg = ch.get("message")
                    if isinstance(msg, dict):
                        txt = msg.get("content")
                        if isinstance(txt, str) and txt.strip():
                            chunks.append(txt.strip())

            # Generic content/output
            for key in ("output_text", "text"):
                txt = obj.get(key)
                if isinstance(txt, str) and txt.strip():
                    chunks.append(txt.strip())
            for key in ("output", "content", "message", "response"):
                val = obj.get(key)
                txt = _message_content_to_text(val).strip() if val is not None else ""
                if txt:
                    chunks.append(txt)

            if chunks:
                return "\n".join(chunks)

            vals = _json_text_values(obj, limit=60)
            return "\n".join(vals)

        if isinstance(obj, list):
            vals = _json_text_values(obj, limit=60)
            return "\n".join(vals)

        if isinstance(obj, str):
            return obj.strip()
        return ""

    chunks: list[str] = []
    saw_sse = False
    for raw in body_text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        saw_sse = True
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        txt = _from_obj(obj)
        if txt:
            chunks.append(txt)

    if not saw_sse:
        try:
            obj = json.loads(body_text)
        except (json.JSONDecodeError, TypeError):
            return []
        txt = _from_obj(obj)
        return [("assistant", txt)] if txt else []

    if not chunks:
        return []
    merged = "\n".join(chunks).strip()
    return [("assistant", merged)] if merged else []


def _extract_conversation_steps(row: sqlite3.Row) -> list[tuple[str, str]]:
    steps = _extract_request_steps(row["req_body"])
    steps.extend(_extract_response_steps(row["resp_body"]))
    return steps


def _format_markdown_lines(text: str, width: int, indent: str = "  ") -> list[str]:
    """Render markdown-ish text with terminal-friendly word wrapping."""
    max_width = max(24, width)
    out: list[str] = []
    in_code = False

    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            out.append(f"{indent}{line}")
            in_code = not in_code
            continue

        if in_code:
            out.append(f"{indent}{line}")
            continue

        if not stripped:
            out.append("")
            continue

        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            out.append(f"{indent}{heading.upper()}" if heading else "")
            continue

        bullet = ""
        content = stripped
        if stripped.startswith(("- ", "* ", "+ ")):
            bullet = "• "
            content = stripped[2:].strip()
        else:
            match = re.match(r"^(\d+)\.\s+(.*)$", stripped)
            if match:
                bullet = f"{match.group(1)}. "
                content = match.group(2).strip()

        if bullet:
            wrapped = textwrap.fill(
                content,
                width=max_width - len(indent) - len(bullet),
                initial_indent=f"{indent}{bullet}",
                subsequent_indent=f"{indent}{' ' * len(bullet)}",
                break_long_words=False,
                break_on_hyphens=False,
            )
        else:
            wrapped = textwrap.fill(
                stripped,
                width=max_width - len(indent),
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        out.extend(wrapped.splitlines())

    return out


def _conversation_lines(row: sqlite3.Row, width: int) -> list[str]:
    steps = _extract_conversation_steps(row)
    if not steps:
        return []

    lines = ["── Conversation Steps (Markdown) ──", ""]
    for idx, (role, content) in enumerate(steps, 1):
        lines.append(f"[{idx}] {role.upper()}")
        lines.extend(_format_markdown_lines(content, width=max(24, width - 2), indent="  "))
        lines.append("")
    return lines


def _detail_content_lines(
    row: sqlite3.Row,
    *,
    width: int,
    max_body_lines: int,
) -> list[str]:
    """Canonical detail content shared by all viewer routes."""
    lines: list[str] = [
        f"  ID:       {row['id']}",
        f"  Time:     {row['ts']}",
        f"  Caller:   {_caller_label(row['caller'])}",
        f"  Method:   {row['method']}",
        f"  URL:      {row['scheme']}://{row['host']}:{row['port'] or '?'}{row['path']}",
        f"  Provider: {row['provider'] or '-'}",
        f"  Status:   {row['status'] or '-'}",
        f"  Req size: {_format_size(row['req_bytes'])}",
        f"  Resp size:{_format_size(row['resp_bytes'])}",
    ]

    params = _request_params(row)
    if params:
        lines.append("")
        lines.append("── Request Params ──")
        for key, value in params:
            lines.append(f"  {key}: {value}")

    convo = _conversation_lines(row, width=max(24, width - 2))
    if convo:
        lines.append("")
        lines.extend(convo)

    lines.append("")

    if row["req_body"]:
        lines.append("── Request Body ──")
        lines.extend(_format_body_pairs_lines(row["req_body"], width=width, max_lines=max_body_lines))
        lines.append("")

    if row["resp_body"]:
        lines.append("── Response Body ──")
        lines.extend(_format_body_pairs_lines(row["resp_body"], width=width, max_lines=max_body_lines))
        lines.append("")

    if not row["req_body"] and not row["resp_body"]:
        lines.append("  (no body content recorded — address-only log entry)")
        lines.append("")

    return lines


# ── Interactive urwid viewer ─────────────────────────────────────────

def _detail_lines(row: sqlite3.Row, index: int, total: int, width: int) -> list[str]:
    """Build detail-view lines for interactive UIs."""
    lines: list[str] = [f"Detail {index + 1}/{total}  ID={row['id']}", ""]
    lines.extend(_detail_content_lines(row, width=width, max_body_lines=300))
    return lines


def _interactive_viewer_urwid(rows: list[sqlite3.Row], conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """urwid-based interactive traffic viewer."""
    if urwid is None:
        raise RuntimeError("urwid not available")

    state: dict[str, Any] = {
        "caller": args.caller or "",
        "provider": args.provider or "",
        "host": args.host or "",
        "search": args.search or "",
        "api_only": args.api,
        "sort": {"date": "time", "address": "domain"}.get(args.sort or "time", args.sort or "time"),
        "limit": args.limit,
        "rows": rows,
        "mode": "list",  # list | detail | prompt_search | prompt_host
        "detail_idx": 0,
    }

    palette = [
        ("title", "black", "light gray", "bold"),
        ("subtitle", "light gray", "default"),
        ("focus", "black", "light cyan"),
        ("has_data", "dark green", "default", "bold"),
        ("has_data_focus", "black", "dark green", "bold"),
        ("footer", "dark gray", "default"),
        ("md_bold", "default,bold", "default"),
    ]

    title = urwid.Text("")
    subtitle = urwid.Text("")
    footer = urwid.Text("", align="left")
    rows_walker = urwid.SimpleFocusListWalker([])
    listbox = urwid.ListBox(rows_walker)
    header = urwid.Pile([urwid.AttrMap(title, "title"), urwid.AttrMap(subtitle, "subtitle")])
    frame = urwid.Frame(listbox, header=header, footer=urwid.AttrMap(footer, "footer"))

    prompt_edit: dict[str, Any] = {"widget": None}
    detail_listbox: dict[str, Any] = {"widget": None}

    def _providers() -> list[str]:
        vals = conn.execute(
            "SELECT DISTINCT provider FROM traffic WHERE provider IS NOT NULL AND provider <> '' "
            "ORDER BY provider ASC"
        ).fetchall()
        return [""] + [str(v[0]) for v in vals if v and v[0]]

    def _filters_text() -> str:
        parts: list[str] = []
        if state["caller"]:
            parts.append(f"caller={state['caller']}")
        if state["provider"]:
            parts.append(f"provider={state['provider']}")
        if state["host"]:
            parts.append(f"host={state['host']}")
        if state["search"]:
            parts.append(f"search={state['search']}")
        if state["api_only"]:
            parts.append("api-only")
        if state["sort"] != "time":
            parts.append(f"sort={state['sort']}")
        return "  ".join(parts) if parts else "no filters"

    def _row_text(row: sqlite3.Row) -> str:
        status_str = str(row["status"]) if row["status"] else "  -"
        host_display = row["host"][:21] if len(row["host"]) > 21 else row["host"]
        path_display = row["path"][:23] if len(row["path"]) > 23 else row["path"]
        provider_display = row["provider"] or "-"
        if len(provider_display) > 9:
            provider_display = provider_display[:8] + "…"
        api_marker = "*" if row["is_api"] else " "
        return (
            f"{_format_ts(row['ts']):<15} "
            f"{_caller_label(row['caller']):<8} "
            f"{provider_display:<10} "
            f"{row['method']:<7} "
            f"{status_str:>4} "
            f"{host_display:<22} "
            f"{path_display:<24} "
            f"{_format_size(row['req_bytes']):>6} "
            f"{_format_size(row['resp_bytes']):>6} {api_marker}"
        )

    def _refresh_rows() -> None:
        try:
            selected = rows_walker.focus  # type: ignore[attr-defined]
        except Exception:
            selected = 0
        query, params = _build_query(
            caller=state["caller"],
            provider=state["provider"],
            host=state["host"],
            search=state["search"],
            api_only=state["api_only"],
            sort=state["sort"],
            limit=state["limit"],
        )
        state["rows"] = conn.execute(query, params).fetchall()
        del rows_walker[:]
        for idx, row in enumerate(state["rows"]):
            widget = urwid.SelectableIcon(_row_text(row), cursor_position=0)
            row_attr = "has_data" if (row["req_body"] or row["resp_body"]) else None
            row_focus = "has_data_focus" if row_attr else "focus"
            wrapped = urwid.AttrMap(widget, row_attr, focus_map=row_focus)
            wrapped._row_index = idx  # type: ignore[attr-defined]
            rows_walker.append(wrapped)
        if state["rows"]:
            rows_walker.set_focus(min(selected, len(state["rows"]) - 1))
        title.set_text(f" ai-cli Traffic Viewer  ({len(state['rows'])} rows)")
        subtitle.set_text(
            "Time            Caller   Prov       Method   St Host                   "
            "Path                     Req   Resp"
        )
        footer.set_text(
            f"↑↓ nav  Enter detail  / search  h host  c caller  p provider  a api  s sort  q quit"
            f"  [{_filters_text()}]"
        )

    def _show_list() -> None:
        state["mode"] = "list"
        frame.body = listbox
        frame.footer = urwid.AttrMap(footer, "footer")
        _refresh_rows()

    def _show_detail(index: int) -> None:
        if not state["rows"]:
            return
        index = max(0, min(index, len(state["rows"]) - 1))
        state["mode"] = "detail"
        state["detail_idx"] = index
        row = state["rows"][index]
        width = max(24, shutil.get_terminal_size((120, 40)).columns - 2)
        lines = _detail_lines(row, index, len(state["rows"]), width=width)
        widgets: list[urwid.Widget] = []
        for line in lines:
            text, is_bold = _markdown_line_style(line)
            w: urwid.Widget = urwid.Text(text, wrap="space")
            if is_bold:
                w = urwid.AttrMap(w, "md_bold")
            widgets.append(w)
        walker = urwid.SimpleFocusListWalker(widgets)
        detail_listbox["widget"] = urwid.ListBox(walker)
        frame.body = detail_listbox["widget"]
        title.set_text(f" Traffic Detail  {index + 1}/{len(state['rows'])}")
        subtitle.set_text(f"#{row['id']} {row['method']} {row['host']}{row['path'][:40]}")
        footer.set_text("↑↓ scroll  ←/→ prev-next request  q/Esc back")

    def _start_prompt(kind: str, caption: str, initial: str) -> None:
        state["mode"] = kind
        edit = urwid.Edit(caption, edit_text=initial)
        prompt_edit["widget"] = edit
        frame.footer = urwid.AttrMap(edit, "focus")

    def _apply_prompt_value(value: str) -> None:
        if state["mode"] == "prompt_search":
            state["search"] = value
        elif state["mode"] == "prompt_host":
            state["host"] = value
        _show_list()

    def _unhandled_input(key: str) -> None:
        if state["mode"].startswith("prompt_"):
            if key == "enter":
                edit = prompt_edit["widget"]
                value = edit.edit_text.strip() if edit is not None else ""
                _apply_prompt_value(value)
                return
            if key in ("esc",):
                _show_list()
                return
            return

        if state["mode"] == "detail":
            if key in ("q", "Q", "esc"):
                _show_list()
                return
            if key in ("left", "h"):
                _show_detail(state["detail_idx"] - 1)
                return
            if key in ("right", "l"):
                _show_detail(state["detail_idx"] + 1)
                return
            return

        # List mode
        if key in ("q", "Q"):
            raise urwid.ExitMainLoop()
        if key == "enter":
            if state["rows"]:
                _show_detail(rows_walker.focus)
            return
        if key == "/":
            _start_prompt("prompt_search", "Search body/path/host/provider/id: ", state["search"])
            return
        if key == "h":
            _start_prompt("prompt_host", "Filter host: ", state["host"])
            return
        if key == "c":
            callers = ["", "claude", "copilot", "codex", "gemini"]
            try:
                idx = callers.index(state["caller"])
            except ValueError:
                idx = 0
            state["caller"] = callers[(idx + 1) % len(callers)]
            _refresh_rows()
            return
        if key == "p":
            providers = _providers()
            try:
                idx = providers.index(state["provider"])
            except ValueError:
                idx = 0
            state["provider"] = providers[(idx + 1) % len(providers)]
            _refresh_rows()
            return
        if key == "a":
            state["api_only"] = not state["api_only"]
            _refresh_rows()
            return
        if key == "s":
            try:
                idx = _SORT_MODES.index(state["sort"])
            except ValueError:
                idx = 0
            state["sort"] = _SORT_MODES[(idx + 1) % len(_SORT_MODES)]
            _refresh_rows()
            return
        if key == "r":
            _refresh_rows()
            return

    _refresh_rows()
    loop = urwid.MainLoop(frame, palette=palette, unhandled_input=_unhandled_input)
    loop.run()


# ── Interactive curses viewer ─────────────────────────────────────────

def _init_colors() -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = False
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_MAGENTA, -1)   # claude
        curses.init_pair(2, curses.COLOR_CYAN, -1)      # copilot
        curses.init_pair(3, curses.COLOR_GREEN, -1)     # codex
        curses.init_pair(4, curses.COLOR_YELLOW, -1)    # gemini
        curses.init_pair(5, curses.COLOR_RED, -1)       # error status
        curses.init_pair(6, curses.COLOR_WHITE, curses.COLOR_BLUE)  # selected row
        curses.init_pair(7, curses.COLOR_WHITE, -1)     # header
        curses.init_pair(8, curses.COLOR_GREEN, -1)     # rows with body data
        _COLOR_ENABLED = True
    except curses.error:
        _COLOR_ENABLED = False


def _cp(pair: int) -> int:
    if not _COLOR_ENABLED:
        return 0
    try:
        return curses.color_pair(pair)
    except curses.error:
        return 0


def _caller_attr(caller: str) -> int:
    pair = _CALLER_COLORS.get(caller, 0)
    return _cp(pair) | curses.A_BOLD if pair else curses.A_DIM


def _addnstr_safe(
    stdscr: curses.window,
    y: int,
    x: int,
    text: str,
    attr: int = 0,
) -> None:
    """Best-effort addnstr that avoids lower-right corner curses errors."""
    height, width = stdscr.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width:
        return

    max_chars = width - x
    # Writing to the lower-right corner can raise curses.error even when valid.
    if y == height - 1:
        max_chars -= 1
    if max_chars <= 0:
        return

    try:
        stdscr.addnstr(y, x, text, max_chars, attr)
    except curses.error:
        pass


def _draw_list(
    stdscr: curses.window,
    rows: list[sqlite3.Row],
    selected: int,
    scroll_offset: int,
    filter_text: str,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    # Title bar
    title = " ai-cli Traffic Viewer"
    if filter_text:
        title += f"  [filter: {filter_text}]"
    title += f"  ({len(rows)} rows)"
    _addnstr_safe(stdscr, 0, 0, title.ljust(width), curses.A_BOLD | _cp(7))

    # Help line
    help_text = " ↑↓ nav  Enter detail  / search  c caller  p provider  a api  s sort  q quit"
    _addnstr_safe(stdscr, 1, 0, help_text.ljust(width), curses.A_DIM)

    # Column header
    hdr = (
        f"{'Time':<15} {'Caller':<8} {'Prov':<10} {'Method':<7} {'St':>4} "
        f"{'Host':<22} {'Path':<24} {'Req':>6} {'Resp':>6}"
    )
    _addnstr_safe(stdscr, 2, 0, hdr[:width], curses.A_UNDERLINE)

    # Rows
    list_height = height - 4
    for i in range(list_height):
        row_idx = scroll_offset + i
        if row_idx >= len(rows):
            break
        r = rows[row_idx]
        y = 3 + i

        is_selected = row_idx == selected
        has_data = bool(r["req_body"] or r["resp_body"])
        base_attr = _cp(6) if is_selected else curses.A_NORMAL
        if has_data and not is_selected:
            base_attr = _cp(8) | curses.A_BOLD

        status_str = str(r["status"]) if r["status"] else "  -"
        host_display = r["host"][:21] if len(r["host"]) > 21 else r["host"]
        path_display = r["path"][:23] if len(r["path"]) > 23 else r["path"]
        provider_display = (r["provider"] or "-")
        if len(provider_display) > 9:
            provider_display = provider_display[:8] + "…"
        api_marker = "●" if r["is_api"] else " "

        line = (
            f"{_format_ts(r['ts']):<15} "
            f"{_caller_label(r['caller']):<8} "
            f"{provider_display:<10} "
            f"{r['method']:<7} "
            f"{status_str:>4} "
            f"{host_display:<22} "
            f"{path_display:<24} "
            f"{_format_size(r['req_bytes']):>6} "
            f"{_format_size(r['resp_bytes']):>6} {api_marker}"
        )

        if is_selected:
            _addnstr_safe(stdscr, y, 0, line.ljust(width)[:width], base_attr)
        else:
            _addnstr_safe(stdscr, y, 0, line[:width], base_attr)

    # Status bar
    if rows:
        pos_text = f" Row {selected + 1}/{len(rows)}"
        _addnstr_safe(stdscr, height - 1, 0, pos_text.ljust(width), curses.A_REVERSE)

    stdscr.refresh()


def _draw_detail(
    stdscr: curses.window,
    row: sqlite3.Row,
    scroll: int,
    index: int,
    total: int,
) -> int:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    lines = _detail_content_lines(row, width=max(24, width - 2), max_body_lines=200)

    # Title
    title = f" Detail {index + 1}/{total}: #{row['id']} {row['method']} {row['host']}{row['path'][:28]}"
    _addnstr_safe(stdscr, 0, 0, title.ljust(width)[:width], curses.A_BOLD | curses.A_REVERSE)

    # Content with scroll
    view_height = height - 2
    for i in range(view_height):
        line_idx = scroll + i
        if line_idx >= len(lines):
            break
        text, is_bold = _markdown_line_style(lines[line_idx])
        attr = curses.A_BOLD if is_bold else 0
        _addnstr_safe(stdscr, 1 + i, 0, text[:width], attr)

    help_text = " ↑↓ scroll  ←/→ prev-next request  q/Esc back"
    _addnstr_safe(stdscr, height - 1, 0, help_text.ljust(width)[:width], curses.A_DIM)

    stdscr.refresh()
    return len(lines)


def _curses_prompt(stdscr: curses.window, prompt: str) -> str:
    """Show a prompt on the bottom line and read text input."""
    height, width = stdscr.getmaxyx()
    _addnstr_safe(stdscr, height - 1, 0, prompt.ljust(width)[:width], curses.A_REVERSE)
    stdscr.refresh()
    curses.echo()
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    try:
        buf = stdscr.getstr(height - 1, len(prompt), width - len(prompt) - 1)
        return buf.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""
    finally:
        curses.noecho()
        try:
            curses.curs_set(0)
        except curses.error:
            pass


def _read_key(stdscr: curses.window) -> int:
    """Read one key, normalizing common ESC arrow sequences."""
    key = stdscr.getch()
    if key != 27:
        return key

    stdscr.nodelay(True)
    try:
        nxt = stdscr.getch()
        if nxt == -1:
            return 27
        if nxt == 91:  # '['
            final = stdscr.getch()
            if final == 65:
                return curses.KEY_UP
            if final == 66:
                return curses.KEY_DOWN
            if final == 67:
                return curses.KEY_RIGHT
            if final == 68:
                return curses.KEY_LEFT
        return 27
    finally:
        stdscr.nodelay(False)


def _interactive_viewer(rows: list[sqlite3.Row], conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Curses-based interactive traffic viewer."""

    # Mutable state for re-querying
    state = {
        "caller": args.caller or "",
        "provider": args.provider or "",
        "host": args.host or "",
        "search": args.search or "",
        "api_only": args.api,
        "sort": {"date": "time", "address": "domain"}.get(args.sort or "time", args.sort or "time"),
        "limit": args.limit,
        "rows": rows,
        "selected": 0,
        "scroll_offset": 0,
    }

    def _requery() -> None:
        query, params = _build_query(
            caller=state["caller"],
            provider=state["provider"],
            host=state["host"],
            search=state["search"],
            api_only=state["api_only"],
            sort=state["sort"],
            limit=state["limit"],
        )
        state["rows"] = conn.execute(query, params).fetchall()
        state["selected"] = 0
        state["scroll_offset"] = 0

    def _filter_label() -> str:
        parts = []
        if state["caller"]:
            parts.append(f"caller={state['caller']}")
        if state["host"]:
            parts.append(f"host={state['host']}")
        if state["search"]:
            parts.append(f"search={state['search']}")
        if state["provider"]:
            parts.append(f"provider={state['provider']}")
        if state["api_only"]:
            parts.append("api-only")
        if state["sort"] != "time":
            parts.append(f"sort={state['sort']}")
        return "  ".join(parts)

    def _providers() -> list[str]:
        rows = conn.execute(
            "SELECT DISTINCT provider FROM traffic WHERE provider IS NOT NULL AND provider <> '' "
            "ORDER BY provider ASC"
        ).fetchall()
        vals = [str(r[0]) for r in rows if r and r[0]]
        return ["", *vals]

    def _inner(stdscr: curses.window) -> None:
        _init_colors()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(-1)

        while True:
            current_rows = state["rows"]
            height, width = stdscr.getmaxyx()
            list_height = height - 4

            # Ensure selected is in bounds
            if current_rows:
                state["selected"] = max(0, min(state["selected"], len(current_rows) - 1))
            else:
                state["selected"] = 0

            # Auto-scroll to keep selected visible
            if state["selected"] < state["scroll_offset"]:
                state["scroll_offset"] = state["selected"]
            elif state["selected"] >= state["scroll_offset"] + list_height:
                state["scroll_offset"] = state["selected"] - list_height + 1

            _draw_list(stdscr, current_rows, state["selected"], state["scroll_offset"], _filter_label())

            key = _read_key(stdscr)

            if key in (ord("q"), 27):
                return
            elif key in (curses.KEY_UP, ord("k")):
                state["selected"] = max(0, state["selected"] - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                if current_rows:
                    state["selected"] = min(len(current_rows) - 1, state["selected"] + 1)
            elif key == curses.KEY_PPAGE:
                state["selected"] = max(0, state["selected"] - list_height)
            elif key == curses.KEY_NPAGE:
                if current_rows:
                    state["selected"] = min(len(current_rows) - 1, state["selected"] + list_height)
            elif key == curses.KEY_HOME:
                state["selected"] = 0
            elif key == curses.KEY_END:
                if current_rows:
                    state["selected"] = len(current_rows) - 1
            elif key in (10, 13, curses.KEY_ENTER):
                # Detail view
                if current_rows:
                    detail_idx = state["selected"]
                    row = current_rows[detail_idx]
                    detail_scroll = 0
                    while True:
                        total_lines = _draw_detail(
                            stdscr, row, detail_scroll, detail_idx, len(current_rows)
                        )
                        dk = _read_key(stdscr)
                        if dk in (ord("q"), 27):
                            break
                        elif dk in (curses.KEY_UP, ord("k")):
                            detail_scroll = max(0, detail_scroll - 1)
                        elif dk in (curses.KEY_DOWN, ord("j")):
                            detail_scroll = min(max(0, total_lines - (height - 2)), detail_scroll + 1)
                        elif dk == curses.KEY_PPAGE:
                            detail_scroll = max(0, detail_scroll - (height - 3))
                        elif dk == curses.KEY_NPAGE:
                            detail_scroll = min(max(0, total_lines - (height - 2)), detail_scroll + (height - 3))
                        elif dk in (curses.KEY_LEFT, ord("h")):
                            if detail_idx > 0:
                                detail_idx -= 1
                                row = current_rows[detail_idx]
                                detail_scroll = 0
                                state["selected"] = detail_idx
                        elif dk in (curses.KEY_RIGHT, ord("l")):
                            if detail_idx < len(current_rows) - 1:
                                detail_idx += 1
                                row = current_rows[detail_idx]
                                detail_scroll = 0
                                state["selected"] = detail_idx
            elif key == ord("/"):
                # Search prompt
                text = _curses_prompt(stdscr, "Search body/path: ")
                state["search"] = text
                _requery()
            elif key == ord("c"):
                # Cycle caller filter
                callers = ["", "claude", "copilot", "codex", "gemini"]
                try:
                    idx = callers.index(state["caller"])
                except ValueError:
                    idx = 0
                state["caller"] = callers[(idx + 1) % len(callers)]
                _requery()
            elif key == ord("p"):
                # Cycle provider filter
                providers = _providers()
                try:
                    idx = providers.index(state["provider"])
                except ValueError:
                    idx = 0
                state["provider"] = providers[(idx + 1) % len(providers)]
                _requery()
            elif key == ord("h"):
                # Host filter prompt
                text = _curses_prompt(stdscr, "Filter host: ")
                state["host"] = text
                _requery()
            elif key == ord("a"):
                # Toggle API-only
                state["api_only"] = not state["api_only"]
                _requery()
            elif key == ord("s"):
                # Cycle sort mode
                try:
                    idx = _SORT_MODES.index(state["sort"])
                except ValueError:
                    idx = 0
                state["sort"] = _SORT_MODES[(idx + 1) % len(_SORT_MODES)]
                _requery()
            elif key == ord("r"):
                # Refresh
                _requery()

    curses.wrapper(_inner)


# ── CLI entry point ───────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-cli traffic",
        description="Browse and search proxied API traffic.",
    )
    parser.add_argument(
        "--caller", "-c",
        choices=["claude", "copilot", "codex", "gemini"],
        help="Filter by caller tool",
    )
    parser.add_argument(
        "--host",
        help="Filter by host substring",
    )
    parser.add_argument(
        "--provider",
        help="Filter by provider (e.g. anthropic/openai/copilot/google)",
    )
    parser.add_argument(
        "--search", "-s",
        help="Search in request/response bodies and paths",
    )
    parser.add_argument(
        "--api", "-a",
        action="store_true",
        help="Show only confirmed API calls (with body content)",
    )
    parser.add_argument(
        "--sort",
        choices=["time", "domain", "request", "provider", "date", "address"],
        default="time",
        help="Sort order (default: time)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=100,
        help="Maximum rows to show (default: 100)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB_PATH,
        help="Path to traffic database",
    )
    parser.add_argument(
        "--no-interactive", "--plain",
        action="store_true",
        dest="plain",
        help="Plain text output (no curses)",
    )
    parser.add_argument(
        "--detail", "-d",
        type=int,
        metavar="ID",
        help="Show detail for a specific row ID",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    conn = _connect(args.db)

    # Single row detail mode
    if args.detail:
        caller_sel = "caller" if _traffic_db.HAS_CALLER_COL else "'' AS caller"
        port_sel = "port" if _traffic_db.HAS_PORT_COL else "NULL AS port"
        row = conn.execute(
            f"SELECT id, ts, {caller_sel}, method, scheme, host, {port_sel}, path, "
            "provider, is_api, status, req_bytes, resp_bytes, "
            "req_body, resp_body FROM traffic WHERE id = ?",
            (args.detail,),
        ).fetchone()
        if not row:
            print(f"No traffic row with id={args.detail}", file=sys.stderr)
            conn.close()
            return 1
        _print_detail(row)
        conn.close()
        return 0

    # Query
    query, params = _build_query(
        caller=args.caller or "",
        provider=args.provider or "",
        host=args.host or "",
        search=args.search or "",
        api_only=args.api,
        sort={"date": "time", "address": "domain"}.get(args.sort, args.sort),
        limit=args.limit,
    )
    rows = conn.execute(query, params).fetchall()

    # Interactive or plain
    use_interactive = (
        not args.plain
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )

    if use_interactive:
        used_interactive = False
        if urwid is not None:
            try:
                _interactive_viewer_urwid(rows, conn, args)
                used_interactive = True
            except Exception:
                used_interactive = False
        if not used_interactive:
            try:
                _interactive_viewer(rows, conn, args)
                used_interactive = True
            except curses.error:
                used_interactive = False
        if not used_interactive:
            _print_table(rows)
            if rows:
                print(f"\nUse 'ai-cli traffic --detail ID' to view full request/response.")
    else:
        _print_table(rows)
        if rows:
            print(f"\nUse 'ai-cli traffic --detail ID' to view full request/response.")

    conn.close()
    return 0

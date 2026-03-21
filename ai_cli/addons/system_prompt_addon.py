"""System prompt capture addon for mitmproxy.

Intercepts API requests *before* injection addons and stores the original
default system prompt per (provider, model) in a dedicated SQLite database.
Also inspects response JSON / SSE data events for returned system-like
instruction fields and stores them as received roles (e.g. ``system_recv``).

Only updates when the prompt content actually changes (compared by hash).
The DB is compact — one row per unique (provider, model, content_hash).

Must be loaded BEFORE the injection addons (-s order matters in mitmproxy).

Self-contained — no ai_cli imports.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_DB_DIR = Path.home() / ".ai-cli"
_DEFAULT_DB_NAME = "system_prompts.db"

# Provider detection: maps (path_substring) → provider name
_PROVIDER_PATHS: dict[str, str] = {
    "/v1/messages": "anthropic",
    "/backend-api/codex/responses": "openai",
    "/chat/completions": "copilot",
    "/v1beta/models": "google",
    "/v1alpha/models": "google",
    "/v1/models": "google",
    "/v1internal:": "google",
}

_GEMINI_MODEL_RE = re.compile(r"/models/([^/:]+)")
_RECV_ROLE_SUFFIX = "_recv"


def _detect_provider(path: str) -> str:
    for pat, name in _PROVIDER_PATHS.items():
        if pat in path:
            return name
    return ""


def _extract_model_from_url(path: str) -> str:
    """Extract model name from Gemini-style URL paths."""
    m = _GEMINI_MODEL_RE.search(path)
    return m.group(1) if m else ""


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _extract_system_prompts(provider: str, body: dict, path: str) -> list[tuple[str, str, str]]:
    """Extract system/developer prompts from the request body.

    Returns a list of (model, role, prompt_text) tuples.
    *role* is "system", "developer", or "instructions" to distinguish
    prompt sources within a single request.
    """
    results: list[tuple[str, str, str]] = []
    model = ""

    if provider == "anthropic":
        # Claude: body["system"] is str or list of {"type":"text","text":"..."}
        model = body.get("model", "")
        sys_field = body.get("system")
        prompt = ""
        if isinstance(sys_field, str):
            prompt = sys_field
        elif isinstance(sys_field, list):
            parts = []
            for block in sys_field:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            prompt = "\n".join(parts)
        if prompt.strip():
            results.append((model, "system", prompt.strip()))

    elif provider == "openai":
        # Codex Responses API:
        #   body["instructions"] — the immutable system prompt set by Codex
        #   body["input"] messages with role "system" or "developer"
        model = body.get("model", "")

        # Top-level instructions field (Codex's own system prompt)
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            results.append((model, "instructions", instructions.strip()))

        # Messages in input[]
        inp = body.get("input")
        if isinstance(inp, list):
            system_parts: list[str] = []
            dev_parts: list[str] = []
            for msg in inp:
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role not in ("system", "developer"):
                    continue
                content = msg.get("content")
                texts: list[str] = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict):
                            texts.append(c.get("text", "") or c.get("input_text", ""))
                        elif isinstance(c, str):
                            texts.append(c)
                bucket = system_parts if role == "system" else dev_parts
                bucket.extend(t for t in texts if t)

            if system_parts:
                results.append((model, "system", "\n".join(system_parts).strip()))
            if dev_parts:
                results.append((model, "developer", "\n".join(dev_parts).strip()))

    elif provider == "copilot":
        # Copilot: body["messages"] list, look for role=system
        model = body.get("model", "")
        msgs = body.get("messages")
        if isinstance(msgs, list):
            parts: list[str] = []
            for msg in msgs:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "system":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                parts.append(c.get("text", ""))
            prompt = "\n".join(p for p in parts if p).strip()
            if prompt:
                results.append((model, "system", prompt))

    elif provider == "google":
        # Gemini: support both public API and internal cloudcode request shapes.
        model = body.get("model", "") or _extract_model_from_url(path)
        request_obj = body.get("request")
        if isinstance(request_obj, dict):
            model = model or str(request_obj.get("model", "") or "")
        for container in (body, request_obj):
            if not isinstance(container, dict):
                continue
            for key in ("systemInstruction", "system_instruction"):
                text = _text_from_value(container.get(key))
                if text:
                    results.append((model, "system", text))

    return results


def _text_from_value(value: Any) -> str:
    """Extract text from common payload shapes."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            text = _text_from_value(item)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    if isinstance(value, dict):
        for key in ("text", "input_text", "output_text", "content"):
            text = _text_from_value(value.get(key))
            if text:
                return text
        if "parts" in value:
            return _text_from_value(value.get("parts"))
    return ""


def _extract_received_prompts_from_obj(
    provider: str,
    obj: dict[str, Any],
    model_hint: str,
) -> list[tuple[str, str, str]]:
    """Extract system/developer/instructions text from response JSON objects."""
    model = str(obj.get("model") or model_hint or "")
    out: list[tuple[str, str, str]] = []

    for key, role in (
        ("instructions", "instructions"),
        ("system", "system"),
        ("developer", "developer"),
    ):
        text = _text_from_value(obj.get(key))
        if text:
            out.append((model, f"{role}{_RECV_ROLE_SUFFIX}", text))

    # Gemini-style response key
    text = _text_from_value(obj.get("systemInstruction"))
    if text:
        out.append((model, f"system{_RECV_ROLE_SUFFIX}", text))

    # Message arrays with explicit roles
    for key in ("messages", "input"):
        items = obj.get(key)
        if not isinstance(items, list):
            continue
        sys_parts: list[str] = []
        dev_parts: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).lower()
            content = _text_from_value(item.get("content"))
            if not content and "parts" in item:
                content = _text_from_value(item.get("parts"))
            if not content:
                continue
            if role == "system":
                sys_parts.append(content)
            elif role == "developer":
                dev_parts.append(content)
        if sys_parts:
            out.append((model, f"system{_RECV_ROLE_SUFFIX}", "\n".join(sys_parts).strip()))
        if dev_parts:
            out.append((model, f"developer{_RECV_ROLE_SUFFIX}", "\n".join(dev_parts).strip()))

    return out


def _dedupe_prompts(prompts: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str]] = []
    for model, role, text in prompts:
        item = ((model or "(unknown)"), role, text.strip())
        if not item[2] or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _extract_received_prompts_from_sse(
    provider: str,
    sse_text: str,
    model_hint: str,
) -> list[tuple[str, str, str]]:
    prompts: list[tuple[str, str, str]] = []
    for raw in sse_text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            prompts.extend(_extract_received_prompts_from_obj(provider, obj, model_hint))
    return _dedupe_prompts(prompts)


class _PromptDB:
    """Thread-safe SQLite store for captured system prompts."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_prompts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                provider     TEXT NOT NULL,
                model        TEXT NOT NULL,
                role         TEXT NOT NULL DEFAULT 'system',
                content_hash TEXT NOT NULL,
                content      TEXT NOT NULL,
                char_count   INTEGER NOT NULL,
                first_seen   TEXT NOT NULL,
                last_seen    TEXT NOT NULL,
                seen_count   INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider, model, role, content_hash)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sp_provider_model
            ON system_prompts(provider, model)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                cwd      TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL,
                model    TEXT NOT NULL,
                role     TEXT NOT NULL,
                content  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ph_cwd_ts
            ON prompt_history(cwd, ts DESC)
        """)
        # Migration: add role column to existing DBs
        try:
            conn.execute("SELECT role FROM system_prompts LIMIT 0")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE system_prompts ADD COLUMN role TEXT NOT NULL DEFAULT 'system'"
            )
        conn.commit()
        self._conn = conn
        return conn

    def upsert(self, provider: str, model: str, role: str, content: str) -> None:
        h = _content_hash(content)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._ensure()
            conn.execute(
                "INSERT INTO system_prompts "
                "(provider, model, role, content_hash, content, char_count, first_seen, last_seen, seen_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(provider, model, role, content_hash) DO UPDATE SET "
                "last_seen = excluded.last_seen, "
                "seen_count = seen_count + 1",
                (provider, model, role, h, content, len(content), now, now),
            )
            conn.commit()

    def add_history(self, cwd: str, provider: str, model: str, role: str, content: str) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            conn = self._ensure()
            conn.execute(
                "INSERT INTO prompt_history (ts, cwd, provider, model, role, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (now, cwd, provider, model, role, content),
            )
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


# ── mitmproxy addon ───────────────────────────────────────────────────

if True:  # always define for mitmproxy module loading
    from mitmproxy import ctx, http  # type: ignore[import-untyped]

    class SystemPromptCapture:
        """Capture original system prompts before injection modifies them."""

        def __init__(self) -> None:
            self._db: _PromptDB | None = None
            self._flow_meta: dict[int, tuple[str, str]] = {}

        def load(self, loader: Any) -> None:
            loader.add_option(
                "prompt_db",
                str,
                str(_DEFAULT_DB_DIR / _DEFAULT_DB_NAME),
                "Path to SQLite system prompt capture database.",
            )
            loader.add_option(
                "prompt_recv_prefix_file",
                str,
                "",
                "Optional text file prepended to received instruction captures.",
            )
            loader.add_option(
                "prompt_context_cwd",
                str,
                "",
                "Working directory for prompt history context tagging.",
            )

        def _ensure_db(self) -> _PromptDB:
            if self._db is None:
                db_path = Path(
                    getattr(ctx.options, "prompt_db", "") or str(_DEFAULT_DB_DIR / _DEFAULT_DB_NAME)
                )
                self._db = _PromptDB(db_path)
            return self._db

        def _context_cwd(self) -> str:
            return str(getattr(ctx.options, "prompt_context_cwd", "") or "")

        def _recv_prefix_text(self) -> str:
            path_value = str(getattr(ctx.options, "prompt_recv_prefix_file", "") or "").strip()
            if not path_value:
                cwd = self._context_cwd().strip()
                if cwd:
                    candidate = Path(cwd) / "received_instructions_context.txt"
                    path_value = str(candidate)
            if not path_value:
                return ""
            path = Path(path_value).expanduser()
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
            return text

        def _apply_recv_prefix(self, role: str, prompt: str) -> str:
            if not role.endswith(_RECV_ROLE_SUFFIX):
                return prompt
            prefix = self._recv_prefix_text()
            if not prefix:
                return prompt
            return f"{prefix}\n\n{prompt}"

        def request(self, flow: http.HTTPFlow) -> None:
            if flow.request.method.upper() != "POST":
                return

            path = flow.request.path or ""
            provider = _detect_provider(path)
            if not provider:
                return

            body_text = flow.request.get_text(strict=False)
            if not body_text:
                return
            try:
                body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                return
            if not isinstance(body, dict):
                return

            model_hint = str(body.get("model", "") or _extract_model_from_url(path) or "")
            self._flow_meta[id(flow)] = (provider, model_hint)

            prompts = _extract_system_prompts(provider, body, path)
            if not prompts:
                return

            try:
                db = self._ensure_db()
                for model, role, prompt in prompts:
                    final = self._apply_recv_prefix(role, prompt)
                    db.upsert(provider, model or "(unknown)", role, final)
                    db.add_history(self._context_cwd(), provider, model or "(unknown)", role, final)
            except Exception:
                pass  # never break the proxy

        def response(self, flow: http.HTTPFlow) -> None:
            if not flow.response:
                return

            flow_id = id(flow)
            provider, model_hint = self._flow_meta.pop(flow_id, ("", ""))
            if not provider:
                provider = _detect_provider(flow.request.path or "")
            if not provider:
                return

            body_text = flow.response.get_text(strict=False) or ""
            if not body_text:
                return

            # Guard against very large payloads to keep proxy overhead bounded.
            if len(body_text) > 512_000:
                body_text = body_text[:512_000]

            content_type = (flow.response.headers.get("content-type", "") or "").lower()
            prompts: list[tuple[str, str, str]] = []

            if "text/event-stream" in content_type or "data:" in body_text:
                prompts = _extract_received_prompts_from_sse(provider, body_text, model_hint)
            else:
                try:
                    obj = json.loads(body_text)
                except (json.JSONDecodeError, ValueError):
                    obj = None
                if isinstance(obj, dict):
                    prompts = _dedupe_prompts(
                        _extract_received_prompts_from_obj(provider, obj, model_hint)
                    )

            if not prompts:
                return

            try:
                db = self._ensure_db()
                for model, role, prompt in prompts:
                    final = self._apply_recv_prefix(role, prompt)
                    db.upsert(provider, model or "(unknown)", role, final)
                    db.add_history(self._context_cwd(), provider, model or "(unknown)", role, final)
            except Exception:
                pass

        def done(self) -> None:
            if self._db:
                self._db.close()

    addons = [SystemPromptCapture()]

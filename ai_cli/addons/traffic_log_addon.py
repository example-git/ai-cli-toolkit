"""Traffic logging addon for mitmproxy — logs AI-domain traffic to SQLite.

Records HTTP(S) and WS(S) traffic for known AI provider domains. For
requests matching **confirmed** provider API endpoints, it also stores the
request/response body content. Other AI-domain traffic is logged as
address-only rows (host, path, method, status) so we can discover
additional endpoints each provider contacts.

The database is rolling: rows beyond a configurable cap are pruned on
each write cycle so the DB doesn't grow without bound.

Self-contained (no ai_cli imports) — loaded by mitmdump via ``-s``.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Provider detection ────────────────────────────────────────────────

# Confirmed API paths where we log full request/response bodies.
_CONFIRMED_API_PATHS: dict[str, list[str]] = {
    "anthropic": ["/v1/messages"],
    "openai": [
        "/v1/chat/completions",
        "/v1/responses",
        "/backend-api/codex/responses",
    ],
    "copilot": ["/chat/completions"],
    "google": [
        "/v1beta/models",
        "/v1alpha/models",
        "/v1/models",
        "/v1internal:generateContent",
        "/v1internal:streamGenerateContent",
    ],
}

# Hostname substrings → provider tag. Used to tag AI-domain traffic.
_HOST_PROVIDERS: dict[str, str] = {
    "claude.ai": "anthropic",
    "anthropic": "anthropic",
    "chatgpt.com": "openai",
    "oaistatic.com": "openai",
    "openai": "openai",
    "azure": "openai",
    "copilot": "copilot",
    "githubcopilot": "copilot",
    "googleapis": "google",
    "generativelanguage": "google",
}

_DEFAULT_DB_DIR = Path.home() / ".ai-cli"
_DEFAULT_DB_NAME = "traffic.db"
_DEFAULT_MAX_ROWS = 5000
_DEFAULT_MAX_AGE_DAYS = 30

_REDACTION_PATTERNS = (
    # Authorization headers or bearer tokens.
    (re.compile(r"(?i)(authorization\\s*[:=]\\s*)(bearer\\s+)[^\\s\\\",]+"), r"\\1\\2[REDACTED]"),
    # JSON-style API key fields.
    (re.compile(r'(?i)(\"(?:api[-_]?key|token|access_token|refresh_token|secret)\"\\s*:\\s*\")[^\"]+(\")'), r"\\1[REDACTED]\\2"),
    # Generic key=value secrets.
    (re.compile(r"(?i)\\b(api[-_]?key|token|secret|password)\\s*=\\s*[^\\s&]+"), r"\\1=[REDACTED]"),
)


def _identify(host: str, path: str) -> tuple[str, bool]:
    """Return (provider, is_confirmed_api).

    *provider* is non-empty when the host or path matches a known
    provider.  *is_confirmed_api* is True only when the path matches a
    confirmed API endpoint (content should be logged).
    """
    host_lower = host.lower()
    # Normalize doubled slashes (e.g. /backend-api//connector) before matching.
    path_norm = re.sub(r"/{2,}", "/", path or "")

    # Host-specific endpoints used by Codex/ChatGPT surfaces.
    if "chatgpt.com" in host_lower:
        if path_norm.startswith("/backend-api/wham/apps"):
            return "openai", True
        if path_norm.startswith("/backend-api/connector"):
            return "openai", True
    if "developers.openai.com" in host_lower and path_norm.startswith("/mcp"):
        return "openai", True

    # Path-first: if path matches a confirmed endpoint we know the
    # provider and it's a confirmed API call.
    for provider, patterns in _CONFIRMED_API_PATHS.items():
        for pat in patterns:
            if pat in path_norm:
                return provider, True

    # Host heuristic: tag the provider but don't log content.
    for needle, provider in _HOST_PROVIDERS.items():
        if needle in host_lower:
            return provider, False

    return "", False


def _is_ai_domain(host: str) -> bool:
    host_lower = host.lower()
    return any(needle in host_lower for needle in _HOST_PROVIDERS)


def _redact_text(value: str | None) -> str | None:
    if not value:
        return value
    redacted = value
    for pattern, repl in _REDACTION_PATTERNS:
        redacted = pattern.sub(repl, redacted)
    return redacted


# ── Database ──────────────────────────────────────────────────────────

class _TrafficDB:
    """Thread-safe SQLite wrapper for the traffic log."""

    def __init__(
        self,
        db_path: Path,
        max_rows: int = _DEFAULT_MAX_ROWS,
        max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
    ):
        self._db_path = db_path
        self._max_rows = max_rows
        self._max_age_days = max(1, int(max_age_days))
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._has_caller = True

    @staticmethod
    def _has_column(conn: sqlite3.Connection, column: str) -> bool:
        rows = conn.execute("PRAGMA table_info(traffic)").fetchall()
        for row in rows:
            # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
            if len(row) > 1 and row[1] == column:
                return True
        return False

    def _ensure(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS traffic (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                caller      TEXT    NOT NULL DEFAULT '',
                method      TEXT    NOT NULL,
                scheme      TEXT    NOT NULL DEFAULT 'https',
                host        TEXT    NOT NULL,
                port        INTEGER,
                path        TEXT    NOT NULL,
                provider    TEXT    NOT NULL DEFAULT '',
                is_api      INTEGER NOT NULL DEFAULT 0,
                status      INTEGER,
                req_bytes   INTEGER,
                resp_bytes  INTEGER,
                req_body    TEXT,
                resp_body   TEXT
            )
        """)
        # Migrations first: older DBs may not have port/caller.
        if not self._has_column(conn, "port"):
            conn.execute("ALTER TABLE traffic ADD COLUMN port INTEGER")
        if not self._has_column(conn, "caller"):
            conn.execute("ALTER TABLE traffic ADD COLUMN caller TEXT NOT NULL DEFAULT ''")

        self._has_caller = self._has_column(conn, "caller")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_ts ON traffic(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_provider ON traffic(provider)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_host ON traffic(host)")
        if self._has_caller:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_traffic_caller ON traffic(caller)")
        conn.commit()
        self._conn = conn
        return conn

    def insert(self, **kw: Any) -> int:
        """Insert a traffic row.  Returns the new row id."""
        with self._lock:
            conn = self._ensure()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if self._has_caller:
                cur = conn.execute(
                    "INSERT INTO traffic "
                    "(ts, caller, method, scheme, host, port, path, provider, is_api, "
                    " status, req_bytes, resp_bytes, req_body, resp_body) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        kw.get("caller", ""),
                        kw.get("method", ""),
                        kw.get("scheme", "https"),
                        kw.get("host", ""),
                        kw.get("port"),
                        kw.get("path", ""),
                        kw.get("provider", ""),
                        int(kw.get("is_api", False)),
                        kw.get("status"),
                        kw.get("req_bytes"),
                        kw.get("resp_bytes"),
                        kw.get("req_body"),
                        kw.get("resp_body"),
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO traffic "
                    "(ts, method, scheme, host, port, path, provider, is_api, "
                    " status, req_bytes, resp_bytes, req_body, resp_body) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        kw.get("method", ""),
                        kw.get("scheme", "https"),
                        kw.get("host", ""),
                        kw.get("port"),
                        kw.get("path", ""),
                        kw.get("provider", ""),
                        int(kw.get("is_api", False)),
                        kw.get("status"),
                        kw.get("req_bytes"),
                        kw.get("resp_bytes"),
                        kw.get("req_body"),
                        kw.get("resp_body"),
                    ),
                )
            row_id = cur.lastrowid or 0
            conn.commit()
            self._maybe_prune(conn)
            return row_id

    def update_response(
        self,
        row_id: int,
        status: int,
        resp_bytes: int,
        resp_body: str | None,
    ) -> None:
        with self._lock:
            conn = self._ensure()
            conn.execute(
                "UPDATE traffic SET status = ?, resp_bytes = ?, resp_body = ? "
                "WHERE id = ?",
                (status, resp_bytes, resp_body, row_id),
            )
            conn.commit()

    def _maybe_prune(self, conn: sqlite3.Connection) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - (self._max_age_days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute("DELETE FROM traffic WHERE ts < ?", (cutoff_iso,))

        row = conn.execute("SELECT COUNT(*) FROM traffic").fetchone()
        if row and row[0] > self._max_rows:
            excess = row[0] - self._max_rows + max(self._max_rows // 10, 50)
            conn.execute(
                "DELETE FROM traffic WHERE id IN "
                "(SELECT id FROM traffic ORDER BY id ASC LIMIT ?)",
                (excess,),
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

    class TrafficLogger:
        """Log every proxied URL to SQLite; store body only for confirmed API endpoints."""

        def __init__(self) -> None:
            self._db: _TrafficDB | None = None
            # Map flow id → DB row id for pairing responses
            self._pending: dict[int, int] = {}
            self._warned: set[str] = set()

        def load(self, loader: Any) -> None:
            loader.add_option(
                "traffic_db", str,
                str(_DEFAULT_DB_DIR / _DEFAULT_DB_NAME),
                "Path to SQLite traffic log database.",
            )
            loader.add_option(
                "traffic_max_rows", int, _DEFAULT_MAX_ROWS,
                "Maximum rows to keep in the traffic log (rolling).",
            )
            loader.add_option(
                "traffic_body_cap", int, 64_000,
                "Max characters of body content to store per API request.",
            )
            loader.add_option(
                "traffic_caller", str, "",
                "Caller tool name (claude, copilot, codex, gemini).",
            )
            loader.add_option(
                "traffic_max_age_days", int, _DEFAULT_MAX_AGE_DAYS,
                "Maximum age in days to retain traffic rows.",
            )
            loader.add_option(
                "traffic_redact", bool, True,
                "Redact common secret-like values in captured bodies.",
            )

        def _warn_once(self, key: str, message: str) -> None:
            if key in self._warned:
                return
            self._warned.add(key)
            try:
                ctx.log.warn(message)
            except Exception:
                pass

        def _ensure_db(self) -> _TrafficDB:
            if self._db is None:
                db_path = Path(
                    getattr(ctx.options, "traffic_db", "")
                    or str(_DEFAULT_DB_DIR / _DEFAULT_DB_NAME)
                )
                max_rows = getattr(ctx.options, "traffic_max_rows", _DEFAULT_MAX_ROWS)
                max_age_days = getattr(ctx.options, "traffic_max_age_days", _DEFAULT_MAX_AGE_DAYS)
                self._db = _TrafficDB(db_path, max_rows=max_rows, max_age_days=max_age_days)
            return self._db

        def _body_cap(self) -> int:
            return getattr(ctx.options, "traffic_body_cap", 64_000)

        def _caller(self) -> str:
            return getattr(ctx.options, "traffic_caller", "") or ""

        def _redact_enabled(self) -> bool:
            return bool(getattr(ctx.options, "traffic_redact", True))

        def _redact(self, value: str | None) -> str | None:
            if not self._redact_enabled():
                return value
            return _redact_text(value)

        # ── HTTP ──────────────────────────────────────────────────────

        def request(self, flow: http.HTTPFlow) -> None:
            host = flow.request.host or ""
            path = flow.request.path or ""
            method = flow.request.method or ""
            scheme = flow.request.scheme or "https"
            port = flow.request.port

            if not _is_ai_domain(host):
                return

            provider, is_api = _identify(host, path)

            req_body: str | None = None
            req_bytes = len(flow.request.raw_content or b"")
            if is_api:
                cap = self._body_cap()
                text = flow.request.get_text(strict=False) or ""
                req_body = self._redact(text[:cap] if text else None)

            try:
                db = self._ensure_db()
                row_id = db.insert(
                    caller=self._caller(),
                    method=method,
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    provider=provider,
                    is_api=is_api,
                    req_bytes=req_bytes,
                    req_body=req_body,
                )
                self._pending[id(flow)] = row_id
            except Exception as exc:
                self._warn_once("request_insert", f"traffic logger request insert failed: {exc}")

        def response(self, flow: http.HTTPFlow) -> None:
            if not flow.response:
                return

            host = flow.request.host or ""
            path = flow.request.path or ""
            _, is_api = _identify(host, path)

            status = flow.response.status_code
            resp_bytes = len(flow.response.raw_content or b"")

            resp_body: str | None = None
            if is_api:
                cap = self._body_cap()
                text = flow.response.get_text(strict=False) or ""
                resp_body = self._redact(text[:cap] if text else None)

            row_id = self._pending.pop(id(flow), 0)
            if not row_id:
                return

            try:
                db = self._ensure_db()
                db.update_response(row_id, status, resp_bytes, resp_body)
            except Exception as exc:
                self._warn_once("response_update", f"traffic logger response update failed: {exc}")

        # ── WebSocket ─────────────────────────────────────────────────

        def websocket_start(self, flow: http.HTTPFlow) -> None:
            if not hasattr(flow, "websocket") or flow.websocket is None:
                return
            host = flow.request.host or ""
            if not _is_ai_domain(host):
                return
            path = flow.request.path or ""
            port = flow.request.port
            scheme = "wss" if flow.request.scheme in {"https", "wss"} else "ws"
            provider, is_api = _identify(host, path)
            try:
                db = self._ensure_db()
                db.insert(
                    caller=self._caller(),
                    method="ws-open",
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    provider=provider,
                    is_api=is_api or bool(provider),
                )
            except Exception as exc:
                self._warn_once("ws_open", f"traffic logger websocket_start failed: {exc}")

        def websocket_message(self, flow: http.HTTPFlow) -> None:
            """Log every WebSocket frame (content for known providers)."""
            if not hasattr(flow, "websocket") or flow.websocket is None:
                return

            ws = flow.websocket
            if not ws.messages:
                return

            message = ws.messages[-1]
            host = flow.request.host or ""
            if not _is_ai_domain(host):
                return
            path = flow.request.path or ""
            scheme = "wss" if flow.request.scheme in {"https", "wss"} else "ws"
            port = flow.request.port
            direction = "ws-out" if message.from_client else "ws-in"

            provider, is_api = _identify(host, path)

            raw = message.content
            if isinstance(raw, bytes):
                frame_len = len(raw)
            else:
                frame_len = len(raw.encode("utf-8")) if raw else 0

            # For websocket: log body for any known provider (frames are
            # typically small API payloads), not just confirmed paths.
            body: str | None = None
            if provider:
                cap = self._body_cap()
                try:
                    text = raw.decode("utf-8") if isinstance(raw, bytes) else (raw or "")
                    body = self._redact(text[:cap] if text else None)
                except (UnicodeDecodeError, AttributeError):
                    pass

            try:
                db = self._ensure_db()
                db.insert(
                    caller=self._caller(),
                    method=direction,
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    provider=provider,
                    is_api=is_api or bool(provider),
                    req_bytes=frame_len if message.from_client else None,
                    resp_bytes=frame_len if not message.from_client else None,
                    req_body=body if message.from_client else None,
                    resp_body=body if not message.from_client else None,
                )
            except Exception as exc:
                self._warn_once("ws_message", f"traffic logger websocket_message failed: {exc}")

        def websocket_end(self, flow: http.HTTPFlow) -> None:
            if not hasattr(flow, "websocket") or flow.websocket is None:
                return
            host = flow.request.host or ""
            if not _is_ai_domain(host):
                return
            path = flow.request.path or ""
            port = flow.request.port
            scheme = "wss" if flow.request.scheme in {"https", "wss"} else "ws"
            provider, is_api = _identify(host, path)
            status = getattr(flow.websocket, "close_code", None)
            try:
                db = self._ensure_db()
                db.insert(
                    caller=self._caller(),
                    method="ws-close",
                    scheme=scheme,
                    host=host,
                    port=port,
                    path=path,
                    provider=provider,
                    is_api=is_api or bool(provider),
                    status=status,
                )
            except Exception as exc:
                self._warn_once("ws_close", f"traffic logger websocket_end failed: {exc}")

        def done(self) -> None:
            if self._db:
                self._db.close()

    addons = [TrafficLogger()]

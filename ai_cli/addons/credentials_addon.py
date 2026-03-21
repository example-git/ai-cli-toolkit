"""OAuth credential capture addon for mitmproxy.

Loaded alongside tool-specific addons (currently Claude only) to intercept
OAuth token and profile responses and save credentials to disk.

Self-contained — no ai_cli imports (loaded by mitmdump directly).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CLAUDE_CONFIG_DIR = "~/.claude"
OAUTH_SCOPE_USER_INFERENCE = "user:inference"
OAUTH_TOKEN_PATH = "/v1/oauth/token"
OAUTH_PROFILE_PATH = "/api/oauth/profile"
OAUTH_SCOPES_FIXED = [
    "user:inference",
    "user:mcp_servers",
    "user:profile",
    "user:sessions:claude_code",
]
CREDENTIALS_FILE = ".credentials.json"
CREDENTIALS_ENC_FILE = ".credentials.json.enc"
CREDENTIALS_KEY_FILE = ".credentials.key"


def _log(path_value: str, message: str) -> None:
    if not path_value:
        return
    try:
        p = Path(path_value).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")
    except OSError:
        pass


def _config_dir() -> Path:
    return Path(os.getenv("CLAUDE_CONFIG_DIR", DEFAULT_CLAUDE_CONFIG_DIR)).expanduser()


def _parse_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        p = json.loads(text)
    except json.JSONDecodeError:
        return None
    return p if isinstance(p, dict) else None


def _parse_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s for s in value.split(" ") if s]
    if isinstance(value, list):
        return [s for s in value if isinstance(s, str) and s]
    return []


def _deep_find(payload: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(payload, dict):
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
        for v in payload.values():
            found = _deep_find(v, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _deep_find(item, keys)
            if found is not None:
                return found
    return None


def _extract_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    scopes = _parse_scopes(_deep_find(payload, ("scopes", "scope")))
    if scopes:
        meta["scopes"] = scopes
    sub = _deep_find(payload, ("subscriptionType", "subscription_type"))
    if isinstance(sub, str) and sub:
        meta["subscriptionType"] = sub
    tier = _deep_find(
        payload, ("rateLimitTier", "rate_limit_tier", "rateLimitTierName", "rate_limit_tier_name")
    )
    if isinstance(tier, str) and tier:
        meta["rateLimitTier"] = tier
    return meta


def _sub_from_profile(profile: dict[str, Any]) -> str | None:
    direct = profile.get("subscriptionType")
    if isinstance(direct, str) and direct:
        return direct
    org = profile.get("organization")
    if not isinstance(org, dict):
        return None
    org_type = org.get("organization_type")
    if not isinstance(org_type, str):
        return None
    return {
        "claude_max": "max",
        "claude_pro": "pro",
        "claude_enterprise": "enterprise",
        "claude_team": "team",
    }.get(org_type)


def _read_creds() -> dict[str, Any]:
    path = _config_dir() / CREDENTIALS_FILE
    try:
        return _parse_json(path.read_text(encoding="utf-8")) or {}
    except OSError:
        return {}


def _ensure_key(log_file: str) -> Path | None:
    key_path = _config_dir() / CREDENTIALS_KEY_FILE
    if key_path.exists():
        return key_path
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(f"{os.urandom(32).hex()}\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
        return key_path
    except OSError as exc:
        _log(log_file, f"Failed creating credentials key: {exc}")
        return None


def _write_encrypted(data: dict[str, Any], log_file: str) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        return
    key_path = _ensure_key(log_file)
    if not key_path:
        return
    try:
        proc = subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-a", "-pass", f"file:{key_path}"],
            input=json.dumps(data),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError:
        return
    if proc.returncode != 0 or not proc.stdout:
        return
    enc_path = _config_dir() / CREDENTIALS_ENC_FILE
    try:
        enc_path.write_text(proc.stdout, encoding="utf-8")
        os.chmod(enc_path, 0o600)
    except OSError:
        pass


def _write_oauth(oauth: dict[str, Any], log_file: str) -> None:
    path = _config_dir() / CREDENTIALS_FILE
    oauth["scopes"] = list(OAUTH_SCOPES_FIXED)
    data = {"claudeAiOauth": oauth}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, 0o600)
        _log(log_file, f"Saved claudeAiOauth to {path}")
        _write_encrypted(data, log_file)
    except OSError as exc:
        _log(log_file, f"Failed to save credentials: {exc}")


def _build_oauth(
    bearer: str | None, existing: dict[str, Any] | None, meta: dict[str, Any] | None
) -> dict[str, Any]:
    oauth = dict(existing or {})
    meta = meta or {}
    if bearer and not isinstance(oauth.get("accessToken"), str):
        oauth["accessToken"] = bearer
    scopes = (
        _parse_scopes(meta.get("scopes"))
        or _parse_scopes(oauth.get("scopes"))
        or [OAUTH_SCOPE_USER_INFERENCE]
    )
    if OAUTH_SCOPE_USER_INFERENCE not in scopes:
        scopes.append(OAUTH_SCOPE_USER_INFERENCE)
    oauth["scopes"] = scopes
    if not isinstance(oauth.get("expiresAt"), int):
        oauth["expiresAt"] = int(time.time() * 1000 + 3600000)
    if "subscriptionType" in meta and isinstance(meta["subscriptionType"], str):
        oauth["subscriptionType"] = meta["subscriptionType"]
    elif "subscriptionType" not in oauth:
        oauth["subscriptionType"] = None
    if "rateLimitTier" in meta and isinstance(meta["rateLimitTier"], str):
        oauth["rateLimitTier"] = meta["rateLimitTier"]
    if "refreshToken" not in oauth:
        oauth["refreshToken"] = None
    return oauth


def _extract_bearer(flow: Any) -> str | None:
    auth = flow.request.headers.get("authorization", "")
    if not isinstance(auth, str):
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        return None
    token = parts[1].strip()
    return token if token else None


# ---------------------------------------------------------------------------
# mitmproxy addon
# ---------------------------------------------------------------------------

from mitmproxy import ctx, http  # type: ignore[import-untyped]


class CredentialCaptureAddon:
    """Capture OAuth credentials from Claude API responses."""

    def load(self, loader: Any) -> None:
        loader.add_option("wrapper_log_file", str, "", "Path to wrapper log file.")

    def response(self, flow: http.HTTPFlow) -> None:
        log_file = getattr(ctx.options, "wrapper_log_file", "") or ""
        path = flow.request.path or ""
        method = flow.request.method.upper()

        # Bootstrap: capture bearer token from any authenticated request
        existing_doc = _read_creds()
        existing_oauth = existing_doc.get("claudeAiOauth")
        if not isinstance(existing_oauth, dict) or not isinstance(
            existing_oauth.get("accessToken"), str
        ):
            bearer = _extract_bearer(flow)
            if bearer:
                oauth = _build_oauth(
                    bearer, existing_oauth if isinstance(existing_oauth, dict) else {}, None
                )
                if isinstance(oauth.get("accessToken"), str):
                    _write_oauth(oauth, log_file)

        # Token endpoint
        if method == "POST" and OAUTH_TOKEN_PATH in path:
            resp = flow.response
            if not resp or resp.status_code != 200:
                return
            resp_data = _parse_json(resp.get_text(strict=False))
            if not resp_data:
                return
            req_data = _parse_json(flow.request.get_text(strict=False)) or {}
            existing_doc = _read_creds()
            existing_oauth = existing_doc.get("claudeAiOauth")
            if not isinstance(existing_oauth, dict):
                existing_oauth = {}
            access_token = resp_data.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return
            meta = _extract_metadata(resp_data)
            oauth = _build_oauth(_extract_bearer(flow), existing_oauth, meta)
            oauth["accessToken"] = access_token
            refresh = resp_data.get("refresh_token")
            if not isinstance(refresh, str) or not refresh:
                refresh = req_data.get("refresh_token")
            if isinstance(refresh, str) and refresh:
                oauth["refreshToken"] = refresh
            expires_in = resp_data.get("expires_in")
            if isinstance(expires_in, (int, float)) and expires_in > 0:
                oauth["expiresAt"] = int(time.time() * 1000 + float(expires_in) * 1000)
            _write_oauth(oauth, log_file)
            return

        # Profile endpoint
        if method == "GET" and OAUTH_PROFILE_PATH in path:
            resp = flow.response
            if not resp or resp.status_code != 200:
                return
            profile = _parse_json(resp.get_text(strict=False))
            if not profile:
                return
            existing_doc = _read_creds()
            existing_oauth = existing_doc.get("claudeAiOauth")
            if not isinstance(existing_oauth, dict):
                existing_oauth = {}
            meta = _extract_metadata(profile)
            if "subscriptionType" not in meta:
                inferred = _sub_from_profile(profile)
                if inferred:
                    meta["subscriptionType"] = inferred
            oauth = _build_oauth(_extract_bearer(flow), existing_oauth, meta)
            changed = any(oauth.get(k) != v for k, v in meta.items())
            if not existing_oauth and isinstance(oauth.get("accessToken"), str):
                changed = True
            if changed:
                _write_oauth(oauth, log_file)
            return

        # Generic OAuth metadata endpoints
        if "/api/oauth/" in path or "/v1/oauth/" in path:
            resp = flow.response
            if not resp or resp.status_code >= 400:
                return
            data = _parse_json(resp.get_text(strict=False))
            if not data:
                return
            meta = _extract_metadata(data)
            existing_doc = _read_creds()
            existing_oauth = existing_doc.get("claudeAiOauth")
            if not isinstance(existing_oauth, dict):
                existing_oauth = {}
            oauth = _build_oauth(_extract_bearer(flow), existing_oauth, meta)
            changed = any(oauth.get(k) != v for k, v in meta.items())
            if not existing_oauth and isinstance(oauth.get("accessToken"), str):
                changed = True
            if changed:
                _write_oauth(oauth, log_file)


addons = [CredentialCaptureAddon()]

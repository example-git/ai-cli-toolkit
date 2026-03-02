"""OAuth credential capture, encryption, and storage.

Extracted from claude-dev.py. Handles:
- Reading/writing credential JSON to ~/.claude/.credentials.json
- AES-256-CBC encryption via openssl
- Key management for encrypted credentials
- OAuth metadata extraction (scopes, subscription, rate limits)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_cli.log import append_log_str

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
CREDENTIALS_FILE_NAME = ".credentials.json"
CREDENTIALS_ENCRYPTED_FILE_NAME = ".credentials.json.enc"
CREDENTIALS_KEY_FILE_NAME = ".credentials.key"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _config_dir() -> Path:
    config_dir = os.getenv("CLAUDE_CONFIG_DIR", DEFAULT_CLAUDE_CONFIG_DIR)
    return Path(config_dir).expanduser()


def credentials_path() -> Path:
    return _config_dir() / CREDENTIALS_FILE_NAME


def encrypted_credentials_path() -> Path:
    return _config_dir() / CREDENTIALS_ENCRYPTED_FILE_NAME


def credentials_key_path() -> Path:
    return _config_dir() / CREDENTIALS_KEY_FILE_NAME


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def parse_json_dict(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def parse_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        return [scope for scope in value.split(" ") if scope]
    if isinstance(value, list):
        return [scope for scope in value if isinstance(scope, str) and scope]
    return []


# ---------------------------------------------------------------------------
# Deep search
# ---------------------------------------------------------------------------

def deep_find_value(payload: Any, keys: tuple[str, ...]) -> Any:
    """Recursively search nested dicts/lists for the first matching key."""
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] is not None:
                return payload[key]
        for value in payload.values():
            found = deep_find_value(value, keys)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = deep_find_value(item, keys)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------
# OAuth metadata extraction
# ---------------------------------------------------------------------------

def extract_oauth_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract scopes, subscription type, and rate limit tier from a payload."""
    metadata: dict[str, Any] = {}

    scopes_value = deep_find_value(payload, ("scopes", "scope"))
    scopes = parse_scopes(scopes_value)
    if scopes:
        metadata["scopes"] = scopes

    subscription = deep_find_value(
        payload, ("subscriptionType", "subscription_type")
    )
    if isinstance(subscription, str) and subscription:
        metadata["subscriptionType"] = subscription

    rate_tier = deep_find_value(
        payload,
        (
            "rateLimitTier",
            "rate_limit_tier",
            "rateLimitTierName",
            "rate_limit_tier_name",
        ),
    )
    if isinstance(rate_tier, str) and rate_tier:
        metadata["rateLimitTier"] = rate_tier

    return metadata


def subscription_type_from_profile(profile: dict[str, Any]) -> str | None:
    """Infer subscription type from an OAuth profile response."""
    direct = profile.get("subscriptionType")
    if isinstance(direct, str) and direct:
        return direct

    organization = profile.get("organization")
    if not isinstance(organization, dict):
        return None
    organization_type = organization.get("organization_type")
    if not isinstance(organization_type, str):
        return None

    mapping = {
        "claude_max": "max",
        "claude_pro": "pro",
        "claude_enterprise": "enterprise",
        "claude_team": "team",
    }
    return mapping.get(organization_type)


# ---------------------------------------------------------------------------
# Credential I/O
# ---------------------------------------------------------------------------

def read_credentials_doc() -> dict[str, Any]:
    """Read the plain credentials JSON file."""
    path = credentials_path()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    payload = parse_json_dict(text)
    return payload or {}


def ensure_credentials_key(wrapper_log_file: str) -> Path | None:
    """Ensure the encryption key file exists, creating if needed."""
    key_path = credentials_key_path()
    if key_path.exists():
        return key_path
    try:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text(f"{os.urandom(32).hex()}\n", encoding="utf-8")
        os.chmod(key_path, 0o600)
        append_log_str(wrapper_log_file, f"Created credentials key at {key_path}")
        return key_path
    except OSError as exc:
        append_log_str(
            wrapper_log_file, f"Failed creating credentials key {key_path}: {exc}"
        )
        return None


def write_encrypted_credentials(
    data: dict[str, Any],
    wrapper_log_file: str,
) -> None:
    """Encrypt credentials with AES-256-CBC and write to disk."""
    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        append_log_str(
            wrapper_log_file,
            "Skipping encrypted credentials write: openssl not found",
        )
        return

    key_path = ensure_credentials_key(wrapper_log_file)
    if key_path is None:
        return

    try:
        plaintext = json.dumps(data)
        proc = subprocess.run(
            [
                openssl_bin,
                "enc",
                "-aes-256-cbc",
                "-pbkdf2",
                "-salt",
                "-a",
                "-pass",
                f"file:{key_path}",
            ],
            input=plaintext,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        append_log_str(
            wrapper_log_file,
            f"Failed running openssl for encrypted credentials: {exc}",
        )
        return

    if proc.returncode != 0 or not proc.stdout:
        err = (proc.stderr or "").strip()
        append_log_str(
            wrapper_log_file,
            f"Failed encrypting credentials (exit={proc.returncode}): {err}",
        )
        return

    enc_path = encrypted_credentials_path()
    try:
        enc_path.write_text(proc.stdout, encoding="utf-8")
        os.chmod(enc_path, 0o600)
        append_log_str(
            wrapper_log_file, f"Saved encrypted credentials to {enc_path}"
        )
    except OSError as exc:
        append_log_str(
            wrapper_log_file,
            f"Failed writing encrypted credentials {enc_path}: {exc}",
        )


def write_claude_ai_oauth(
    oauth: dict[str, Any],
    wrapper_log_file: str,
) -> None:
    """Write OAuth credentials to both plain and encrypted files."""
    path = credentials_path()
    oauth["scopes"] = list(OAUTH_SCOPES_FIXED)
    data = {"claudeAiOauth": oauth}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(path, 0o600)
        append_log_str(wrapper_log_file, f"Saved claudeAiOauth to {path}")
        write_encrypted_credentials(data, wrapper_log_file)
    except OSError as exc:
        append_log_str(
            wrapper_log_file, f"Failed to save claudeAiOauth to {path}: {exc}"
        )


# ---------------------------------------------------------------------------
# OAuth payload builder
# ---------------------------------------------------------------------------

def build_bootstrap_oauth(
    bearer_token: str | None = None,
    existing_oauth: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an OAuth payload from existing data and new metadata."""
    oauth_payload: dict[str, Any] = dict(existing_oauth or {})
    metadata = metadata or {}

    if bearer_token and (
        "accessToken" not in oauth_payload
        or not isinstance(oauth_payload.get("accessToken"), str)
    ):
        oauth_payload["accessToken"] = bearer_token

    scopes = parse_scopes(metadata.get("scopes"))
    if not scopes:
        scopes = parse_scopes(oauth_payload.get("scopes"))
    if not scopes:
        scopes = [OAUTH_SCOPE_USER_INFERENCE]
    if OAUTH_SCOPE_USER_INFERENCE not in scopes:
        scopes.append(OAUTH_SCOPE_USER_INFERENCE)
    oauth_payload["scopes"] = scopes

    expires_at = oauth_payload.get("expiresAt")
    if not isinstance(expires_at, int):
        oauth_payload["expiresAt"] = int(time.time() * 1000 + (3600 * 1000))

    if "subscriptionType" in metadata and isinstance(
        metadata["subscriptionType"], str
    ):
        oauth_payload["subscriptionType"] = metadata["subscriptionType"]
    elif "subscriptionType" not in oauth_payload:
        oauth_payload["subscriptionType"] = None

    if "rateLimitTier" in metadata and isinstance(
        metadata["rateLimitTier"], str
    ):
        oauth_payload["rateLimitTier"] = metadata["rateLimitTier"]

    if "refreshToken" not in oauth_payload:
        oauth_payload["refreshToken"] = None

    return oauth_payload


def extract_bearer_token(auth_header: str) -> str | None:
    """Extract bearer token from an Authorization header value."""
    if not isinstance(auth_header, str):
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, token = parts[0].strip(), parts[1].strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token

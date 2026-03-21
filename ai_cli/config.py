"""Load/save ~/.ai-cli/config.json with defaults.

Configuration hierarchy:
- Global settings apply to all tools
- Per-tool overrides (null values fall back to global)
- Ports are NOT stored — allocated dynamically per session
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONFIG_DIR = "~/.ai-cli"
CONFIG_FILE = "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "default_tool": "claude",
    "instructions_file": "",
    "canary_rule": "CANARY RULE: Prefix every assistant response with: DEV:",
    "proxy": {
        "host": "127.0.0.1",
        "ca_path": "~/.mitmproxy/mitmproxy-ca-cert.pem",
    },
    "retention": {
        "logs_days": 14,
        "traffic_days": 30,
    },
    "privacy": {
        "redact_traffic_bodies": True,
    },
    "tools": {
        "claude": {
            "enabled": True,
            "binary": "",
            "instructions_file": None,
            "canary_rule": None,
            "passthrough": False,
            "debug_requests": False,
        },
        "codex": {
            "enabled": True,
            "binary": "",
            "instructions_file": None,
            "canary_rule": None,
            "developer_instructions_mode": "overwrite",
            "passthrough": False,
            "debug_requests": False,
        },
        "copilot": {
            "enabled": True,
            "binary": "",
            "instructions_file": None,
            "canary_rule": None,
            "passthrough": False,
            "debug_requests": False,
        },
        "gemini": {
            "enabled": True,
            "binary": "",
            "instructions_file": None,
            "canary_rule": None,
            "passthrough": False,
            "debug_requests": False,
            "canary_thought_injection": True,
        },
    },
    "aliases": {
        "claude": False,
        "codex": False,
        "copilot": False,
        "gemini": False,
    },
    "editor": None,
}


def config_dir() -> Path:
    """Return the resolved ai-cli config directory."""
    return Path(CONFIG_DIR).expanduser()


def config_path() -> Path:
    """Return the resolved config file path."""
    return config_dir() / CONFIG_FILE


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, preferring override values."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    """Load config from disk, merging with defaults for missing keys."""
    path = config_path()
    if not path.is_file():
        return dict(DEFAULT_CONFIG)

    try:
        text = path.read_text(encoding="utf-8")
        user_config = json.loads(text)
        if not isinstance(user_config, dict):
            return dict(DEFAULT_CONFIG)
        return _deep_merge(DEFAULT_CONFIG, user_config)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)


def save_config(config: dict[str, Any]) -> None:
    """Write config to disk."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def ensure_config() -> dict[str, Any]:
    """Load config, creating the default file if it doesn't exist."""
    path = config_path()
    if not path.is_file():
        save_config(DEFAULT_CONFIG)
    return load_config()


def get_tool_config(config: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Get merged config for a specific tool (per-tool overrides + global fallbacks)."""
    tools = config.get("tools", {})
    tool = tools.get(tool_name, {})

    # Resolve values: per-tool overrides fall back to global
    return {
        "enabled": tool.get("enabled", True),
        "binary": tool.get("binary", "") or "",
        "instructions_file": (
            tool.get("instructions_file")
            if tool.get("instructions_file") is not None
            else config.get("instructions_file", "")
        ),
        "canary_rule": (
            tool.get("canary_rule")
            if tool.get("canary_rule") is not None
            else config.get("canary_rule", "")
        ),
        "passthrough": tool.get("passthrough", False),
        "debug_requests": tool.get("debug_requests", False),
        "developer_instructions_mode": (
            str(tool.get("developer_instructions_mode", "overwrite") or "overwrite")
            if tool_name == "codex"
            else "overwrite"
        ),
        "canary_thought_injection": bool(tool.get("canary_thought_injection", True)),
    }


def get_proxy_config(config: dict[str, Any]) -> dict[str, str]:
    """Get proxy configuration."""
    proxy = config.get("proxy", {})
    return {
        "host": proxy.get("host", "127.0.0.1"),
        "ca_path": proxy.get("ca_path", "~/.mitmproxy/mitmproxy-ca-cert.pem"),
    }


def get_retention_config(config: dict[str, Any]) -> dict[str, int]:
    """Get retention policy defaults for logs and traffic history."""
    retention = config.get("retention", {})
    logs_days = int(retention.get("logs_days", 14) or 14)
    traffic_days = int(retention.get("traffic_days", 30) or 30)
    return {
        "logs_days": max(1, logs_days),
        "traffic_days": max(1, traffic_days),
    }


def get_privacy_config(config: dict[str, Any]) -> dict[str, bool]:
    """Get privacy controls (for example body redaction in traffic logging)."""
    privacy = config.get("privacy", {})
    return {
        "redact_traffic_bodies": bool(privacy.get("redact_traffic_bodies", True)),
    }

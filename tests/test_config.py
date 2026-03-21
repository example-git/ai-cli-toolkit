from __future__ import annotations

import json

from ai_cli import config


def test_deep_merge_recursively_applies_overrides() -> None:
    base = {
        "proxy": {"host": "127.0.0.1", "ca_path": "a"},
        "tools": {"codex": {"enabled": True, "binary": "codex"}},
    }
    override = {
        "proxy": {"host": "0.0.0.0"},
        "tools": {"codex": {"binary": "/tmp/codex"}},
    }

    merged = config._deep_merge(base, override)

    assert merged["proxy"] == {"host": "0.0.0.0", "ca_path": "a"}
    assert merged["tools"]["codex"] == {"enabled": True, "binary": "/tmp/codex"}


def test_load_config_merges_with_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    cfg_path = config.config_path()
    cfg_path.write_text(
        json.dumps(
            {
                "default_tool": "codex",
                "proxy": {"host": "0.0.0.0"},
                "tools": {"codex": {"binary": "~/bin/codex"}},
            }
        ),
        encoding="utf-8",
    )

    loaded = config.load_config()

    assert loaded["default_tool"] == "codex"
    assert loaded["proxy"]["host"] == "0.0.0.0"
    assert loaded["proxy"]["ca_path"] == "~/.mitmproxy/mitmproxy-ca-cert.pem"
    assert loaded["tools"]["codex"]["binary"] == "~/bin/codex"
    assert "claude" in loaded["tools"]


def test_load_config_invalid_json_returns_defaults(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    config.config_path().write_text("{not-json", encoding="utf-8")

    loaded = config.load_config()

    assert loaded == config.DEFAULT_CONFIG


def test_ensure_config_creates_default_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))

    loaded = config.ensure_config()

    assert config.config_path().is_file()
    assert loaded == config.DEFAULT_CONFIG


def test_get_tool_config_applies_global_fallbacks() -> None:
    cfg = {
        "instructions_file": "/global.txt",
        "canary_rule": "GLOBAL",
        "tools": {
            "codex": {
                "enabled": False,
                "binary": "codex-bin",
                "instructions_file": None,
                "canary_rule": None,
                "passthrough": True,
                "debug_requests": True,
            }
        },
    }

    resolved = config.get_tool_config(cfg, "codex")

    assert resolved == {
        "enabled": False,
        "binary": "codex-bin",
        "instructions_file": "/global.txt",
        "canary_rule": "GLOBAL",
        "passthrough": True,
        "debug_requests": True,
        "developer_instructions_mode": "overwrite",
        "canary_thought_injection": True,
    }


def test_get_proxy_config_returns_defaults_for_missing_keys() -> None:
    proxy = config.get_proxy_config({"proxy": {"host": "10.0.0.2"}})

    assert proxy == {
        "host": "10.0.0.2",
        "ca_path": "~/.mitmproxy/mitmproxy-ca-cert.pem",
    }

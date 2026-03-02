# Config Reference

Config file path:

- `~/.ai-cli/config.json`

## Defaults

```json
{
  "version": 1,
  "default_tool": "claude",
  "instructions_file": "",
  "canary_rule": "CANARY RULE: Prefix every assistant response with: DEV:",
  "proxy": {
    "host": "127.0.0.1",
    "ca_path": "~/.mitmproxy/mitmproxy-ca-cert.pem"
  },
  "retention": {
    "logs_days": 14,
    "traffic_days": 30
  },
  "privacy": {
    "redact_traffic_bodies": true
  },
  "tools": {
    "claude": {
      "enabled": true,
      "binary": "",
      "instructions_file": null,
      "canary_rule": null,
      "passthrough": false,
      "debug_requests": false
    },
    "codex": {
      "enabled": true,
      "binary": "",
      "instructions_file": null,
      "canary_rule": null,
      "developer_instructions_mode": "overwrite",
      "passthrough": false,
      "debug_requests": false
    },
    "copilot": {
      "enabled": true,
      "binary": "",
      "instructions_file": null,
      "canary_rule": null,
      "passthrough": false,
      "debug_requests": false
    },
    "gemini": {
      "enabled": true,
      "binary": "",
      "instructions_file": null,
      "canary_rule": null,
      "passthrough": false,
      "debug_requests": false
    }
  },
  "aliases": {
    "claude": false,
    "codex": false,
    "copilot": false,
    "gemini": false
  },
  "editor": null
}
```

## Override Rules

- Tool-level `instructions_file` and `canary_rule` override global values only when non-`null`.
- `developer_instructions_mode` applies only to `codex` and supports `overwrite`, `append`, `prepend`.
- Retention days are clamped to at least `1`.
- Ports are allocated dynamically per session and are not stored in config.

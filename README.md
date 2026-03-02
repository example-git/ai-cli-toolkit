# AI Cli Toolkit

AI Cli Toolkit is a unified wrapper around multiple AI coding CLIs:

- Claude Code
- OpenAI Codex CLI
- GitHub Copilot CLI
- Gemini CLI

It runs each tool through a managed mitmproxy layer to inject instructions consistently, handle per-tool request formats, and keep session tooling in one place.

## Current Status

Implemented and smoke-tested:

- Multi-tool dispatch and alias-style argv routing
- Per-tool instruction injection addons
- Dynamic per-session proxy ports
- Session extraction across multiple agents
- Startup recent-context injection (cwd-scoped, cross-agent)
- Interactive menu, update command, completions, installer, statusline

## Key Features

- Single entrypoint: `ai-cli <tool> [DIR] [args...]`
- Optional aliasing so `claude`, `codex`, `copilot`, `gemini` route through the wrapper
- Layered instruction composition:
  - Canary
  - Base
  - Per-tool
  - Per-project
  - User custom
- Agent-agnostic session inspection:
  - `ai-cli session --list`
  - `ai-cli session --agent claude --tail 20`
  - `ai-cli session --all --grep "keyword"`
- Startup continuity context:
  - At tool launch, recent cwd-matching context is built from session logs
  - The context block is included in startup prompt text and printed at init
- Per-tool updater:
  - `ai-cli update --list`
  - `ai-cli update codex`
  - `ai-cli update --all`
## Requirements

- Python `>=3.8`
- `mitmproxy` (auto-installed on first run if missing)
- Wrapped tool binaries installed (`claude`, `codex`, `copilot`, `gemini`)

## Install

### Preferred

```bash
bash install.sh
```

Useful flags:

- `--reinstall`
- `--alias-all`
- `--alias <tool>` (repeatable)
- `--no-alias`
- `--auto-install-deps` (allow installer to install required system deps like `tmux`)
- `--yes` (assume yes for interactive prompts)
- `--non-interactive`

### Manual dev install

```bash
python3 -m pip install --user -e .
```

## CLI Usage

```bash
ai-cli <tool> [DIR] [args...]
ai-cli menu
ai-cli status
ai-cli system [tool]
ai-cli edit-signal
ai-cli session [options]
ai-cli update [tool|--all]
```

Directory launch behavior:

- If the first argument after `<tool>` is an existing directory, ai-cli launches the wrapped tool in that directory.
- This applies to both:
  - `ai-cli claude /path/to/project`
  - `claude /path/to/project` (when `claude` is aliased to ai-cli wrapper)

`ai-cli menu` uses curses in an interactive TTY and falls back to a non-interactive status output otherwise.

Tools:

- `claude`
- `codex`
- `copilot`
- `gemini`

Note for Codex users:

- In wrapped `ai-cli codex` sessions, `Ctrl+G` external-editor behavior is enabled
  by default.
- To disable Codex external-editor behavior, set:
  `AI_CLI_CODEX_DISABLE_EXTERNAL_EDITOR=1`

## Configuration

Config file:

- `~/.ai-cli/config.json`

Key fields:

- Global `instructions_file` and `canary_rule`
- Proxy host and CA path
- Retention policy:
  - `retention.logs_days`
  - `retention.traffic_days`
- Privacy policy:
  - `privacy.redact_traffic_bodies`
- Per-tool overrides for:
  - `enabled`
  - `binary`
  - `instructions_file`
  - `canary_rule`
  - `passthrough`
  - `debug_requests`
  - `developer_instructions_mode` (Codex: `overwrite`, `append`, `prepend`; default `overwrite`)
- Alias state tracking

Codex prompt handling (`developer_instructions_mode=overwrite`) builds a sectioned developer message:
- `<GLOBAL GUIDELINES>` from global user instructions file (`instructions_file`)
- `<DEVELOPER PROMPT>` from codex-specific instructions
- recurring runtime blocks (permissions/apps/collaboration mode) preserved in tagged recurring sections

In `ai-mux` sessions:
- `F7` opens the global prompt file in your editor (`VISUAL`/`EDITOR`, fallback `nano`/`vi`/`vim`)
- `F8` opens the active tool's prompt file
- Codex injections read these files per request, so file edits apply to subsequent turns in the same conversation

## Instruction Files

Instruction layering is assembled at runtime:

1. Canary rule
2. Base template (`templates/base_instructions.txt` or `~/.ai-cli/base_instructions.txt`)
3. Per-tool (`~/.ai-cli/instructions/<tool>.txt`)
4. Project (`./.ai-cli/project_instructions.txt`)
5. User (`~/.ai-cli/system_instructions.txt` or configured file)

Edit quickly:

```bash
ai-cli system
ai-cli system codex
```

If the wrapper is already running, `ai-cli edit-signal` triggers the edit signal path.

## Retention And Privacy

- Wrapper startup runs best-effort housekeeping:
  - Prunes old wrapper logs from `~/.ai-cli/logs` using `retention.logs_days`
  - Prunes old rows from `~/.ai-cli/traffic.db` using `retention.traffic_days`
- Traffic body capture is redacted by default (`privacy.redact_traffic_bodies = true`) for common secret/token patterns.

## Session Tooling

List all discovered sessions:

```bash
ai-cli session --list
```

Show merged timeline:

```bash
ai-cli session --all --tail 50
```

Filter by agent:

```bash
ai-cli session --agent codex --tail 30
```

Filter by text:

```bash
ai-cli session --all --grep "statusline"
```

## Shell Completions

Installed by `install.sh`:

- Zsh: `completions/_ai-cli`
- Bash: `completions/ai-cli.bash`

## Statusline

A multi-tool aware statusline command is installed to:

- `~/.claude/statusline-command.sh`

Installer updates `~/.claude/settings.json` to point `statusLine` at the script.

## Development

Basic checks:

```bash
python3 -m compileall ai_cli
python3 -m pip install -e '.[dev]'
pytest
pre-commit run --all-files
python3 -m ai_cli --help
python3 -m ai_cli session --help
python3 -m ai_cli update --help
bash -n install.sh
```

CI is configured in `.github/workflows/ci.yml` and runs linting, typing, tests, and compile checks.

## Troubleshooting

- Proxy launches but tool cannot reach APIs:
  - Confirm CA exists at `~/.mitmproxy/mitmproxy-ca-cert.pem`
  - Check `~/.ai-cli/logs/*.mitmdump.log` for TLS/connection errors
- Codex injection/traffic capture stopped:
  - Ensure `~/.codex/config.toml` has `[network] allow_upstream_proxy = true` and `mitm = false`
- No traffic rows appear:
  - Verify tool was launched through `ai-cli`
  - Check retention config is not too aggressive (`retention.traffic_days`)
- Installer fails on missing `tmux`:
  - Re-run with `--auto-install-deps` (or install `tmux` manually first)

## Project Layout

- `ai_cli/` core package
- `ai_cli/tools/` tool specs and registry
- `ai_cli/addons/` mitmproxy injection addons
- `templates/` base instruction template
- `completions/` shell completion scripts
- `statusline/` statusline command script
- `reference/` preserved source/reference material

## README Maintenance

This README is intended to be a living operational guide.

When behavior changes, update this file in the same change set for:

- CLI commands or flags
- Installer behavior
- Config schema/defaults
- Session discovery/parsing behavior
- Prompt/instruction composition behavior

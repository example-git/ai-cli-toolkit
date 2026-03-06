<p align="center">
  <img src="docs/assets/ai-cli-banner.png" alt="AI Cli Toolkit banner" width="980" />
</p>

# AI Cli Toolkit

AI Cli Toolkit is a unified wrapper around multiple AI coding CLIs:

- Claude Code
- OpenAI Codex CLI
- GitHub Copilot CLI
- Gemini CLI

It runs each tool through a managed mitmproxy layer to inject instructions consistently, handle per-tool request formats, and keep session tooling in one place.

## Early Version Notice

---

This is an early `0.2.0` release. Features are still evolving, behavior may change, and some workflows may be incomplete or unstable.

Use this project at your own risk. You are responsible for how you use it, including compliance with platform policies, terms of service, and applicable laws. The maintainers are not liable for misuse, data loss, account issues, service interruptions, or other consequences resulting from use of this tool.

---

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
- Remote session support:
  - Launch tools on remote hosts: `ai-cli codex user@host:/path/to/project`
  - Packages and deploys ai-mux, prompt layers, and editor launcher to the remote
  - Syncs edited prompt files back on session exit
- In-session prompt editor (F5–F8):
  - F5: global instructions (`system_instructions.txt`)
  - F6: base instructions (`base_instructions.txt`)
  - F7: per-tool instructions (`instructions/<tool>.txt`)
  - F8: per-project instructions (`.ai-cli/project_instructions.txt`)
  - Status bar shows shortcut hints; prefix with `C-]` to trigger
- ai-mux tmux orchestrator:
  - Per-tool tmux sockets (`--socket-name`)
  - Auto-detach stale clients on reconnect (scoped per-session)
  - Cross-platform: arm64 macOS + x86_64 Linux binaries
## Requirements

- Python `>=3.12`
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
ai-cli <tool> user@host:/remote/dir [args...]   # remote session
ai-cli menu
ai-cli status
ai-cli system [tool]
ai-cli system prompt [model]
ai-cli prompt-edit <global|tool> [tool]
ai-cli history [options]
ai-cli session [options]  # alias for history
ai-cli traffic [options]
ai-cli cleanup [options]
ai-cli update [tool|--all]
ai-cli completions generate [--shell bash|zsh|all]
```

Directory launch behavior:

- If the first argument after `<tool>` is an existing directory, ai-cli launches the wrapped tool in that directory.
- If the argument is `user@host:/path`, ai-cli packages and deploys a remote session via SSH/rsync.
- This applies to both:
  - `ai-cli claude /path/to/project`
  - `claude /path/to/project` (when `claude` is aliased to ai-cli wrapper)
  - `ai-cli codex user@server:/home/user/project`

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
- `F5` opens the global prompt file in your editor (`VISUAL`/`EDITOR`, fallback `nano`/`vi`/`vim`)
- `F6` opens the base instructions file
- `F7` opens the active tool's prompt file
- `F8` opens the project-level prompt file
- All F-key bindings require `C-]` prefix (shown in status bar)
- Codex injections read these files per request, so file edits apply to subsequent turns in the same conversation

## Instruction Files

Instruction sources:

1. Canary rule
2. Base template (`templates/base_instructions.txt` or `~/.ai-cli/base_instructions.txt`)
3. Per-tool (`~/.ai-cli/instructions/<tool>.txt`)
4. Project (`./.ai-cli/project_instructions.txt`)
5. User (`~/.ai-cli/system_instructions.txt` or configured file)

Runtime behavior:

- `compose_instructions()` builds the 5-layer text for wrapper logging/hash visibility.
- Addons inject using the global instructions file + canary rule.
- For Codex, `~/.ai-cli/instructions/codex.txt` is also used as the `<DEVELOPER PROMPT>` section when `developer_instructions_mode=overwrite`.
- Startup recent-context is appended to the canary rule unless disabled.

Edit quickly:

```bash
ai-cli system
ai-cli system codex
ai-cli prompt-edit global
ai-cli prompt-edit tool codex
```

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

`install.sh` copies completion source files from this repo into shell completion directories:

- Zsh source: `completions/_ai-cli` -> `~/.oh-my-zsh/custom/completions/_ai-cli` (or `~/.zsh/completions/_ai-cli`)
- Bash source: `completions/ai-cli.bash` -> `~/.local/share/bash-completion/completions/ai-cli`

You can also generate scripts directly:

```bash
ai-cli completions generate --shell all
```

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

CI is configured in `.github/workflows/ci.yml` and currently runs `pre-commit` and `pytest`.

## Docs

Additional user docs live under `docs/`:

- `docs/index.md`
- `docs/getting-started.md`
- `docs/cli-reference.md`
- `docs/config-reference.md`
- `docs/operations-runbook.md`
- `docs/privacy-data-handling.md`

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
- `ai_cli/bin/` compiled ai-mux binaries (arm64 macOS, x86_64 Linux)
- `ai_cli/remote.py` remote session spec and SSH runner
- `ai_cli/remote_package.py` remote package builder, tmux conf, prompt sync
- `ai_cli/prompt_editor_launcher.py` F5–F8 prompt editor (deployed to remote)
- `mux/` Rust source for ai-mux tmux orchestrator
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

# Operations Runbook

## Install Side Effects

- `install.sh` can update shell rc files for aliases/completions.
- `install.sh` installs completion scripts to user shell completion directories.
- `install.sh` installs/updates `~/.claude/statusline-command.sh`.

## Proxy Lifecycle

- Wrapper launches `mitmdump` with tool-specific addons.
- Proxy host/CA path come from config (`proxy.host`, `proxy.ca_path`).
- Proxy port is dynamically allocated per run.
- If proxy startup fails, wrapper can continue with proxy/injection disabled for that run.

## Common Failure Modes

- Missing or unreadable CA cert path.
- `mitmdump` binary unavailable and bootstrap install failing.
- Network/proxy settings in downstream tool block upstream proxy usage.
- Detached tmux sessions leaving stale proxy processes.

## Recovery Steps

- Check wrapper logs in `~/.ai-cli/logs/*.log` and `*.mitmdump.log`.
- Run `ai-cli cleanup --list` and `ai-cli cleanup --all -y` when needed.
- Reconfirm tool-specific proxy settings after updates.
- Re-run with explicit status checks: `ai-cli status`.

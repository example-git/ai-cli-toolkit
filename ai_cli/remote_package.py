"""Remote package — assemble an isolated $HOME for tool sessions on remote hosts.

When launching a tool on a remote host, this module:
1. Computes a deterministic session directory on the remote.
2. Builds a manifest of local config/credential/instruction files to include.
3. Pushes the package via rsync (single invocation from a staging tmpdir).
4. Provides a probe to detect whether a tmux session is already running.
5. Pulls session artifacts (logs, projects) back to the local host after exit.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ai_cli.instructions import ensure_project_instructions_file
from ai_cli.remote import RemoteSpec, print_sync_status

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

_SSH_OPTS = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
_RSYNC_CHMOD = "Du=rwx,Dgo=,Fu=rw,Fgo="
_SKIP_FILE_NAMES = {".DS_Store"}
_MUX_SOURCE_FILES = ("Cargo.toml", "Cargo.lock", "src")

_TOOL_PACKAGE_FILES: dict[str, list[tuple[str, str]]] = {
    "claude": [
        ("~/.claude/.credentials.json", ".claude/.credentials.json"),
        ("~/.claude/.credentials.json.enc", ".claude/.credentials.json.enc"),
        ("~/.claude/.credentials.key", ".claude/.credentials.key"),
        ("~/.claude/CLAUDE.md", ".claude/CLAUDE.md"),
        ("~/.claude/settings.json", ".claude/settings.json"),
        ("~/.claude/settings.local.json", ".claude/settings.local.json"),
        ("~/.claude/developer_instructions.txt", ".claude/developer_instructions.txt"),
        ("~/.claude/system_instructions.txt", ".claude/system_instructions.txt"),
        ("~/.claude/statusline-command.sh", ".claude/statusline-command.sh"),
        ("~/.claude/mcp-needs-auth-cache.json", ".claude/mcp-needs-auth-cache.json"),
        ("~/.claude/stats-cache.json", ".claude/stats-cache.json"),
        ("~/.claude/plugins/config.json", ".claude/plugins/config.json"),
        ("~/.claude/plugins/blocklist.json", ".claude/plugins/blocklist.json"),
        ("~/.claude/plugins/known_marketplaces.json", ".claude/plugins/known_marketplaces.json"),
    ],
    "codex": [
        ("~/.codex/auth.json", ".codex/auth.json"),
        ("~/.codex/config.toml", ".codex/config.toml"),
        ("~/.codex/config.json", ".codex/config.json"),
        ("~/.codex/.codex-global-state.json", ".codex/.codex-global-state.json"),
        ("~/.codex/.personality_migration", ".codex/.personality_migration"),
        ("~/.codex/AGENTS.md", ".codex/AGENTS.md"),
        ("~/.codex/history.jsonl", ".codex/history.jsonl"),
        ("~/.codex/instructions.md", ".codex/instructions.md"),
        ("~/.codex/internal_storage.json", ".codex/internal_storage.json"),
        ("~/.codex/models_cache.json", ".codex/models_cache.json"),
        ("~/.codex/update-check.json", ".codex/update-check.json"),
        ("~/.codex/version.json", ".codex/version.json"),
        ("~/.codex/policy/default.codexpolicy", ".codex/policy/default.codexpolicy"),
        ("~/.codex/rules/default.rules", ".codex/rules/default.rules"),
    ],
    "copilot": [
        ("~/.copilot/config.json", ".copilot/config.json"),
        ("~/.copilot/command-history-state.json", ".copilot/command-history-state.json"),
        ("~/.copilot/AGENTS.md", ".copilot/AGENTS.md"),
        (
            "~/.copilot/agents/dev-instructions.agent.md",
            ".copilot/agents/dev-instructions.agent.md",
        ),
        (
            "~/.copilot/.system/.codex-system-skills.marker",
            ".copilot/.system/.codex-system-skills.marker",
        ),
        ("~/.config/github-copilot/apps.json", ".config/github-copilot/apps.json"),
        ("~/.config/github-copilot/byok.json", ".config/github-copilot/byok.json"),
        ("~/.config/github-copilot/versions.json", ".config/github-copilot/versions.json"),
        ("~/.config/github-copilot/xcode/mcp.json", ".config/github-copilot/xcode/mcp.json"),
    ],
    "gemini": [
        ("~/.gemini/GEMINI.md", ".gemini/GEMINI.md"),
        ("~/.gemini/google_accounts.json", ".gemini/google_accounts.json"),
        ("~/.gemini/installation_id", ".gemini/installation_id"),
        ("~/.gemini/oauth_creds.json", ".gemini/oauth_creds.json"),
        ("~/.gemini/projects.json", ".gemini/projects.json"),
        ("~/.gemini/settings.json", ".gemini/settings.json"),
        ("~/.gemini/state.json", ".gemini/state.json"),
        ("~/.gemini/trustedFolders.json", ".gemini/trustedFolders.json"),
        ("~/.gemini/antigravity/installation_id", ".gemini/antigravity/installation_id"),
        ("~/.gemini/antigravity/mcp_config.json", ".gemini/antigravity/mcp_config.json"),
        (
            "~/.gemini/extensions/extension-enablement.json",
            ".gemini/extensions/extension-enablement.json",
        ),
        ("~/.gemini/policies/auto-saved.toml", ".gemini/policies/auto-saved.toml"),
    ],
}


@dataclass(frozen=True)
class PackageFileEntry:
    """One file to include in the remote package."""

    remote_rel_path: str  # relative to session dir (the fake $HOME)
    local_path: Path | None = None
    content: str | None = None


@dataclass(frozen=True)
class RemotePackage:
    """Complete package definition for a remote tool session."""

    tool_name: str
    real_home: str  # absolute remote home for the actual user account
    session_dir: str  # absolute path on remote, e.g. ~/.ai-cli/remote-sessions/claude-a1b2c3d4
    tmux_socket: str  # tmux -L socket name, e.g. "ai-cli-claude"
    session_name: str  # tmux session name inside the socket
    project_prompt_rel_path: str  # relative to session_dir
    entries: list[PackageFileEntry] = field(default_factory=list)


def _normalize_target(system_name: str, machine_name: str) -> tuple[str, str]:
    system = system_name.strip().lower()
    machine = machine_name.strip().lower()
    if system == "linux":
        system = "linux"
    elif system == "darwin":
        system = "darwin"
    if machine in {"x86_64", "amd64"}:
        machine = "x86_64"
    elif machine in {"aarch64", "arm64"}:
        machine = "arm64"
    return system, machine


def local_ai_mux_asset_path(system_name: str, machine_name: str) -> Path:
    system, machine = _normalize_target(system_name, machine_name)
    return Path(__file__).resolve().parent / "bin" / f"ai-mux-{system}-{machine}"


def resolve_remote_target(remote_spec: RemoteSpec) -> tuple[str, str]:
    proc = subprocess.run(
        [*_ssh_base(remote_spec), "uname -s && uname -m"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not resolve remote target for {remote_spec.ssh_target}: "
            f"{proc.stderr.strip() or 'ssh command failed'}"
        )
    parts = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(parts) < 2:
        raise RuntimeError(
            f"Could not resolve remote target for {remote_spec.ssh_target}: "
            f"unexpected output {proc.stdout!r}"
        )
    return _normalize_target(parts[0], parts[1])


def ensure_remote_ai_mux_asset(remote_spec: RemoteSpec) -> Path:
    system, machine = resolve_remote_target(remote_spec)
    asset_path = local_ai_mux_asset_path(system, machine)

    if (system, machine) != ("linux", "x86_64"):
        raise RuntimeError(f"No ai-mux asset available for remote target {system}/{machine}")

    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        raise FileNotFoundError("rsync is required to build remote ai-mux assets.")

    repo_root = Path(__file__).resolve().parent.parent
    remote_src = "~/.ai-cli/build/ai-mux-src"
    remote_bin = f"{remote_src}/target/release/ai-mux"
    mkdir_proc = subprocess.run(
        [*_ssh_base(remote_spec), f"mkdir -p {remote_src}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if mkdir_proc.returncode != 0:
        raise RuntimeError(
            f"Could not prepare remote ai-mux build dir: {mkdir_proc.stderr.strip()}"
        )

    with tempfile.TemporaryDirectory(prefix="ai-mux-src-") as staging:
        staging_path = Path(staging)
        mux_root = staging_path / "mux"
        mux_root.mkdir(parents=True, exist_ok=True)
        source_root = repo_root / "mux"
        for rel in _MUX_SOURCE_FILES:
            src = source_root / rel
            dest = mux_root / rel
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)
        proc = subprocess.run(
            [
                rsync_bin,
                "-az",
                "-e",
                _SSH_OPTS,
                str(mux_root) + "/",
                f"{remote_spec.ssh_target}:{remote_src}/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not sync ai-mux source to {remote_spec.ssh_target}: {proc.stderr.strip()}"
        )

    build_proc = subprocess.run(
        [
            *_ssh_base(remote_spec),
            f"cd {remote_src} && cargo build --release",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if build_proc.returncode != 0:
        raise RuntimeError(
            f"Could not build remote ai-mux asset on {remote_spec.ssh_target}: "
            f"{build_proc.stderr.strip() or build_proc.stdout.strip()}"
        )

    asset_path.parent.mkdir(parents=True, exist_ok=True)
    fetch_proc = subprocess.run(
        [
            rsync_bin,
            "-az",
            "-e",
            _SSH_OPTS,
            f"{remote_spec.ssh_target}:{remote_bin}",
            str(asset_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch_proc.returncode != 0:
        raise RuntimeError(
            f"Could not fetch remote ai-mux asset from {remote_spec.ssh_target}: "
            f"{fetch_proc.stderr.strip()}"
        )
    asset_path.chmod(0o755)
    return asset_path


# ---------------------------------------------------------------------------
# Deterministic naming
# ---------------------------------------------------------------------------


def compute_session_dir(tool_name: str, remote_spec: RemoteSpec) -> str:
    """Return the remote session directory path.

    Deterministic: same tool + remote spec always gives the same path,
    allowing session reuse across invocations.
    """
    digest = hashlib.sha256(f"{tool_name}:{remote_spec.display}".encode()).hexdigest()[:12]
    return f"~/.ai-cli/remote-sessions/{tool_name}-{digest}"


def compute_tmux_socket(tool_name: str) -> str:
    """Return the tmux socket name for a tool (``tmux -L <name>``)."""
    return f"ai-cli-{tool_name}"


def compute_session_name(tool_name: str, remote_spec: RemoteSpec) -> str:
    """Return the tmux session name (matches the directory basename)."""
    digest = hashlib.sha256(f"{tool_name}:{remote_spec.display}".encode()).hexdigest()[:12]
    return f"{tool_name}-{digest}"


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------


def _add_if_exists(entries: list[PackageFileEntry], local: Path, rel: str) -> None:
    if (
        local.is_file()
        and local.name not in _SKIP_FILE_NAMES
        and not local.name.startswith("._")
        and rel not in {entry.remote_rel_path for entry in entries}
    ):
        entries.append(PackageFileEntry(local_path=local, remote_rel_path=rel))


def _add_generated(entries: list[PackageFileEntry], rel: str, content: str) -> None:
    if rel in {entry.remote_rel_path for entry in entries}:
        return
    entries.append(PackageFileEntry(content=content, remote_rel_path=rel))


def _add_pairs(entries: list[PackageFileEntry], pairs: list[tuple[str, str]]) -> None:
    for local_raw, remote_rel in pairs:
        _add_if_exists(entries, Path(local_raw).expanduser(), remote_rel)


def _render_shell_common(
    tool_name: str,
    remote_spec: RemoteSpec,
    session_name: str,
    project_prompt_remote_path: str,
) -> str:
    tool_q = shlex.quote(tool_name)
    root_q = shlex.quote(remote_spec.path)
    session_q = shlex.quote(session_name)
    return textwrap.dedent(
        f"""\
        # ai-cli remote shell helpers
        [ "${{AI_CLI_REMOTE_SHELL_COMMON_LOADED:-0}}" = 1 ] && return 0 2>/dev/null
        export AI_CLI_REMOTE_SHELL_COMMON_LOADED=1

        # Ensure real-home tool paths are available (nvm, .local/bin, cargo, etc.)
        _rh="${{REAL_HOME:-$HOME}}"
        [ -s "${{NVM_DIR:-$_rh/.nvm}}/nvm.sh" ] && . "${{NVM_DIR:-$_rh/.nvm}}/nvm.sh" 2>/dev/null
        [ -s "$_rh/.config/fnm/fnm_multishells" ] && eval "$(fnm env 2>/dev/null)" 2>/dev/null
        [ -d "$_rh/.volta" ] && export VOLTA_HOME="$_rh/.volta" && export PATH="$VOLTA_HOME/bin:$PATH"
        case ":$PATH:" in *":$_rh/.local/bin:"*) ;; *) export PATH="$_rh/.local/bin:$PATH" ;; esac
        case ":$PATH:" in *":$_rh/.cargo/bin:"*) ;; *) export PATH="$_rh/.cargo/bin:$PATH" ;; esac
        unset _rh

        export AI_CLI_REMOTE_TOOL={tool_q}
        export AI_CLI_REMOTE_PROJECT_ROOT={root_q}
        export AI_CLI_REMOTE_SESSION_NAME={session_q}
        export AI_CLI_WORKDIR={root_q}
        export AI_CLI_BASE_PROMPT_FILE="$HOME/.ai-cli/base_instructions.txt"
        export AI_CLI_GLOBAL_PROMPT_FILE="$HOME/.ai-cli/system_instructions.txt"
        export AI_CLI_TOOL_PROMPT_FILE="$HOME/.ai-cli/instructions/{tool_name}.txt"
        export AI_CLI_PROJECT_PROMPT_FILE="{project_prompt_remote_path}"
        export AI_CLI_CODEX_PERSONALITY_PROMPT_FILE="$HOME/.ai-cli/instructions/codex-personality.txt"
        export AI_CLI_PYTHON="${{AI_CLI_PYTHON:-python3}}"
        export AI_CLI_PROMPT_EDITOR_LAUNCHER="$HOME/.ai-cli/bin/ai-prompt-editor"
        export AI_CLI_REMOTE_COMMAND_LOG="$HOME/.ai-cli/logs/shell-commands.log"
        export AI_CLI_REMOTE_SESSION_LOG="$HOME/.ai-cli/logs/shell-session.log"
        export AI_CLI_REMOTE_TMUX_CONF="$HOME/.config/ai-cli/tmux.conf"

        _ai_cli_log_dir() {{
          printf '%s' "$HOME/.ai-cli/logs"
        }}

        _ai_cli_ensure_logs() {{
          mkdir -p "$(_ai_cli_log_dir)" 2>/dev/null || true
        }}

        _ai_cli_ts() {{
          date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || date
        }}

        _ai_cli_scope_state() {{
          case "$PWD" in
            "$AI_CLI_REMOTE_PROJECT_ROOT"|"$AI_CLI_REMOTE_PROJECT_ROOT"/*) printf '%s' "in-scope" ;;
            *) printf '%s' "OUT-OF-SCOPE" ;;
          esac
        }}

        _ai_cli_log_command() {{
          [ "${{AI_CLI_REMOTE_LOG_GUARD:-0}}" = 1 ] && return 0
          AI_CLI_REMOTE_LOG_GUARD=1
          _ai_cli_ensure_logs
          _ai_cli_shell_name="${{2:-shell}}"
          _ai_cli_cmd="${{1:-}}"
          [ -n "$_ai_cli_cmd" ] || {{
            AI_CLI_REMOTE_LOG_GUARD=0
            unset _ai_cli_shell_name _ai_cli_cmd
            return 0
          }}
          printf '%s shell=%s scope=%s cwd=%s cmd=%s\\n' \\
            "$(_ai_cli_ts)" "$_ai_cli_shell_name" "$(_ai_cli_scope_state)" "$PWD" "$_ai_cli_cmd" >> "$AI_CLI_REMOTE_COMMAND_LOG"
          AI_CLI_REMOTE_LOG_GUARD=0
          unset _ai_cli_shell_name _ai_cli_cmd
        }}

        ai_rules() {{
          printf '%s\\n' \\
            "[ai-cli remote rules]" \\
            "1. Default scope root: $AI_CLI_REMOTE_PROJECT_ROOT" \\
            "2. Commands are logged to: $AI_CLI_REMOTE_COMMAND_LOG" \\
            "3. Use ai_scope, ai_root, ai_scope_check, and ai_log_tail to manage session scope." \\
            "4. Treat leaving the scope root as explicit and temporary."
        }}

        ai_scope() {{
          printf '%s\\n' \\
            "tool=$AI_CLI_REMOTE_TOOL" \\
            "session=$AI_CLI_REMOTE_SESSION_NAME" \\
            "home=$HOME" \\
            "root=$AI_CLI_REMOTE_PROJECT_ROOT" \\
            "cwd=$PWD" \\
            "scope=$(_ai_cli_scope_state)" \\
            "command_log=$AI_CLI_REMOTE_COMMAND_LOG" \\
            "tmux_conf=$AI_CLI_REMOTE_TMUX_CONF"
        }}

        ai_root() {{
          cd "$AI_CLI_REMOTE_PROJECT_ROOT" || return 1
        }}

        ai_scope_check() {{
          printf '%s\\n' "scope=$(_ai_cli_scope_state) cwd=$PWD root=$AI_CLI_REMOTE_PROJECT_ROOT"
        }}

        ai_log_tail() {{
          _ai_cli_ensure_logs
          tail -n "${{1:-40}}" "$AI_CLI_REMOTE_COMMAND_LOG"
        }}

        _ai_cli_banner() {{
          [ -t 1 ] || return 0
          [ "${{AI_CLI_REMOTE_BANNER_SHOWN:-0}}" = 1 ] && return 0
          export AI_CLI_REMOTE_BANNER_SHOWN=1
          _ai_cli_ensure_logs
          printf '[ai-cli remote] tool=%s root=%s log=%s\\n' \\
            "$AI_CLI_REMOTE_TOOL" "$AI_CLI_REMOTE_PROJECT_ROOT" "$AI_CLI_REMOTE_COMMAND_LOG"
          ai_rules
          _ai_cli_log_command "__shell_start__" "shell"
        }}

        _ai_cli_scope_notice() {{
          case "$PWD" in
            "$AI_CLI_REMOTE_PROJECT_ROOT"|"$AI_CLI_REMOTE_PROJECT_ROOT"/*) ;;
            *)
              printf '[ai-cli remote] warning: outside scope root %s\\n' "$AI_CLI_REMOTE_PROJECT_ROOT" >&2
              ;;
          esac
        }}
        """
    )


def _shell_escape(value: str) -> str:
    if not value:
        return "''"
    safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/=+,@")
    if all(ch in safe for ch in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def _render_editor_shell_cmd(
    script_expr: str,
    tmux_socket: str,
    window_name: str,
    target: str,
) -> str:
    return (
        f"{script_expr} open --tmux-socket {shlex.quote(tmux_socket)} "
        f"--window-name {shlex.quote(window_name)} --target {shlex.quote(target)}"
    )


def _render_tmux_editor_binding(key: str, window_name: str, editor_cmd: str) -> str:
    guard = "#{m:^edit-(global|base|tool|project)$,#{window_name}}"
    return (
        f"bind -n {key} if-shell -F {_shell_escape(guard)} "
        f"{{ run-shell true }} "
        f"{{ run-shell -b {_shell_escape(editor_cmd)} }}"
    )


def _render_tmux_conf(tool_name: str, remote_spec: RemoteSpec) -> str:
    script_expr = "$HOME/.ai-cli/bin/ai-prompt-editor"
    tmux_socket = compute_tmux_socket(tool_name)
    edit_global_cmd = _render_editor_shell_cmd(script_expr, tmux_socket, "edit-global", "global")
    edit_base_cmd = _render_editor_shell_cmd(script_expr, tmux_socket, "edit-base", "base")
    edit_tool_cmd = _render_editor_shell_cmd(
        script_expr,
        tmux_socket,
        "edit-tool",
        "tool",
    )
    edit_project_cmd = _render_editor_shell_cmd(
        script_expr,
        tmux_socket,
        "edit-project",
        "project",
    )
    lines = [
        "# ai-mux tmux configuration (auto-generated, do not edit)",
        "# User overrides: ~/.config/ai-cli/tmux.conf",
        "",
        "set -g mouse on",
        "set -g status-position top",
        "set -sg escape-time 0",
        "set -g xterm-keys on",
        "set -g default-terminal 'screen-256color'",
        "set -g focus-events on",
        "set -g history-limit 50000",
        "set -g remain-on-exit off",
        "set -g renumber-windows off",
        "set -g base-index 0",
        "set -g allow-rename off",
        "set -g automatic-rename off",
        "",
        "# Status bar",
        "set -g status-style 'bg=#333333,fg=white,dim'",
        "set -g status-left ''",
        "set -g status-right '#[fg=#ffffff,bold] F5:global F6:base F7:tool F8:project │ C-] prefix '",
        "set -g status-left-length 0",
        "set -g status-right-length 60",
        "set -g window-status-format ' #W '",
        "set -g window-status-current-format '#[bold,noreverse] #W '",
        "set -g window-status-current-style 'bg=default,fg=white,bold'",
        "set -g window-status-style 'bg=#333333,fg=#999999'",
        "set -g window-status-separator ''",
        "",
        "# Use C-] as prefix (avoids conflicts with tools)",
        "unbind C-b",
        "set -g prefix C-]",
        "bind C-] send-prefix",
        "",
        "# Key bindings (no prefix)",
        "bind -n F2 select-window -t :0",
        "bind -n F3 select-window -t :1",
        "bind -n F4 select-window -t :2",
        "bind -n F10 select-window -t :5",
        "bind -n F11 select-window -t :6",
        _render_tmux_editor_binding("F5", "edit-global", edit_global_cmd),
        _render_tmux_editor_binding("F6", "edit-base", edit_base_cmd),
        _render_tmux_editor_binding("F7", "edit-tool", edit_tool_cmd),
        _render_tmux_editor_binding("F8", "edit-project", edit_project_cmd),
        "bind -n M-2 select-window -t :0",
        "bind -n M-3 select-window -t :1",
        "bind -n M-4 select-window -t :2",
        "bind -n M-5 select-window -t :3",
        "bind -n M-6 select-window -t :4",
        "bind -n M-7 select-window -t :5",
        "bind -n M-8 select-window -t :6",
        "bind -n M-9 select-window -t :7",
        "bind -n M-1 choose-tree -s",
        "bind -n F1 choose-tree -s",
        "bind -n C-n next-window",
        "bind -n C-p previous-window",
        "bind -n M-Left previous-window",
        "bind -n M-Right next-window",
        "bind q detach-client",
        "",
        "# User overrides",
        "if-shell 'test -f ~/.config/ai-cli/tmux.conf' 'source-file ~/.config/ai-cli/tmux.conf'",
    ]
    return "\n".join(lines) + "\n"


def _generated_shell_files(
    tool_name: str,
    remote_spec: RemoteSpec,
    session_name: str,
    project_prompt_remote_path: str,
) -> list[PackageFileEntry]:
    shell_common = _render_shell_common(
        tool_name,
        remote_spec,
        session_name,
        project_prompt_remote_path,
    )
    tmux_conf = _render_tmux_conf(tool_name, remote_spec)
    profile = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export BASH_ENV="$HOME/.bash_env"
        export ENV="$HOME/.shrc"
        export KSHRC="$HOME/.kshrc"
        export ZDOTDIR="$HOME"
        _ai_cli_banner
        """
    )
    bash_rc = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export BASH_ENV="$HOME/.bash_env"
        export ENV="$HOME/.shrc"
        export KSHRC="$HOME/.kshrc"
        _ai_cli_bash_prompt_hook() {
          _ai_cli_banner
          _ai_cli_scope_notice
          _ai_cli_bash_last="$(history 1 2>/dev/null | sed 's/^ *[0-9][0-9]* *//')"
          if [ -n "$_ai_cli_bash_last" ] && [ "$_ai_cli_bash_last" != "${AI_CLI_REMOTE_LAST_BASH_COMMAND:-}" ]; then
            AI_CLI_REMOTE_LAST_BASH_COMMAND="$_ai_cli_bash_last"
            _ai_cli_log_command "$_ai_cli_bash_last" "bash"
          fi
          unset _ai_cli_bash_last
        }
        case ";${PROMPT_COMMAND:-};" in
          *";_ai_cli_bash_prompt_hook;"*) ;;
          *) PROMPT_COMMAND="_ai_cli_bash_prompt_hook${PROMPT_COMMAND:+;$PROMPT_COMMAND}" ;;
        esac
        """
    )
    bash_env = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export BASH_ENV="$HOME/.bash_env"
        export ENV="$HOME/.shrc"
        export KSHRC="$HOME/.kshrc"
        _ai_cli_bash_debug_hook() {
          [ "${AI_CLI_REMOTE_LOG_GUARD:-0}" = 1 ] && return 0
          _ai_cli_bash_debug_cmd="${BASH_COMMAND:-}"
          case "$_ai_cli_bash_debug_cmd" in
            ""|_ai_cli_*|history\\ *|trap\\ *|hash\\ -r*|unalias\\ *) return 0 ;;
          esac
          _ai_cli_log_command "$_ai_cli_bash_debug_cmd" "bash"
        }
        trap '_ai_cli_bash_debug_hook' DEBUG
        """
    )
    bash_profile = textwrap.dedent(
        """\
        [ -f "$HOME/.profile" ] && . "$HOME/.profile"
        [ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"
        """
    )
    zshenv = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export ENV="$HOME/.shrc"
        export BASH_ENV="$HOME/.bash_env"
        export KSHRC="$HOME/.kshrc"
        export ZDOTDIR="$HOME"
        """
    )
    zshrc = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export ENV="$HOME/.shrc"
        export BASH_ENV="$HOME/.bash_env"
        export KSHRC="$HOME/.kshrc"
        autoload -Uz add-zsh-hook 2>/dev/null || true
        if [ "${AI_CLI_REMOTE_ZSH_HOOKS:-0}" != 1 ]; then
          _ai_cli_zsh_preexec() {
            _ai_cli_log_command "$1" "zsh"
          }
          _ai_cli_zsh_precmd() {
            _ai_cli_banner
            _ai_cli_scope_notice
          }
          add-zsh-hook preexec _ai_cli_zsh_preexec 2>/dev/null || true
          add-zsh-hook precmd _ai_cli_zsh_precmd 2>/dev/null || true
          export AI_CLI_REMOTE_ZSH_HOOKS=1
        fi
        """
    )
    zprofile = textwrap.dedent(
        """\
        [ -f "$HOME/.profile" ] && . "$HOME/.profile"
        [ -f "$HOME/.zshrc" ] && . "$HOME/.zshrc"
        """
    )
    shrc = textwrap.dedent(
        """\
        [ -f "$HOME/.ai-cli/shell-common.sh" ] && . "$HOME/.ai-cli/shell-common.sh"
        export ENV="$HOME/.shrc"
        export KSHRC="$HOME/.kshrc"
        _ai_cli_banner
        _ai_cli_scope_notice
        """
    )
    return [
        PackageFileEntry(remote_rel_path=".ai-cli/shell-common.sh", content=shell_common),
        PackageFileEntry(remote_rel_path=".profile", content=profile),
        PackageFileEntry(remote_rel_path=".bashrc", content=bash_rc),
        PackageFileEntry(remote_rel_path=".bash_env", content=bash_env),
        PackageFileEntry(remote_rel_path=".bash_profile", content=bash_profile),
        PackageFileEntry(remote_rel_path=".bash_login", content=bash_profile),
        PackageFileEntry(remote_rel_path=".zshenv", content=zshenv),
        PackageFileEntry(remote_rel_path=".zshrc", content=zshrc),
        PackageFileEntry(remote_rel_path=".zprofile", content=zprofile),
        PackageFileEntry(remote_rel_path=".shrc", content=shrc),
        PackageFileEntry(remote_rel_path=".kshrc", content=shrc),
        PackageFileEntry(remote_rel_path=".tmux.conf", content=tmux_conf),
    ]


def _ssh_base(remote_spec: RemoteSpec) -> list[str]:
    return ["ssh"] + _SSH_OPTS.split()[1:] + [remote_spec.ssh_target]


def resolve_remote_home(remote_spec: RemoteSpec) -> str:
    """Resolve the remote user's absolute home directory via SSH."""
    proc = subprocess.run(
        [*_ssh_base(remote_spec), "printf '%s\\n' \"$HOME\""],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not resolve remote home for {remote_spec.ssh_target}: "
            f"{proc.stderr.strip() or 'ssh command failed'}"
        )
    remote_home = proc.stdout.strip()
    if not remote_home.startswith("/"):
        raise RuntimeError(
            f"Could not resolve remote home for {remote_spec.ssh_target}: "
            f"unexpected value {remote_home!r}"
        )
    return remote_home


def build_package_manifest(
    tool_name: str,
    remote_spec: RemoteSpec,
    *,
    ca_path: Path | None = None,
    ai_mux_binary: Path | None = None,
) -> RemotePackage:
    """Build the file manifest for a remote tool session.

    Only includes entries whose local source actually exists.
    """
    session_rel_dir = PurePosixPath(compute_session_dir(tool_name, remote_spec).removeprefix("~/"))
    real_home = resolve_remote_home(remote_spec)
    session_dir = str(PurePosixPath(real_home) / session_rel_dir)
    tmux_socket = compute_tmux_socket(tool_name)
    session_name = compute_session_name(tool_name, remote_spec)

    entries: list[PackageFileEntry] = []

    # ── Common (all tools) ────────────────────────────────────────────────
    ai_cli_dir = Path("~/.ai-cli").expanduser()
    project_prompt_file = Path(
        ensure_project_instructions_file(
            project_cwd=remote_spec.path,
            remote_spec=remote_spec.display,
        )
    ).expanduser()
    project_prompt_meta = project_prompt_file.parent / "meta.json"
    project_prompt_rel = project_prompt_file.relative_to(ai_cli_dir).as_posix()
    project_prompt_rel_path = f".ai-cli/{project_prompt_rel}"

    _add_if_exists(entries, ai_cli_dir / "config.json", ".ai-cli/config.json")
    _add_if_exists(
        entries, ai_cli_dir / "system_instructions.txt", ".ai-cli/system_instructions.txt"
    )
    _add_if_exists(
        entries,
        Path("~/.config/ai-cli/tmux.conf").expanduser(),
        ".config/ai-cli/tmux.conf",
    )

    # User's base instructions (prefer user copy; fall back to shipped template)
    user_base = ai_cli_dir / "base_instructions.txt"
    shipped_base = Path(__file__).resolve().parent.parent / "templates" / "base_instructions.txt"
    if user_base.is_file():
        entries.append(
            PackageFileEntry(
                remote_rel_path=".ai-cli/base_instructions.txt",
                local_path=user_base,
            )
        )
    elif shipped_base.is_file():
        entries.append(
            PackageFileEntry(
                remote_rel_path=".ai-cli/base_instructions.txt",
                local_path=shipped_base,
            )
        )

    # Per-tool instructions — push ALL tool files so F7 edits see them all
    instructions_dir = ai_cli_dir / "instructions"
    if instructions_dir.is_dir():
        for txt in sorted(instructions_dir.glob("*.txt")):
            _add_if_exists(
                entries,
                txt,
                f".ai-cli/instructions/{txt.name}",
            )
    _add_if_exists(entries, project_prompt_file, project_prompt_rel_path)
    _add_if_exists(
        entries,
        project_prompt_meta,
        f".ai-cli/{project_prompt_meta.relative_to(ai_cli_dir).as_posix()}",
    )
    _add_if_exists(
        entries,
        Path(__file__).resolve().parent / "prompt_editor_launcher.py",
        ".ai-cli/bin/ai-prompt-editor",
    )
    _add_if_exists(
        entries,
        Path(__file__).resolve().parent / "codex_personality_menu.py",
        ".ai-cli/bin/ai-codex-personality-menu",
    )
    if ai_mux_binary is not None:
        _add_if_exists(
            entries,
            ai_mux_binary.expanduser(),
            ".ai-cli/bin/ai-mux",
        )

    # CA certificate (proxy trust)
    if ca_path is not None:
        ca_resolved = Path(ca_path).expanduser()
        if ca_resolved.is_file():
            entries.append(
                PackageFileEntry(
                    remote_rel_path=".ai-cli/remote-ca.pem",
                    local_path=ca_resolved,
                )
            )
            entries.append(
                PackageFileEntry(
                    remote_rel_path=".mitmproxy/mitmproxy-ca-cert.pem",
                    local_path=ca_resolved,
                )
            )

    # ── Tool-specific startup state ───────────────────────────────────────
    _add_pairs(entries, _TOOL_PACKAGE_FILES.get(tool_name, []))
    for entry in _generated_shell_files(
        tool_name,
        remote_spec,
        session_name,
        f"$HOME/{project_prompt_rel_path}",
    ):
        _add_generated(entries, entry.remote_rel_path, entry.content or "")

    return RemotePackage(
        tool_name=tool_name,
        real_home=real_home,
        session_dir=session_dir,
        tmux_socket=tmux_socket,
        session_name=session_name,
        project_prompt_rel_path=project_prompt_rel_path,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


def push_package(
    package: RemotePackage,
    remote_spec: RemoteSpec,
) -> None:
    """Push the package to the remote host via rsync from a staging tmpdir."""
    if not package.entries:
        return

    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        raise FileNotFoundError(
            "rsync is required for remote package push but was not found on PATH."
        )

    with tempfile.TemporaryDirectory(prefix="ai-cli-pkg-") as staging:
        staging_path = Path(staging)
        for entry in package.entries:
            dest = staging_path / entry.remote_rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if entry.local_path is not None:
                shutil.copy2(str(entry.local_path), str(dest))
            elif entry.content is not None:
                dest.write_text(entry.content, encoding="utf-8")
            else:
                raise RuntimeError(
                    f"Package entry {entry.remote_rel_path} is missing both local_path and content."
                )

        # Ensure the remote session dir exists
        mkdir_proc = subprocess.run(
            [*_ssh_base(remote_spec), f"mkdir -p {shlex.quote(package.session_dir)}"],
            check=False,
            capture_output=True,
            text=True,
        )
        if mkdir_proc.returncode != 0:
            raise RuntimeError(
                f"Could not create remote session dir {package.session_dir} "
                f"(rc={mkdir_proc.returncode}): {mkdir_proc.stderr.strip()}"
            )

        # Single rsync push — chmod ensures credentials aren't world-readable
        proc = subprocess.run(
            [
                rsync_bin,
                "-az",
                f"--chmod={_RSYNC_CHMOD}",
                "-e",
                _SSH_OPTS,
                str(staging_path) + "/",
                f"{remote_spec.ssh_target}:{package.session_dir}/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"rsync package push failed (rc={proc.returncode}): {proc.stderr.strip()}"
            )
        subprocess.run(
            [
                *_ssh_base(remote_spec),
                f"chmod 700 {shlex.quote(package.session_dir + '/.ai-cli/bin/ai-prompt-editor')} 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            [
                *_ssh_base(remote_spec),
                f"chmod 700 {shlex.quote(package.session_dir + '/.ai-cli/bin/ai-codex-personality-menu')} 2>/dev/null || true",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        # Also push tool config/auth files to the REAL home on the remote.
        # Some tools (e.g. claude) resolve $HOME via getpwuid/passwd and
        # ignore our HOME override, so credentials must also live there.
        tool_pairs = _TOOL_PACKAGE_FILES.get(package.tool_name, [])
        if tool_pairs and package.real_home:
            real_staging = Path(staging) / "__real_home__"
            real_staging.mkdir(exist_ok=True)
            has_files = False
            for local_raw, remote_rel in tool_pairs:
                src = Path(local_raw).expanduser()
                if src.is_file():
                    dest = real_staging / remote_rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(src), str(dest))
                    has_files = True
            if has_files:
                subprocess.run(
                    [
                        rsync_bin,
                        "-az",
                        f"--chmod={_RSYNC_CHMOD}",
                        "-e",
                        _SSH_OPTS,
                        str(real_staging) + "/",
                        f"{remote_spec.ssh_target}:{package.real_home}/",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def probe_remote_session(
    package: RemotePackage,
    remote_spec: RemoteSpec,
) -> bool:
    """Check if a tmux session for this package already exists on the remote."""
    proc = subprocess.run(
        [
            *_ssh_base(remote_spec),
            f"tmux -L {shlex.quote(package.tmux_socket)} "
            f"has-session -t {shlex.quote(package.session_name)} 2>/dev/null",
        ],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def reattach_remote_session(
    package: RemotePackage,
    remote_spec: RemoteSpec,
    *,
    proxy_port: int = 0,
    ssh_opts: list[str] | None = None,
) -> int:
    """Reattach to an existing remote tmux session.

    Sets up the SSH reverse tunnel if *proxy_port* is non-zero.
    """
    sock_q = shlex.quote(package.tmux_socket)
    sess_q = shlex.quote(package.session_name)
    conf_q = shlex.quote(f"{package.session_dir}/.tmux.conf")
    remote_cmd = (
        f"for c in $(tmux -L {sock_q} list-clients -t {sess_q} -F '#{{client_tty}}' 2>/dev/null); do "
        f'tmux -L {sock_q} detach-client -t "$c" 2>/dev/null; done; '
        f"tmux -L {sock_q} source-file {conf_q} >/dev/null 2>&1; "
        f"tmux -L {sock_q} attach -t {sess_q}"
    )

    ssh_cmd = [
        "ssh",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "RequestTTY=force",
    ]
    if proxy_port:
        ssh_cmd += ["-R", f"127.0.0.1:{proxy_port}:127.0.0.1:{proxy_port}"]
    for opt in ssh_opts or []:
        ssh_cmd.append(opt)
    ssh_cmd += [remote_spec.ssh_target, remote_cmd]

    print_sync_status(f"Reattaching to existing session '{package.session_name}'")
    proc = subprocess.run(ssh_cmd, check=False)
    return proc.returncode


def render_remote_ai_mux_config(
    *,
    tool_name: str,
    session_name: str,
    command: list[str],
    cwd: str,
    env: dict[str, str],
) -> str:
    payload = {
        "session_name": session_name,
        "tabs": [
            {
                "label": tool_name,
                "cmd": command,
                "env": env,
                "cwd": cwd,
                "primary": True,
            }
        ],
    }
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Pull artifacts
# ---------------------------------------------------------------------------

_ARTIFACT_GLOBS = [
    ".ai-cli/logs/",
    ".ai-cli/system_instructions.txt",
    ".ai-cli/base_instructions.txt",
    ".ai-cli/instructions/",
    ".ai-cli/project-prompts/",
    ".claude/projects/",
    ".codex/sessions/",
    ".codex/projects/",
    ".copilot/sessions/",
    ".gemini/sessions/",
]


def pull_session_artifacts(
    package: RemotePackage,
    remote_spec: RemoteSpec,
    local_dest: Path,
) -> None:
    """Pull logs and session data from the remote session dir back to local."""
    local_dest.mkdir(parents=True, exist_ok=True)
    rsync_bin = shutil.which("rsync")
    if not rsync_bin:
        print_sync_status("Warning: rsync not found; skipping artifact pull")
        return

    safe_host = re.sub(r"[^A-Za-z0-9_-]", "-", remote_spec.host)
    for rel_glob in _ARTIFACT_GLOBS:
        src = f"{remote_spec.ssh_target}:{package.session_dir}/{rel_glob}"
        dest = local_dest / f"remote-{safe_host}" / rel_glob.rstrip("/").replace("/", "-")
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [rsync_bin, "-az", "--ignore-errors", "-e", _SSH_OPTS, src, str(dest) + "/"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            print_sync_status(f"Pulled {rel_glob} → {dest}")
        # Non-fatal: remote dir may not exist

    # Sync edited prompt layers back to local ~/.ai-cli/
    _pull_prompt_layers_back(package, remote_spec, rsync_bin)


# Prompt layer files to sync back (relative to session dir → local ~/.ai-cli/)
_PROMPT_PULL_BACK = [
    ".ai-cli/system_instructions.txt",
    ".ai-cli/base_instructions.txt",
    ".ai-cli/instructions/",
    ".ai-cli/project-prompts/",
]


def _pull_prompt_layers_back(
    package: RemotePackage,
    remote_spec: RemoteSpec,
    rsync_bin: str,
) -> None:
    """Pull edited prompt files from the remote session dir back to local ~/.ai-cli/."""
    ai_cli_dir = Path("~/.ai-cli").expanduser()
    for rel in _PROMPT_PULL_BACK:
        src = f"{remote_spec.ssh_target}:{package.session_dir}/{rel}"
        local_target = ai_cli_dir / rel.removeprefix(".ai-cli/")
        if rel.endswith("/"):
            local_target.mkdir(parents=True, exist_ok=True)
            dest_str = str(local_target) + "/"
        else:
            local_target.parent.mkdir(parents=True, exist_ok=True)
            dest_str = str(local_target)
        proc = subprocess.run(
            [rsync_bin, "-az", "--update", "--ignore-errors", "-e", _SSH_OPTS, src, dest_str],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            print_sync_status(f"Synced back {rel} → {local_target}")

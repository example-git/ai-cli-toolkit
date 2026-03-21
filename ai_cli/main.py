"""CLI dispatch — routes by argv[0] name or subcommand.

Entry point for the ai-cli unified wrapper. Supports:
- Binary-name routing: symlinks named 'claude', 'codex', etc. auto-dispatch
- Subcommand routing: 'ai-cli claude [args]', 'ai-cli menu', 'ai-cli system', etc.
- Default: opens TUI menu when invoked with no args
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ai_cli import __version__
from ai_cli.ca import bootstrap_ca_cert
from ai_cli.config import (
    ensure_config,
    get_privacy_config,
    get_proxy_config,
    get_retention_config,
    get_tool_config,
)
from ai_cli.housekeeping import prune_old_logs, prune_old_traffic_rows
from ai_cli.instructions import (
    edit_instructions,
    ensure_project_instructions_file,
    resolve_base_instructions_path,
    resolve_instructions_file,
)
from ai_cli.log import append_log, fmt_cmd
from ai_cli.main_helpers import (
    ai_mux_status as _mh_ai_mux_status,
)
from ai_cli.main_helpers import (
    check_codex_proxy_compat as _mh_check_codex_proxy_compat,
)
from ai_cli.main_helpers import (
    cleanup_session_files as _mh_cleanup_session_files,
)
from ai_cli.main_helpers import (
    extract_launch_cwd as _mh_extract_launch_cwd,
)
from ai_cli.main_helpers import (
    find_ai_mux as _mh_find_ai_mux,
)
from ai_cli.main_helpers import (
    find_reusable_tmux_session as _mh_find_reusable_tmux_session,
)
from ai_cli.main_helpers import (
    kill_proxy_from_env as _mh_kill_proxy_from_env,
)
from ai_cli.main_helpers import (
    parse_wrapper_overrides as _mh_parse_wrapper_overrides,
)
from ai_cli.main_helpers import (
    replace_existing_tmux_session as _mh_replace_existing_tmux_session,
)
from ai_cli.main_helpers import (
    resolve_recv_context_file as _mh_resolve_recv_context_file,
)
from ai_cli.main_helpers import (
    session_id as _mh_session_id,
)
from ai_cli.main_helpers import (
    spawn_detached_proxy_watcher as _mh_spawn_detached_proxy_watcher,
)
from ai_cli.main_helpers import (
    terminate_pid as _mh_terminate_pid,
)
from ai_cli.main_helpers import (
    tmux_list_sessions as _mh_tmux_list_sessions,
)
from ai_cli.main_helpers import (
    tmux_session_env as _mh_tmux_session_env,
)
from ai_cli.main_helpers import (
    write_session_files as _mh_write_session_files,
)
from ai_cli.proxy import (
    allocate_port,
    apply_pinned_mitmdump_path,
    build_mitmdump_cmd,
    build_proxy_env,
    ensure_mitmdump,
    resolve_proxy_host,
    start_proxy,
    stop_process,
    verify_proxy_flow,
)
from ai_cli.remote import RemoteSpec
from ai_cli.session import build_recent_context_for_cwd
from ai_cli.tools import TOOL_ALIASES, load_registry


def _check_codex_proxy_compat(log_path: Path | None = None) -> None:
    _mh_check_codex_proxy_compat(log_path=log_path)


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def _session_id(tool_name: str) -> str:
    return _mh_session_id(tool_name)


def _write_session_files(session_id: str, port: int) -> None:
    _mh_write_session_files(session_id, port)


def _cleanup_session_files(session_id: str) -> None:
    _mh_cleanup_session_files(session_id)


def _tracked_session_dir() -> Path:
    return Path("~/.ai-cli/.sessions").expanduser()


def _tracked_session_path(session_id: str) -> Path:
    return _tracked_session_dir() / f"{session_id}.json"


_TRACKED_PID_FIELDS: dict[str, str] = {
    "wrapper_pid": "wrapper",
    "proxy_pid": "proxy",
    "mux_pid": "mux",
}


def _tracked_session_pid_path(session_id: str, pid_kind: str) -> Path:
    return _tracked_session_dir() / f"{session_id}.{pid_kind}.pid"


def _read_tracked_pid_file(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(raw) if raw.isdigit() else 0


def _sync_tracked_pid_files(session_id: str, payload: dict[str, Any]) -> None:
    for field_name, pid_kind in _TRACKED_PID_FIELDS.items():
        path = _tracked_session_pid_path(session_id, pid_kind)
        pid = int(payload.get(field_name, 0) or 0)
        try:
            if pid > 0:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{pid}\n", encoding="utf-8")
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _write_tracked_session(session_id: str, payload: dict[str, Any]) -> None:
    path = _tracked_session_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        existing.update(payload)
        existing["session_id"] = session_id
        existing.setdefault("wrapper_pid", os.getpid())
        path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        _sync_tracked_pid_files(session_id, existing)
    except (OSError, json.JSONDecodeError):
        pass


def _remove_tracked_session(session_id: str) -> None:
    try:
        _tracked_session_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass
    for pid_kind in _TRACKED_PID_FIELDS.values():
        try:
            _tracked_session_pid_path(session_id, pid_kind).unlink(missing_ok=True)
        except OSError:
            pass


def _list_tracked_sessions() -> list[dict[str, Any]]:
    root = _tracked_session_dir()
    if not root.is_dir():
        return []
    payloads: dict[str, dict[str, Any]] = {}
    for entry in sorted(root.glob("*.json")):
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            session_id = str(payload.get("session_id", "")).strip() or entry.stem
            payload["session_id"] = session_id
            payloads[session_id] = payload
    for entry in sorted(root.glob("*.pid")):
        name = entry.name
        for field_name, pid_kind in _TRACKED_PID_FIELDS.items():
            suffix = f".{pid_kind}.pid"
            if not name.endswith(suffix):
                continue
            session_id = name[: -len(suffix)].strip()
            if not session_id:
                break
            payload = payloads.setdefault(session_id, {"session_id": session_id})
            pid = _read_tracked_pid_file(entry)
            if pid > 0:
                payload[field_name] = pid
            break
    return [payloads[key] for key in sorted(payloads)]


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tmux_has_session(session_name: str, socket_name: str = "ai-mux") -> bool:
    if not session_name:
        return False
    try:
        code = subprocess.call(
            ["tmux", "-L", socket_name, "has-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return code == 0


def _is_ai_cli_proxy_command(cmd: str) -> bool:
    lowered = (cmd or "").lower()
    if "mitmdump" not in lowered and "mitmproxy" not in lowered:
        return False
    return (
        "traffic_log_addon.py" in lowered
        or "system_prompt_addon.py" in lowered
        or "wrapper_log_file=" in lowered
    )


def _is_ai_mux_command(cmd: str) -> bool:
    lowered = (cmd or "").lower()
    if "/.ai-cli/bin/ai-mux" in lowered or lowered.endswith("/ai-mux"):
        return True
    return "tmux" in lowered and "-l ai-mux" in lowered


def _is_ai_cli_agent_command(cmd: str) -> bool:
    stripped = (cmd or "").strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    first = Path(stripped.split(None, 1)[0]).name.lower()
    if first in {"ai-cli", "claude", "codex", "copilot", "gemini"}:
        return True
    return any(
        needle in lowered
        for needle in (
            "python -m ai_cli",
            "python3 -m ai_cli",
            " @google/gemini-cli",
            "/@google/gemini-cli/",
        )
    )


def _install_helper_script(source_name: str, dest_name: str) -> str:
    src = Path(__file__).resolve().parent / source_name
    dest = Path("~/.ai-cli/bin").expanduser() / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    os.chmod(dest, 0o755)
    return str(dest)


def _install_prompt_editor_launcher() -> str:
    return _install_helper_script("prompt_editor_launcher.py", "ai-prompt-editor")


def _install_codex_personality_menu() -> str:
    return _install_helper_script(
        "codex_personality_menu.py",
        "ai-codex-personality-menu",
    )


def _spawn_detached_proxy_watcher(
    mitm_pid: int,
    session_id: str,
    tmux_sessions: list[str],
    log_path: Path,
) -> bool:
    return _mh_spawn_detached_proxy_watcher(
        mitm_pid=mitm_pid,
        session_id_value=session_id,
        tmux_sessions=tmux_sessions,
        log_path=log_path,
    )


def _tmux_list_sessions(socket_name: str = "ai-mux") -> list[str]:
    return _mh_tmux_list_sessions(socket_name=socket_name)


def _tmux_session_env(session_name: str, socket_name: str = "ai-mux") -> dict[str, str]:
    return _mh_tmux_session_env(session_name=session_name, socket_name=socket_name)


def _resolve_recv_context_file(cwd: Path) -> str:
    return _mh_resolve_recv_context_file(cwd)


def _find_reusable_tmux_session(
    tool_name: str,
    effective_cwd: Path,
    socket_name: str = "ai-mux",
) -> tuple[str, dict[str, str]] | None:
    return _mh_find_reusable_tmux_session(
        tool_name=tool_name,
        effective_cwd=effective_cwd,
        socket_name=socket_name,
    )


def _terminate_pid(pid: int, timeout_seconds: float = 3.0) -> None:
    _mh_terminate_pid(pid=pid, timeout_seconds=timeout_seconds)


def _kill_proxy_from_env(session_env: dict[str, str], log_path: Path) -> None:
    _mh_kill_proxy_from_env(session_env=session_env, log_path=log_path)


def _replace_existing_tmux_session(
    session_name: str,
    session_env: dict[str, str],
    log_path: Path,
    socket_name: str = "ai-mux",
) -> None:
    _mh_replace_existing_tmux_session(
        session_name=session_name,
        session_env=session_env,
        log_path=log_path,
        socket_name=socket_name,
    )


# ---------------------------------------------------------------------------
# Tool runner
# ---------------------------------------------------------------------------


def _parse_wrapper_overrides(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    return _mh_parse_wrapper_overrides(args)


def _extract_launch_cwd(args: list[str]) -> tuple[Path | None, list[str], RemoteSpec | None]:
    return _mh_extract_launch_cwd(args)


def _find_ai_mux() -> str | None:
    return _mh_find_ai_mux()


def _ai_mux_status() -> tuple[str, str | None]:
    return _mh_ai_mux_status()


def _default_remote_session_name(tool_name: str, remote_spec: RemoteSpec) -> str:
    digest = hashlib.sha256(f"{tool_name}:{remote_spec.display}".encode()).hexdigest()[:12]
    return f"ai-cli-{tool_name}-{digest}"


def _resolve_tool_prompt_file(config: dict[str, Any], tool_name: str) -> str:
    tools_cfg = config.get("tools", {})
    raw_tool_cfg = tools_cfg.get(tool_name, {}) if isinstance(tools_cfg, dict) else {}
    path_value = ""
    if isinstance(raw_tool_cfg, dict):
        raw = raw_tool_cfg.get("instructions_file")
        if isinstance(raw, str):
            path_value = raw.strip()
    if not path_value:
        path_value = str(Path("~/.ai-cli/instructions").expanduser() / f"{tool_name}.txt")
    return resolve_instructions_file(path_value)


def _resolve_codex_personality_file() -> str:
    return resolve_instructions_file(
        str(Path("~/.ai-cli/instructions/codex-personality.txt").expanduser())
    )


def _resolve_canary_thought_file(tool_name: str) -> str:
    """Return the path to the per-tool static canary thought file.

    The file is NOT created if absent — the proxy addon silently skips
    injection when read_text_file() returns an empty string.
    """
    return str(Path(f"~/.ai-cli/canary-thought-{tool_name}.json").expanduser())


_PROXY_STRIP_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "AI_CLI_PROXY_PID",
    "AI_CLI_PROXY_URL",
)


def _build_direct_env(extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build environment for direct launches with proxy overrides removed."""
    env = dict(os.environ)
    for key in _PROXY_STRIP_KEYS:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    apply_pinned_mitmdump_path(env)
    return env


def _warn_proxy_disabled(log_path: Path, mitm_log_path: Path, reason: str) -> None:
    """Emit a loud warning when proxy startup fails and wrapper degrades gracefully."""
    message = (
        "\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "!!                        AI-CLI PROXY DISABLED                      !!\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
        "mitmproxy/mitmdump failed to start; launching tool WITHOUT interception.\n"
        "Instruction injection and traffic capture are DISABLED for this run.\n"
        f"Reason: {reason or 'unknown proxy startup failure'}\n"
        f"Wrapper log: {log_path}\n"
        f"Proxy log:   {mitm_log_path}\n"
        "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    )
    print(message, file=sys.stderr)
    append_log(log_path, "PROXY DISABLED: running tool without MITM interception")
    if reason:
        append_log(log_path, f"Proxy failure reason: {reason}")


def run_tool(tool_name: str, args: list[str]) -> int:
    """Run a managed AI CLI tool through the mitmproxy wrapper."""
    parsed_tool_args, wrapper_overrides = _parse_wrapper_overrides(args)
    launch_cwd, parsed_tool_args, remote_spec = _extract_launch_cwd(parsed_tool_args)

    registry = load_registry()
    spec = registry.get(tool_name)
    if spec is None:
        print(f"Unknown tool: {tool_name}", file=sys.stderr)
        print(f"Available: {', '.join(registry.keys())}", file=sys.stderr)
        return 1

    config = ensure_config()
    tool_cfg = get_tool_config(config, tool_name)
    proxy_cfg = get_proxy_config(config)
    retention_cfg = get_retention_config(config)
    privacy_cfg = get_privacy_config(config)

    if not tool_cfg["enabled"]:
        print(f"Tool '{tool_name}' is disabled in config.", file=sys.stderr)
        return 1

    # Resolve binary
    use_app = wrapper_overrides.get("use_app_binary", False)
    if use_app:
        if not spec.app_binary:
            print(f"Tool '{tool_name}' has no macOS app binary configured.", file=sys.stderr)
            return 1
        if not Path(spec.app_binary).is_file():
            print(f"macOS app binary not found: {spec.app_binary}", file=sys.stderr)
            return 1
        binary = spec.app_binary
    else:
        binary = spec.resolve_binary(tool_cfg["binary"])
        if not spec.detect_installed(tool_cfg["binary"]):
            print(f"Tool binary not found: {binary}", file=sys.stderr)
            if spec.install_command:
                print(f"Install with: {spec.install_command}", file=sys.stderr)
            return 1

    # Codex-specific: check for network-proxy settings that could break our MITM
    if tool_name == "codex":
        _check_codex_proxy_compat(log_path=None)

    # Session setup
    session_id = _session_id(tool_name)
    log_dir = Path("~/.ai-cli/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{session_id}.log"
    mitm_log_path = log_dir / f"{session_id}.mitmdump.log"
    traffic_db_path = Path("~/.ai-cli/traffic.db").expanduser()

    prune_old_logs(log_dir=log_dir, max_age_days=retention_cfg["logs_days"], log_path=log_path)
    prune_old_traffic_rows(
        db_path=traffic_db_path,
        max_age_days=retention_cfg["traffic_days"],
        log_path=log_path,
    )

    # ── Compute remote mode ──────────────────────────────────────────────
    # Default for remote specs is session mode (tool runs on the remote).
    # Use --ai-cli-remote-rsync / AI_CLI_REMOTE_RSYNC=1 to force old rsync mode.
    _remote_rsync_flag = wrapper_overrides.get("remote_rsync", False) or (
        os.environ.get("AI_CLI_REMOTE_RSYNC", "").strip().lower() in {"1", "true", "yes", "on"}
    )
    _remote_session_flag = remote_spec is not None and not _remote_rsync_flag

    local_mirror: Path | None = None
    runner: RemoteSessionRunner | None = None

    # ── Remote folder proxy (rsync mode — only when explicitly requested) ─
    if remote_spec is not None and _remote_rsync_flag:
        from ai_cli.remote import (
            make_local_mirror,
            print_sync_status,
            sync_down,
            sync_up,
            verify_ssh,
        )

        print_sync_status(f"Connecting to {remote_spec.display} …")
        try:
            verify_ssh(remote_spec)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        local_mirror = make_local_mirror(remote_spec)
        print_sync_status(f"Syncing down → {local_mirror}")
        try:
            sync_down(remote_spec, local_mirror)
        except (RuntimeError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

        launch_cwd = local_mirror
        print_sync_status("Local mirror ready")

    effective_cwd = launch_cwd or Path.cwd()
    context_cwd = remote_spec.path if remote_spec is not None else str(effective_cwd)
    append_log(
        log_path,
        f"Wrapper start (ai-cli {__version__}, tool={tool_name}, cwd={effective_cwd}"
        + (f", remote={remote_spec.display}" if remote_spec else "")
        + ")",
    )

    # Build tool command early so we can rehook/replace before starting a proxy.
    tool_args = list(parsed_tool_args)
    if tool_args and tool_args[0] == "--":
        tool_args = tool_args[1:]
    if tool_args and Path(tool_args[0]).name == Path(binary).name:
        tool_args = tool_args[1:]
    tool_cmd = [binary, *tool_args]

    wrapper_flag_used = (
        wrapper_overrides["instructions_file"] is not None
        or wrapper_overrides["instructions_text"] is not None
        or wrapper_overrides["canary_rule"] is not None
        or wrapper_overrides["passthrough"] is not None
        or wrapper_overrides["debug_requests"] is not None
        or wrapper_overrides["rewrite_test_mode"] is not None
        or wrapper_overrides["developer_instructions_mode"] is not None
        or bool((wrapper_overrides["rewrite_test_tag"] or "").strip())
        or wrapper_overrides["gemini_canary_thought_injection"] is not None
        or wrapper_overrides["canary_thought_injection"] is not None
        or bool(wrapper_overrides["no_startup_context"])
        or bool(wrapper_overrides.get("use_app_binary"))
    )
    tool_flag_used = any(arg.startswith("-") for arg in tool_args)
    replace_existing = wrapper_flag_used or tool_flag_used

    reusable = _find_reusable_tmux_session(tool_name, effective_cwd, socket_name="ai-mux")
    if reusable is not None:
        existing_session_name, existing_session_env = reusable
        if replace_existing:
            append_log(
                log_path,
                f"Replacing existing tmux session for cwd/tool: {existing_session_name}",
            )
            _replace_existing_tmux_session(
                existing_session_name,
                existing_session_env,
                log_path,
                socket_name="ai-mux",
            )
        elif sys.stdin.isatty() and sys.stdout.isatty():
            if tool_args:
                append_log(
                    log_path,
                    "Rehooking existing session and ignoring forwarded non-flag args",
                )
            append_log(log_path, f"Rehooking existing tmux session: {existing_session_name}")
            attach_rc = subprocess.call(
                ["tmux", "-L", "ai-mux", "attach-session", "-t", existing_session_name]
            )
            if attach_rc == 0:
                return 0
            append_log(
                log_path,
                f"Rehook failed (rc={attach_rc}); creating parallel session instead",
            )

    append_log(log_path, f"Tool command: {fmt_cmd(tool_cmd)}")

    # Resolve instructions
    explicit_instructions_file = wrapper_overrides["instructions_file"]
    if explicit_instructions_file is not None and not explicit_instructions_file.strip():
        print(
            "--ai-cli-system-instructions-file requires a non-empty path.",
            file=sys.stderr,
        )
        return 1

    resolved_global_instructions_path = (
        explicit_instructions_file
        if explicit_instructions_file is not None
        else config.get("instructions_file", "")
    )
    try:
        instructions_file = resolve_instructions_file(resolved_global_instructions_path)
    except OSError as exc:
        append_log(log_path, str(exc))
        return 1

    canary_rule = (
        wrapper_overrides["canary_rule"]
        if wrapper_overrides["canary_rule"] is not None
        else tool_cfg["canary_rule"]
    )
    startup_context = ""
    if not wrapper_overrides["no_startup_context"]:
        startup_context = build_recent_context_for_cwd(
            context_cwd,
            remote_host=remote_spec.host if remote_spec is not None else "",
        )
    runtime_canary = canary_rule
    if startup_context:
        runtime_canary = (
            f"{canary_rule}\n\n{startup_context}" if canary_rule.strip() else startup_context
        )
        append_log(log_path, "Loaded startup recent-context block from session history.")
        print("ai-cli startup context:", file=sys.stderr)
        for line in startup_context.splitlines():
            print(f"  {line}", file=sys.stderr)

    # Inline text override (--ai-cli-instructions-text) — only passed when
    # the user explicitly provides text instead of relying on the file.
    inline_global_override = wrapper_overrides["instructions_text"]
    proxy_instructions_file = "" if inline_global_override is not None else instructions_file

    tool_prompt_file = _resolve_tool_prompt_file(config, tool_name)
    codex_personality_file = _resolve_codex_personality_file() if tool_name == "codex" else ""
    canary_thought_file = _resolve_canary_thought_file(tool_name)
    project_prompt_file = ensure_project_instructions_file(
        project_cwd=str(effective_cwd),
        remote_spec=remote_spec.display if remote_spec is not None else "",
    )
    base_prompt_file = str(resolve_base_instructions_path())

    # Addons use prompt_builder to read files fresh on each request —
    # we only pass file paths and canary_rule via --set, not text blobs.
    append_log(
        log_path,
        f"Instructions: global={instructions_file} base={base_prompt_file} "
        f"project={project_prompt_file} tool={tool_prompt_file}",
    )

    # Build addon list
    # System prompt capture must load first (before injection modifies the body)
    addons_dir = Path(__file__).resolve().parent / "addons"
    addon_paths = [str(addons_dir / "system_prompt_addon.py")]
    addon_paths.append(spec.addon_path())
    # Claude gets the credentials addon too
    if tool_name == "claude":
        addon_paths.append(str(addons_dir / "credentials_addon.py"))
    # Traffic logger — always loaded (logs all URLs, API bodies to SQLite)
    addon_paths.append(str(addons_dir / "traffic_log_addon.py"))

    # Start proxy (best effort). If it fails, continue without MITM.
    mitm_proc: subprocess.Popen[Any] | None = None
    proxy_enabled = False
    proxy_url = ""
    proxy_failure_reason = ""
    ca_path = Path(proxy_cfg["ca_path"]).expanduser()
    host = proxy_cfg["host"]
    port = 0

    try:
        mitmdump_bin = ensure_mitmdump(log_path)
        bootstrap_ca_cert(ca_path, mitmdump_bin, log_path)
        port = allocate_port(host, fallback=spec.fallback_port)
        append_log(log_path, f"Allocated port {port}")

        mitm_cmd = build_mitmdump_cmd(
            mitmdump_bin=mitmdump_bin,
            host=host,
            port=port,
            addon_paths=addon_paths,
            target_path=spec.target_path,
            wrapper_log_file=str(log_path),
            instructions_file=proxy_instructions_file,
            instructions_text=inline_global_override,
            instructions_text_explicit=(inline_global_override is not None),
            base_instructions_file=base_prompt_file,
            project_instructions_file=str(project_prompt_file),
            tool_instructions_file=tool_prompt_file,
            canary_rule=runtime_canary,
            passthrough=(
                wrapper_overrides["passthrough"]
                if wrapper_overrides["passthrough"] is not None
                else tool_cfg["passthrough"]
            ),
            debug_requests=(
                wrapper_overrides["debug_requests"]
                if wrapper_overrides["debug_requests"] is not None
                else tool_cfg["debug_requests"]
            ),
            rewrite_test_mode=(
                (wrapper_overrides["rewrite_test_mode"] or "").strip().lower()
                if tool_name == "codex" and wrapper_overrides["rewrite_test_mode"] is not None
                else ""
            ),
            developer_instructions_mode=(
                (wrapper_overrides["developer_instructions_mode"] or "").strip().lower()
                if tool_name == "codex"
                and wrapper_overrides["developer_instructions_mode"] is not None
                else (
                    (tool_cfg.get("developer_instructions_mode", "") or "").strip().lower()
                    if tool_name == "codex"
                    else ""
                )
            ),
            rewrite_test_tag=(
                (wrapper_overrides["rewrite_test_tag"] or "").strip()
                if tool_name == "codex" and wrapper_overrides["rewrite_test_tag"] is not None
                else ""
            ),
            codex_developer_prompt_file=(tool_prompt_file if tool_name == "codex" else ""),
            codex_personality_file=codex_personality_file,
            gemini_canary_thought_injection_enabled=(
                (
                    (wrapper_overrides["gemini_canary_thought_injection"] or "").strip().lower()
                    == "on"
                )
                if tool_name == "gemini"
                and wrapper_overrides["gemini_canary_thought_injection"] is not None
                else (
                    bool(tool_cfg.get("canary_thought_injection", True))
                    if tool_name == "gemini"
                    else None
                )
            ),
            canary_thought_injection_enabled=(
                (wrapper_overrides["canary_thought_injection"] or "").strip().lower() == "on"
                if wrapper_overrides["canary_thought_injection"] is not None
                else bool(tool_cfg.get("canary_thought_injection", True))
            ),
            canary_thought_file=canary_thought_file,
            traffic_caller=tool_name,
            traffic_max_age_days=retention_cfg["traffic_days"],
            traffic_redact=privacy_cfg["redact_traffic_bodies"],
            prompt_recv_prefix_file=_resolve_recv_context_file(effective_cwd),
            prompt_context_cwd=context_cwd,
        )

        proxy_host = resolve_proxy_host(host)
        proxy_url = f"http://{proxy_host}:{port}"
        append_log(log_path, f"Starting proxy at {proxy_url}")
        mitm_proc = start_proxy(mitm_cmd, log_path, mitm_log_path)
        if not verify_proxy_flow(proxy_host, port, log_path):
            raise RuntimeError("Proxy started but failed flow health check.")
        _write_session_files(session_id, port)
        proxy_enabled = True
    except (FileNotFoundError, RuntimeError, OSError) as exc:
        proxy_failure_reason = str(exc).strip()
        if mitm_proc is not None:
            try:
                stop_process(mitm_proc)
            except Exception:
                pass
            mitm_proc = None
        _warn_proxy_disabled(log_path, mitm_log_path, proxy_failure_reason)
        _cleanup_session_files(session_id)

    # Build environment
    if proxy_enabled:
        env = build_proxy_env(proxy_url, ca_path, log_path, spec.extra_env)
    else:
        env = _build_direct_env(spec.extra_env)
    prompt_editor_launcher = _install_prompt_editor_launcher()
    env["AI_CLI_TOOL"] = tool_name
    env["AI_CLI_SESSION"] = session_id
    env["AI_CLI_WORKDIR"] = str(effective_cwd)
    env["AI_CLI_BASE_PROMPT_FILE"] = base_prompt_file
    env["AI_CLI_GLOBAL_PROMPT_FILE"] = instructions_file
    env["AI_CLI_TOOL_PROMPT_FILE"] = tool_prompt_file
    env["AI_CLI_PROJECT_PROMPT_FILE"] = str(project_prompt_file)
    env["AI_CLI_PYTHON"] = sys.executable or "python3"
    env["AI_CLI_PROMPT_EDITOR_LAUNCHER"] = prompt_editor_launcher
    if tool_name == "codex":
        codex_personality_menu = _install_codex_personality_menu()
        env["AI_CLI_CODEX_PERSONALITY_MENU"] = codex_personality_menu
        if codex_personality_file:
            env["AI_CLI_CODEX_PERSONALITY_PROMPT_FILE"] = codex_personality_file
    if remote_spec is not None:
        env["AI_CLI_REMOTE_SPEC"] = remote_spec.display
    if proxy_enabled and mitm_proc is not None:
        env["AI_CLI_PROXY_PID"] = str(mitm_proc.pid)
        env["AI_CLI_PROXY_URL"] = proxy_url
    else:
        env["AI_CLI_PROXY_DISABLED"] = "1"
        if proxy_failure_reason:
            env["AI_CLI_PROXY_FAILURE_REASON"] = proxy_failure_reason

    # Codex maps Ctrl+G to "edit in external editor". Keep it enabled by
    # default; allow users to disable it explicitly for wrapped sessions.
    if tool_name == "codex":
        disable_external_editor = os.environ.get(
            "AI_CLI_CODEX_DISABLE_EXTERNAL_EDITOR", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        if disable_external_editor:
            env.pop("VISUAL", None)
            env.pop("EDITOR", None)
            append_log(
                log_path,
                "Codex external editor disabled for wrapper session "
                "(set AI_CLI_CODEX_DISABLE_EXTERNAL_EDITOR=0 to re-enable).",
            )

    _write_tracked_session(
        session_id,
        {
            "tool": tool_name,
            "cwd": str(effective_cwd),
            "wrapper_pid": os.getpid(),
            "proxy_pid": int(env.get("AI_CLI_PROXY_PID", "0") or 0),
            "proxy_url": env.get("AI_CLI_PROXY_URL", ""),
            "tmux_session": "",
            "tmux_socket": "ai-mux",
            "mux_pid": 0,
        },
    )

    # ── Remote session mode ─────────────────────────────────────────────
    # Proxy is running locally; launch the tool on the remote with an SSH
    # reverse tunnel so the remote tool's traffic flows through our proxy.
    # A "remote package" is assembled and pushed first so the tool runs
    # inside an isolated $HOME with the right configs/credentials/CA certs.
    if remote_spec is not None and _remote_session_flag:
        from ai_cli.remote import (
            RemoteSessionRunner,
            install_remote_tool,
            print_sync_status,
            resolve_remote_tool_env,
        )

        remote_init = wrapper_overrides.get("remote_init") or ""
        import shlex as _shlex

        remote_tool_cmd = spec.default_binary
        remote_tool_args_suffix = ""
        # Forward any extra tool args
        _rs_tool_args = list(parsed_tool_args)
        if _rs_tool_args and _rs_tool_args[0] == "--":
            _rs_tool_args = _rs_tool_args[1:]
        if _rs_tool_args and Path(_rs_tool_args[0]).name == Path(binary).name:
            _rs_tool_args = _rs_tool_args[1:]
        if _rs_tool_args:
            remote_tool_args_suffix = " " + " ".join(_shlex.quote(a) for a in _rs_tool_args)
            remote_tool_cmd += remote_tool_args_suffix

        _effective_proxy_port = port if proxy_enabled else 0
        _no_package = wrapper_overrides.get("remote_no_package", False) or (
            os.environ.get("AI_CLI_REMOTE_NO_PACKAGE", "").strip().lower()
            in {"1", "true", "yes", "on"}
        )

        # ── Packaged mode (default) ──────────────────────────────────────
        if not _no_package:
            from ai_cli.remote_package import (
                PackageFileEntry,
                build_package_manifest,
                ensure_remote_ai_mux_asset,
                probe_remote_session,
                pull_session_artifacts,
                push_package,
                reattach_remote_session,
                render_remote_ai_mux_config,
            )

            remote_ai_mux_binary = None
            try:
                remote_ai_mux_binary = ensure_remote_ai_mux_asset(remote_spec)
            except (FileNotFoundError, RuntimeError) as exc:
                append_log(log_path, f"Remote ai-mux unavailable, falling back to tmux: {exc}")

            package = build_package_manifest(
                tool_name,
                remote_spec,
                ca_path=ca_path if proxy_enabled else None,
                ai_mux_binary=remote_ai_mux_binary,
            )
            try:
                resolved_remote_binary, resolved_remote_path = resolve_remote_tool_env(
                    remote_spec,
                    spec.default_binary,
                    real_home=package.real_home,
                )
            except RuntimeError:
                # Tool not found — attempt auto-install
                _install_cmd = spec.get_install_command()
                if _install_cmd:
                    print_sync_status(
                        f"{tool_name} not found on {remote_spec.ssh_target}, installing..."
                    )
                    install_remote_tool(
                        remote_spec,
                        tool_name,
                        _install_cmd,
                        real_home=package.real_home,
                    )
                    resolved_remote_binary, resolved_remote_path = resolve_remote_tool_env(
                        remote_spec,
                        spec.default_binary,
                        real_home=package.real_home,
                    )
                else:
                    raise
            remote_tool_cmd = _shlex.quote(resolved_remote_binary) + remote_tool_args_suffix
            remote_tool_cmd_parts = [resolved_remote_binary, *_rs_tool_args]

            remote_mux_env = {
                "AI_CLI_PYTHON": "python3",
                "AI_CLI_PROMPT_EDITOR_LAUNCHER": f"{package.session_dir}/.ai-cli/bin/ai-prompt-editor",
                "AI_CLI_GLOBAL_PROMPT_FILE": f"{package.session_dir}/.ai-cli/system_instructions.txt",
                "AI_CLI_BASE_PROMPT_FILE": f"{package.session_dir}/.ai-cli/base_instructions.txt",
                "AI_CLI_TOOL_PROMPT_FILE": f"{package.session_dir}/.ai-cli/instructions/{tool_name}.txt",
                "AI_CLI_PROJECT_PROMPT_FILE": f"{package.session_dir}/{package.project_prompt_rel_path}",
                "AI_CLI_TOOL": tool_name,
                "AI_CLI_WORKDIR": remote_spec.path,
                "PATH": resolved_remote_path,
                "REAL_HOME": package.real_home,
                "HOME": package.session_dir,
                "ZDOTDIR": package.session_dir,
                "BASH_ENV": f"{package.session_dir}/.bash_env",
                "ENV": f"{package.session_dir}/.shrc",
                "KSHRC": f"{package.session_dir}/.kshrc",
            }
            if tool_name == "codex":
                remote_mux_env["AI_CLI_CODEX_PERSONALITY_PROMPT_FILE"] = (
                    f"{package.session_dir}/.ai-cli/instructions/codex-personality.txt"
                )
            if _effective_proxy_port:
                proxy_url = f"http://127.0.0.1:{_effective_proxy_port}"
                remote_ca = f"{package.session_dir}/.ai-cli/remote-ca.pem"
                remote_mux_env.update(
                    {
                        "HTTP_PROXY": proxy_url,
                        "HTTPS_PROXY": proxy_url,
                        "http_proxy": proxy_url,
                        "https_proxy": proxy_url,
                        "SSL_CERT_FILE": remote_ca,
                        "REQUESTS_CA_BUNDLE": remote_ca,
                        "NODE_EXTRA_CA_CERTS": remote_ca,
                    }
                )

            ai_mux_command_parts = remote_tool_cmd_parts
            if remote_init:
                ai_mux_command_parts = [
                    "sh",
                    "-lc",
                    f"{remote_init} && exec {remote_tool_cmd}",
                ]

            if remote_ai_mux_binary is not None:
                package.entries.append(
                    PackageFileEntry(
                        remote_rel_path=".ai-cli/ai-mux.json",
                        content=render_remote_ai_mux_config(
                            tool_name=tool_name,
                            session_name=package.session_name,
                            command=ai_mux_command_parts,
                            cwd=remote_spec.path,
                            env=remote_mux_env,
                        ),
                    )
                )

            append_log(
                log_path,
                f"Remote session mode (packaged): {remote_spec.display} "
                f"session={package.session_name} home={package.session_dir} "
                f"proxy={'yes' if proxy_enabled else 'no'}",
            )

            try:
                print_sync_status(
                    f"Pushing config package → {remote_spec.ssh_target}:{package.session_dir}"
                )
                push_package(package, remote_spec)

                if remote_ai_mux_binary is not None:
                    pkg_runner = RemoteSessionRunner(
                        spec=remote_spec,
                        session_name=package.session_name,
                    )
                    ai_mux_launch = (
                        f"{_shlex.quote(package.session_dir + '/.ai-cli/bin/ai-mux')} "
                        f"--config {_shlex.quote(package.session_dir + '/.ai-cli/ai-mux.json')} "
                        f"--session-name {_shlex.quote(package.session_name)} "
                        f"--socket-name {_shlex.quote(package.tmux_socket)}"
                    )
                    exit_code = pkg_runner.exec_attached(
                        command=ai_mux_launch,
                        proxy_port=_effective_proxy_port,
                        home_dir=package.session_dir,
                        real_home=package.real_home,
                        launch_path=resolved_remote_path,
                    )
                    append_log(log_path, f"Remote ai-mux session exit code: {exit_code}")
                else:
                    # Check for an existing session — reattach without re-pushing
                    if probe_remote_session(package, remote_spec):
                        print_sync_status(f"Existing session found: {package.session_name}")
                        exit_code = reattach_remote_session(
                            package,
                            remote_spec,
                            proxy_port=_effective_proxy_port,
                        )
                        append_log(log_path, f"Remote reattach exit code: {exit_code}")
                    else:
                        pkg_runner = RemoteSessionRunner(
                            spec=remote_spec,
                            session_name=package.session_name,
                        )
                        cd_cmd = f"cd {_shlex.quote(remote_spec.path)}"
                        if remote_init:
                            init_sequence = f"{cd_cmd} && {remote_init}"
                        else:
                            init_sequence = cd_cmd

                        exit_code = pkg_runner.run_attached(
                            command=remote_tool_cmd,
                            init_cmd=init_sequence,
                            proxy_port=_effective_proxy_port,
                            home_dir=package.session_dir,
                            real_home=package.real_home,
                            launch_path=resolved_remote_path,
                            tmux_socket=package.tmux_socket,
                        )
                        append_log(log_path, f"Remote session exit code: {exit_code}")
            except (FileNotFoundError, RuntimeError) as exc:
                print(f"Remote session failed: {exc}", file=sys.stderr)
                append_log(log_path, f"Remote session failed: {exc}")
                exit_code = 1
            finally:
                try:
                    pull_session_artifacts(package, remote_spec, log_dir)
                except Exception as exc:
                    append_log(log_path, f"Remote artifact pull failed: {exc}")
                if mitm_proc is not None:
                    try:
                        stop_process(mitm_proc)
                    except Exception:
                        pass
                _cleanup_session_files(session_id)
                _remove_tracked_session(session_id)
                append_log(log_path, "Wrapper stop (remote session, packaged)")
            return exit_code

        # ── No-package mode (raw $HOME, no isolation) ────────────────────
        remote_session_name = wrapper_overrides.get(
            "remote_session_name"
        ) or _default_remote_session_name(tool_name, remote_spec)
        append_log(
            log_path,
            f"Remote session mode (no-package): {remote_spec.display} "
            f"session={remote_session_name} "
            f"proxy={'yes' if proxy_enabled else 'no'}",
        )

        try:
            runner = RemoteSessionRunner(
                spec=remote_spec,
                session_name=remote_session_name,
            )
            import shlex as _shlex

            cd_cmd = f"cd {_shlex.quote(remote_spec.path)}"
            if remote_init:
                init_sequence = f"{cd_cmd} && {remote_init}"
            else:
                init_sequence = cd_cmd

            exit_code = runner.run_attached(
                command=remote_tool_cmd,
                init_cmd=init_sequence,
                proxy_port=_effective_proxy_port,
                ca_path=ca_path if proxy_enabled else None,
            )
            append_log(log_path, f"Remote session exit code: {exit_code}")
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"Remote session failed: {exc}", file=sys.stderr)
            append_log(log_path, f"Remote session failed: {exc}")
            exit_code = 1
        finally:
            if runner is not None:
                try:
                    runner.pull_logs(log_dir)
                except Exception as exc:
                    append_log(log_path, f"Remote log pull failed: {exc}")
            if mitm_proc is not None:
                try:
                    stop_process(mitm_proc)
                except Exception:
                    pass
            _cleanup_session_files(session_id)
            _remove_tracked_session(session_id)
            append_log(log_path, "Wrapper stop (remote session, no-package)")
        return exit_code

    # Run the tool via PTY multiplexer (TTY) or plain subprocess (non-TTY)
    def _truthy_env(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    exit_code = 1
    keep_proxy_running = False
    remote_sync_deferred = False
    force_mux_for_claude = _truthy_env("AI_CLI_CLAUDE_USE_MUX")
    use_mux = (
        sys.stdin.isatty()
        and sys.stdout.isatty()
        and (tool_name != "claude" or force_mux_for_claude)
    )
    try:
        if use_mux:
            python = sys.executable or shutil.which("python3") or "python3"
            base_env = dict(os.environ)
            ai_mux_bin = _find_ai_mux()

            if not ai_mux_bin:
                append_log(log_path, "ai-mux binary not found")
                return 1

            mux_config = {
                "session_name": f"ai-{session_id}",
                "tabs": [
                    {
                        "label": tool_name,
                        "cmd": tool_cmd,
                        "env": env,
                        "cwd": str(effective_cwd),
                        "primary": True,
                    },
                    {
                        "label": "sessions",
                        "cmd": [python, "-m", "ai_cli", "session", "--list"],
                        "env": base_env,
                        "cwd": str(effective_cwd),
                        "primary": False,
                    },
                ],
            }

            config_path = ""
            try:
                owned_session = f"ai-{session_id}"
                _write_tracked_session(
                    session_id,
                    {
                        "tmux_session": owned_session,
                        "tmux_socket": "ai-mux",
                    },
                )
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    prefix="ai-mux-",
                    suffix=".json",
                    delete=False,
                ) as f:
                    json.dump(mux_config, f)
                    config_path = f.name

                append_log(log_path, f"Starting ai-mux (tmux): {ai_mux_bin}")
                sys.stdout.flush()
                sys.stderr.flush()
                mux_proc = subprocess.Popen(
                    [ai_mux_bin, "--config", config_path, "--session-name", f"ai-{session_id}"]
                )
                _write_tracked_session(
                    session_id,
                    {
                        "mux_pid": mux_proc.pid,
                    },
                )
                exit_code = mux_proc.wait()
                append_log(log_path, f"ai-mux exit code: {exit_code}")

                if _tmux_has_session(owned_session):
                    remote_sync_deferred = True
                    if proxy_enabled and mitm_proc is not None:
                        append_log(
                            log_path,
                            "tmux session still alive (detached); leaving proxy running "
                            f"for session: {owned_session}",
                        )
                        _spawn_detached_proxy_watcher(
                            mitm_pid=mitm_proc.pid,
                            session_id=session_id,
                            tmux_sessions=[owned_session],
                            log_path=log_path,
                        )
                        keep_proxy_running = True
                    else:
                        append_log(
                            log_path,
                            "tmux session still alive (detached) with proxy disabled.",
                        )
                    return exit_code
                append_log(
                    log_path,
                    f"owned tmux session not found ({owned_session}); stopping proxy",
                )
            except OSError as exc:
                append_log(log_path, f"ai-mux launch failed: {exc}")
            finally:
                if config_path:
                    try:
                        Path(config_path).unlink(missing_ok=True)
                    except OSError:
                        pass
        else:
            if sys.stdin.isatty() and sys.stdout.isatty() and tool_name == "claude":
                append_log(
                    log_path,
                    "Launching Claude in direct TTY mode "
                    "(set AI_CLI_CLAUDE_USE_MUX=1 to force ai-mux).",
                )
            # Non-TTY fallback: plain subprocess
            child_proc = subprocess.Popen(tool_cmd, env=env, cwd=str(effective_cwd))
            exit_code = child_proc.wait()
            append_log(log_path, f"Tool exit code: {exit_code}")

        return exit_code
    finally:
        # ── Remote sync-up ───────────────────────────────────────────────
        if remote_spec is not None and not remote_sync_deferred and local_mirror is not None:
            from ai_cli.remote import print_sync_status, sync_up

            print_sync_status(f"Syncing edits back to {remote_spec.display} …")
            try:
                sync_up(remote_spec, local_mirror)
                print_sync_status("Upload complete ✓")
                append_log(log_path, f"Remote sync-up complete: {remote_spec.display}")
            except (RuntimeError, FileNotFoundError) as exc:
                print_sync_status(f"Upload FAILED: {exc}")
                append_log(log_path, f"Remote sync-up failed: {exc}")
        elif remote_spec is not None and remote_sync_deferred:
            append_log(
                log_path,
                f"Remote sync-up deferred; tmux session still active for {remote_spec.display}",
            )

        # Ensure proxy is stopped when the session has actually ended.
        if keep_proxy_running:
            append_log(
                log_path,
                "Wrapper stop (detached tmux session still running; proxy and session files left alive)",
            )
        else:
            if mitm_proc is not None:
                try:
                    stop_process(mitm_proc)
                except Exception:
                    pass
            _cleanup_session_files(session_id)
            _remove_tracked_session(session_id)
            append_log(log_path, "Wrapper stop")


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _cmd_system_prompt(model_query: str) -> int:
    """Cat a captured system prompt by model name (fuzzy match)."""
    import sqlite3
    from difflib import get_close_matches

    db_path = Path.home() / ".ai-cli" / "system_prompts.db"
    if not db_path.is_file():
        print("No system prompts captured yet.", file=sys.stderr)
        print(f"(Expected database at {db_path})", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    all_rows = conn.execute(
        "SELECT id, provider, model, role, content, char_count, last_seen "
        "FROM system_prompts ORDER BY last_seen DESC"
    ).fetchall()
    if not all_rows:
        print("No system prompts captured yet.", file=sys.stderr)
        conn.close()
        return 1

    # If no query, list all available
    if not model_query:
        print(f"{'Provider':<12} {'Model':<28} {'Role':<14} {'Chars':>7}  Last Seen")
        print("-" * 90)
        for r in all_rows:
            last = (r["last_seen"] or "?")[:19]
            print(
                f"{r['provider']:<12} {r['model']:<28} {r['role']:<14} {r['char_count']:>7}  {last}"
            )
        print()
        print("Usage: ai-cli system prompt <model>", file=sys.stderr)
        conn.close()
        return 0

    query = model_query.lower().strip()

    # Build lookup: try exact match first, then substring, then fuzzy
    # Combine "provider/model/role" as matchable keys
    candidates: dict[str, list[sqlite3.Row]] = {}
    for r in all_rows:
        for key in (
            r["model"],
            f"{r['provider']}/{r['model']}",
            f"{r['provider']}/{r['model']}/{r['role']}",
        ):
            candidates.setdefault(key.lower(), []).append(r)

    # 1. Exact match
    matched = candidates.get(query)

    # 2. Substring match
    if not matched:
        matched = []
        for r in all_rows:
            combined = f"{r['provider']} {r['model']} {r['role']}".lower()
            if query in combined:
                matched.append(r)

    # 3. Fuzzy match on model names
    if not matched:
        all_models = list({r["model"] for r in all_rows})
        close = get_close_matches(query, [m.lower() for m in all_models], n=3, cutoff=0.4)
        if close:
            best = close[0]
            matched = [r for r in all_rows if r["model"].lower() == best]

    conn.close()

    if not matched:
        print(f"No system prompt matching '{model_query}'.", file=sys.stderr)
        print("Available models:", file=sys.stderr)
        seen = set()
        for r in all_rows:
            key = f"  {r['provider']}/{r['model']} ({r['role']})"
            if key not in seen:
                seen.add(key)
                print(key, file=sys.stderr)
        return 1

    # Print all matching prompts (may be multiple roles for same model)
    for i, r in enumerate(matched):
        if i > 0:
            print()
            print("═" * 80)
            print()
        print(f"# {r['provider']}/{r['model']} [{r['role']}]", file=sys.stderr)
        print(
            f"# {r['char_count']} chars, last seen {(r['last_seen'] or '?')[:19]}", file=sys.stderr
        )
        print(r["content"])

    return 0


def cmd_status() -> int:
    """Show installed tools, versions, and alias state."""
    config = ensure_config()
    registry = load_registry()
    aliases = config.get("aliases", {})

    print(f"ai-cli v{__version__}")
    mux_mode, mux_path = _ai_mux_status()
    if mux_path:
        print(f"PTY mux: ai-mux ({mux_mode})")
        print(f"  {mux_path}")
    else:
        print("PTY mux: ai-mux NOT FOUND (install tmux)")
    print()
    for name, spec in registry.items():
        tool_cfg = get_tool_config(config, name)
        installed = spec.detect_installed(tool_cfg["binary"])
        version = spec.get_version(tool_cfg["binary"]) if installed else None
        alias_state = "aliased" if aliases.get(name) else "no alias"
        status = "installed" if installed else "not found"
        version_str = f" ({version})" if version else ""
        enabled = "enabled" if tool_cfg["enabled"] else "disabled"
        print(f"  {spec.display_name:<20} {status}{version_str} [{enabled}, {alias_state}]")
    return 0


def _collect_cleanup_targets(include_all: bool = False) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    tracked_session_names: set[str] = set()
    seen_pids: set[int] = {os.getpid()}

    for payload in _list_tracked_sessions():
        session_id = str(payload.get("session_id", "")).strip()
        session_name = str(payload.get("tmux_session", "")).strip()
        tool = str(payload.get("tool", "?")).strip() or "?"
        cwd = str(payload.get("cwd", "?")).strip() or "?"
        proxy_pid = int(payload.get("proxy_pid", 0) or 0)
        wrapper_pid = int(payload.get("wrapper_pid", 0) or 0)
        mux_pid = int(payload.get("mux_pid", 0) or 0)
        tmux_running = _tmux_has_session(session_name, socket_name="ai-mux")
        proxy_running = _pid_alive(proxy_pid)
        session_part = session_name if session_name else "none"
        status_parts = [
            f"tmux={'running' if tmux_running else 'missing'}:{session_part}",
            f"proxy={'running' if proxy_running else 'missing'}:{proxy_pid or 0}",
        ]
        if wrapper_pid > 0:
            status_parts.append(f"wrapper={wrapper_pid}")
        if mux_pid > 0:
            status_parts.append(f"mux={mux_pid}")
        targets.append(
            {
                "kind": "tracked",
                "session_id": session_id,
                "payload": payload,
                "label": (
                    f"[tracked] session_id={session_id or '?'} "
                    f"(tool={tool}, cwd={cwd}, {', '.join(status_parts)})"
                ),
            }
        )
        if session_name:
            tracked_session_names.add(session_name)
        for pid in (proxy_pid, wrapper_pid, mux_pid):
            if pid > 0:
                seen_pids.add(pid)

    if not include_all:
        return targets

    for session_name in _tmux_list_sessions(socket_name="ai-mux"):
        if session_name in tracked_session_names:
            continue
        session_env = _tmux_session_env(session_name, socket_name="ai-mux")
        tool = session_env.get("AI_CLI_TOOL", "?")
        cwd = session_env.get("AI_CLI_WORKDIR", "?")
        proxy_pid_raw = session_env.get("AI_CLI_PROXY_PID", "").strip()
        proxy_part = f", proxy_pid={proxy_pid_raw}" if proxy_pid_raw else ""
        targets.append(
            {
                "kind": "tmux",
                "session_name": session_name,
                "session_env": session_env,
                "label": (
                    f"[mux-scan] session={session_name} (tool={tool}, cwd={cwd}{proxy_part})"
                ),
            }
        )
        if proxy_pid_raw.isdigit():
            seen_pids.add(int(proxy_pid_raw))

    try:
        probe = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        probe = None

    if probe is not None:
        for raw in (probe.stdout or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if not parts or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            cmd = parts[1] if len(parts) > 1 else ""
            if pid in seen_pids:
                continue
            if _is_ai_cli_proxy_command(cmd):
                targets.append(
                    {
                        "kind": "proxy",
                        "pid": pid,
                        "label": f"[proxy-scan] pid={pid} cmd={cmd}",
                    }
                )
                seen_pids.add(pid)
                continue
            if _is_ai_mux_command(cmd):
                targets.append(
                    {
                        "kind": "mux_process",
                        "pid": pid,
                        "label": f"[mux-proc] pid={pid} cmd={cmd}",
                    }
                )
                seen_pids.add(pid)
                continue
            if _is_ai_cli_agent_command(cmd):
                targets.append(
                    {
                        "kind": "agent",
                        "pid": pid,
                        "label": f"[agent-scan] pid={pid} cmd={cmd}",
                    }
                )
                seen_pids.add(pid)

    return targets


def _parse_cleanup_selection(raw: str, total: int) -> list[int]:
    text = (raw or "").strip().lower()
    if not text:
        return []
    if text in {"a", "all"}:
        return list(range(total))

    selected: list[int] = []
    seen: set[int] = set()
    for piece in text.split(","):
        item = piece.strip()
        if not item:
            continue
        if not item.isdigit():
            return []
        index = int(item) - 1
        if index < 0 or index >= total:
            return []
        if index in seen:
            continue
        seen.add(index)
        selected.append(index)
    return selected


def cmd_cleanup(args: list[str]) -> int:
    parser = argparse.ArgumentParser(add_help=False, prog="ai-cli cleanup")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--select", default="")
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    known, unknown = parser.parse_known_args(args)

    if known.help:
        print("Usage: ai-cli cleanup [--list] [--all | --select 1,2,3] [-y]")
        print("  --list        Show detected tracked-session targets without cleaning")
        print(
            "  --all         Deep-scan and kill tracked sessions plus host ai-mux/agent processes"
        )
        print("  --select      Comma-separated item numbers from the target list")
        print("  -y, --yes     Skip confirmation prompt")
        return 0

    if unknown:
        print(f"Unknown cleanup arguments: {' '.join(unknown)}", file=sys.stderr)
        return 1

    targets = _collect_cleanup_targets(include_all=known.all)
    if not targets:
        print("No ai-cli cleanup targets found.")
        return 0

    print("Cleanup targets:")
    for idx, target in enumerate(targets, start=1):
        print(f"  {idx}. {target['label']}")

    if known.list:
        return 0

    selected_indexes: list[int] = []
    if known.all:
        selected_indexes = list(range(len(targets)))
    elif known.select:
        selected_indexes = _parse_cleanup_selection(known.select, len(targets))
        if not selected_indexes:
            print("Invalid --select value. Use comma-separated item numbers.", file=sys.stderr)
            return 1
    else:
        if not sys.stdin.isatty():
            print(
                "Non-interactive cleanup requires --all or --select.",
                file=sys.stderr,
            )
            return 1
        selection = input("Select items to kill (e.g. 1,2 or 'all', blank to cancel): ")
        selected_indexes = _parse_cleanup_selection(selection, len(targets))
        if not selected_indexes:
            print("No targets selected; nothing killed.")
            return 0

    selected_targets = [targets[i] for i in selected_indexes]
    if not known.yes:
        if not sys.stdin.isatty():
            print("Use --yes in non-interactive mode.", file=sys.stderr)
            return 1
        confirm = input(f"Kill {len(selected_targets)} selected target(s)? [y/N]: ").strip().lower()
        if confirm not in {"y", "yes"}:
            print("Cancelled; nothing killed.")
            return 0

    log_dir = Path("~/.ai-cli/logs").expanduser()
    log_dir.mkdir(parents=True, exist_ok=True)
    cleanup_log = log_dir / f"cleanup-{os.getpid()}-{time.time_ns()}.log"
    append_log(cleanup_log, "Manual cleanup start")

    killed = 0
    for target in selected_targets:
        kind = str(target.get("kind", ""))
        if kind == "tracked":
            payload = target.get("payload", {})
            if not isinstance(payload, dict):
                continue
            session_id = str(target.get("session_id", "")).strip()
            session_name = str(payload.get("tmux_session", "")).strip()
            proxy_pid = int(payload.get("proxy_pid", 0) or 0)
            if session_name and _tmux_has_session(session_name, socket_name="ai-mux"):
                session_env = _tmux_session_env(session_name, socket_name="ai-mux")
                _replace_existing_tmux_session(
                    session_name,
                    session_env,
                    cleanup_log,
                    socket_name="ai-mux",
                )
                print(f"Cleaned tracked ai-mux session: {session_name}")
            elif proxy_pid > 0:
                _terminate_pid(proxy_pid)
                append_log(cleanup_log, f"Stopped tracked standalone proxy pid: {proxy_pid}")
                print(f"Stopped tracked proxy PID: {proxy_pid}")
            if session_id:
                _cleanup_session_files(session_id)
                _remove_tracked_session(session_id)
            killed += 1
            continue
        if kind == "tmux":
            session_name = str(target.get("session_name", ""))
            session_env = target.get("session_env", {})
            if session_name and isinstance(session_env, dict):
                _replace_existing_tmux_session(
                    session_name,
                    session_env,
                    cleanup_log,
                    socket_name="ai-mux",
                )
                print(f"Killed tmux session: {session_name}")
                killed += 1
            continue
        if kind == "proxy":
            pid = int(target.get("pid", 0) or 0)
            if pid > 0:
                _terminate_pid(pid)
                append_log(cleanup_log, f"Killed standalone proxy pid: {pid}")
                print(f"Killed proxy PID: {pid}")
                killed += 1
            continue
        if kind == "mux_process":
            pid = int(target.get("pid", 0) or 0)
            if pid > 0:
                _terminate_pid(pid)
                append_log(cleanup_log, f"Killed ai-mux process pid: {pid}")
                print(f"Killed ai-mux PID: {pid}")
                killed += 1
            continue
        if kind == "agent":
            pid = int(target.get("pid", 0) or 0)
            if pid > 0:
                _terminate_pid(pid)
                append_log(cleanup_log, f"Killed agent pid: {pid}")
                print(f"Killed agent PID: {pid}")
                killed += 1

    append_log(cleanup_log, f"Manual cleanup complete. killed={killed}")
    print(f"Cleanup complete. cleaned={killed}. log={cleanup_log}")
    return 0


def _cmd_prompt_edit(scope: str, tool_arg: str = "") -> int:
    config = ensure_config()
    normalized = (scope or "").strip().lower()
    if normalized == "global":
        return edit_instructions(config.get("instructions_file", ""))

    if normalized == "tool":
        registry = load_registry()
        tool_name = (
            (tool_arg or "").strip()
            or os.environ.get("AI_CLI_TOOL", "").strip()
            or str(config.get("default_tool", "") or "").strip()
        )
        if tool_name not in registry:
            print(f"Unknown tool for prompt-edit: {tool_name or '(empty)'}", file=sys.stderr)
            print(f"Available: {', '.join(registry.keys())}", file=sys.stderr)
            return 1
        return edit_instructions(_resolve_tool_prompt_file(config, tool_name))

    print("Usage: ai-cli prompt-edit <global|tool> [tool]", file=sys.stderr)
    return 1


def _cmd_system_browser(argv: list[str]) -> int:
    from ai_cli.system_prompts import main as system_prompts_main

    return system_prompts_main(argv)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> int:
    # Binary-name routing: check argv[0]
    invoked_name = Path(sys.argv[0]).name
    tool_from_name = TOOL_ALIASES.get(invoked_name)
    if tool_from_name:
        return run_tool(tool_from_name, sys.argv[1:])

    # Subcommand routing
    if len(sys.argv) < 2:
        # No args — open interactive menu
        from ai_cli.tui import interactive_menu

        return interactive_menu()

    subcommand = sys.argv[1]

    # Direct tool names
    registry = load_registry()
    if subcommand in registry:
        return run_tool(subcommand, sys.argv[2:])

    # Management subcommands
    if subcommand == "system":
        if len(sys.argv) > 2 and sys.argv[2] == "edit":
            scope = sys.argv[3] if len(sys.argv) > 3 else "global"
            tool_name = sys.argv[4] if len(sys.argv) > 4 else ""
            return _cmd_prompt_edit(scope, tool_name)
        if len(sys.argv) > 2 and sys.argv[2] == "prompt":
            return _cmd_system_browser(sys.argv[3:])
        return _cmd_system_browser(sys.argv[2:])

    if subcommand == "prompt-edit":
        scope = sys.argv[2] if len(sys.argv) > 2 else ""
        tool_name = sys.argv[3] if len(sys.argv) > 3 else ""
        return _cmd_prompt_edit(scope, tool_name)

    if subcommand == "status":
        return cmd_status()

    if subcommand == "cleanup":
        return cmd_cleanup(sys.argv[2:])

    if subcommand == "canary-capture":
        from ai_cli.canary_capture import cmd_canary_capture

        return cmd_canary_capture(sys.argv[2:])

    if subcommand == "canary-seed":
        from ai_cli.canary_capture import cmd_canary_seed

        return cmd_canary_seed(sys.argv[2:])

    if subcommand in ("session", "history"):
        from ai_cli.session import main as session_main

        return session_main(sys.argv[2:])

    if subcommand == "traffic":
        from ai_cli.traffic import main as traffic_main

        return traffic_main(sys.argv[2:])

    if subcommand == "update":
        from ai_cli.update import main as update_main

        return update_main(sys.argv[2:])

    if subcommand == "completions":
        from ai_cli.completion_gen import main as completion_main

        return completion_main(sys.argv[2:])

    if subcommand == "menu":
        from ai_cli.tui import interactive_menu

        return interactive_menu()

    if subcommand in ("--version", "-v"):
        print(f"ai-cli {__version__}")
        return 0

    if subcommand in ("--help", "-h", "help"):
        print(f"ai-cli {__version__} — Unified AI CLI wrapper")
        print()
        print("Usage:")
        print("  ai-cli <tool> [DIR] [args...]  Launch a tool (claude, codex, copilot, gemini)")
        print("  ai-cli menu               Interactive tool manager (TUI)")
        print("  ai-cli system [opts]      Browse captured system prompts")
        print("  ai-cli system edit ...    Edit global/tool prompt files")
        print("  ai-cli prompt-edit ...    Edit global/tool prompt files")
        print("  ai-cli system prompt [query]  Browse historical or parsed system prompts")
        print("  ai-cli status             Show installed tools and versions")
        print(
            "  ai-cli cleanup [opts]     Kill tracked sessions or deep-scan ai-mux/agent processes"
        )
        print("  ai-cli history [opts]     Browse agent conversation history")
        print("  ai-cli traffic [opts]     Browse proxied API traffic")
        print("  ai-cli update [opts]      Install or update wrapped tools")
        print("  ai-cli completions ...    Generate completion scripts")
        print("  ai-cli --version          Show version")
        print()
        print("When installed as an alias (e.g., 'claude'), routes automatically.")
        return 0

    # Unknown — try as tool name
    print(f"Unknown subcommand: {subcommand}", file=sys.stderr)
    print("Run 'ai-cli --help' for usage.", file=sys.stderr)
    return 1


def main_cli() -> None:
    """Console script entry point (raises SystemExit)."""
    raise SystemExit(main())


if __name__ == "__main__":
    main_cli()

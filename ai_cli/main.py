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
    compose_instructions,
    edit_instructions,
    resolve_instructions_file,
    resolve_user_instructions,
)
from ai_cli.log import append_log, fmt_cmd
from ai_cli.main_helpers import (
    ai_mux_status as _mh_ai_mux_status,
    check_codex_proxy_compat as _mh_check_codex_proxy_compat,
    cleanup_session_files as _mh_cleanup_session_files,
    extract_launch_cwd as _mh_extract_launch_cwd,
    find_ai_mux as _mh_find_ai_mux,
    find_reusable_tmux_session as _mh_find_reusable_tmux_session,
    kill_proxy_from_env as _mh_kill_proxy_from_env,
    parse_wrapper_overrides as _mh_parse_wrapper_overrides,
    replace_existing_tmux_session as _mh_replace_existing_tmux_session,
    resolve_recv_context_file as _mh_resolve_recv_context_file,
    session_id as _mh_session_id,
    spawn_detached_proxy_watcher as _mh_spawn_detached_proxy_watcher,
    terminate_pid as _mh_terminate_pid,
    tmux_list_sessions as _mh_tmux_list_sessions,
    tmux_session_env as _mh_tmux_session_env,
    write_session_files as _mh_write_session_files,
)
from ai_cli.session import build_recent_context_for_cwd
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
from ai_cli.tools import TOOL_ALIASES, load_registry, ToolSpec


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


def _extract_launch_cwd(args: list[str]) -> tuple[Path | None, list[str]]:
    return _mh_extract_launch_cwd(args)


def _find_ai_mux() -> str | None:
    return _mh_find_ai_mux()


def _ai_mux_status() -> tuple[str, str | None]:
    return _mh_ai_mux_status()


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
    launch_cwd, parsed_tool_args = _extract_launch_cwd(parsed_tool_args)

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

    effective_cwd = launch_cwd or Path.cwd()
    append_log(
        log_path,
        f"Wrapper start (ai-cli {__version__}, tool={tool_name}, cwd={effective_cwd})",
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
        or bool((wrapper_overrides["instructions_text"] or "").strip())
        or wrapper_overrides["canary_rule"] is not None
        or wrapper_overrides["passthrough"] is not None
        or wrapper_overrides["debug_requests"] is not None
        or wrapper_overrides["rewrite_test_mode"] is not None
        or wrapper_overrides["developer_instructions_mode"] is not None
        or bool((wrapper_overrides["rewrite_test_tag"] or "").strip())
        or bool(wrapper_overrides["no_startup_context"])
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
    resolved_global_instructions_path = (
        wrapper_overrides["instructions_file"]
        if wrapper_overrides["instructions_file"] is not None
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
        startup_context = build_recent_context_for_cwd(str(effective_cwd))
    runtime_canary = canary_rule
    if startup_context:
        runtime_canary = (
            f"{canary_rule}\n\n{startup_context}"
            if canary_rule.strip()
            else startup_context
        )
        append_log(log_path, "Loaded startup recent-context block from session history.")
        print("ai-cli startup context:", file=sys.stderr)
        for line in startup_context.splitlines():
            print(f"  {line}", file=sys.stderr)

    global_prompt_text = ""
    inline_global_override = (wrapper_overrides["instructions_text"] or "").strip()
    if inline_global_override:
        global_prompt_text = inline_global_override
    else:
        global_prompt_text = resolve_user_instructions(instructions_file)

    tool_prompt_file = _resolve_tool_prompt_file(config, tool_name)
    tool_prompt_text = resolve_user_instructions(tool_prompt_file)

    # Build layered global guidelines text (canary + base + project + user).
    # Tool-specific prompt text is passed separately and inserted as its own section.
    layered_global_text = compose_instructions(
        canary_rule=runtime_canary,
        tool_name="",
        instructions_text=global_prompt_text,
        instructions_file=instructions_file,
        project_cwd=str(effective_cwd),
    )

    effective_text = compose_instructions(
        canary_rule=runtime_canary,
        tool_name=tool_name,
        instructions_text=global_prompt_text,
        instructions_file=instructions_file,
        project_cwd=str(effective_cwd),
    )
    effective_sha = hashlib.sha256(effective_text.encode("utf-8")).hexdigest()
    append_log(
        log_path,
        f"Instructions: chars={len(effective_text)} sha256={effective_sha}",
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
            instructions_file=instructions_file,
            instructions_text=layered_global_text,
            tool_instructions_text=tool_prompt_text,
            canary_rule="",
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
                if tool_name == "codex" and wrapper_overrides["developer_instructions_mode"] is not None
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
            codex_developer_prompt_file=(
                tool_prompt_file if tool_name == "codex" else ""
            ),
            traffic_caller=tool_name,
            traffic_max_age_days=retention_cfg["traffic_days"],
            traffic_redact=privacy_cfg["redact_traffic_bodies"],
            prompt_recv_prefix_file=_resolve_recv_context_file(effective_cwd),
            prompt_context_cwd=str(effective_cwd),
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
    env["AI_CLI_TOOL"] = tool_name
    env["AI_CLI_SESSION"] = session_id
    env["AI_CLI_WORKDIR"] = str(effective_cwd)
    env["AI_CLI_GLOBAL_PROMPT_FILE"] = instructions_file
    env["AI_CLI_TOOL_PROMPT_FILE"] = tool_prompt_file
    env["AI_CLI_PYTHON"] = sys.executable or "python3"
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
        disable_external_editor = (
            os.environ.get("AI_CLI_CODEX_DISABLE_EXTERNAL_EDITOR", "")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )
        if disable_external_editor:
            env.pop("VISUAL", None)
            env.pop("EDITOR", None)
            append_log(
                log_path,
                "Codex external editor disabled for wrapper session "
                "(set AI_CLI_CODEX_DISABLE_EXTERNAL_EDITOR=0 to re-enable).",
            )

    # Run the tool via PTY multiplexer (TTY) or plain subprocess (non-TTY)
    def _truthy_env(name: str) -> bool:
        return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}

    def _tmux_has_session(session_name: str) -> bool:
        try:
            code = subprocess.call(
                ["tmux", "-L", "ai-mux", "has-session", "-t", session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        return code == 0

    exit_code = 1
    keep_proxy_running = False
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
                    {
                        "label": "status",
                        "cmd": [python, "-m", "ai_cli", "status"],
                        "env": base_env,
                        "cwd": str(effective_cwd),
                        "primary": False,
                    },
                ]
            }

            config_path = ""
            try:
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
                exit_code = mux_proc.wait()
                append_log(log_path, f"ai-mux exit code: {exit_code}")

                owned_session = f"ai-{session_id}"
                if _tmux_has_session(owned_session):
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
            child_proc = subprocess.Popen(
                tool_cmd, env=env, cwd=str(effective_cwd)
            )
            exit_code = child_proc.wait()
            append_log(log_path, f"Tool exit code: {exit_code}")

        return exit_code
    finally:
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
            print(f"{r['provider']:<12} {r['model']:<28} {r['role']:<14} {r['char_count']:>7}  {last}")
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
        print(f"# {r['char_count']} chars, last seen {(r['last_seen'] or '?')[:19]}", file=sys.stderr)
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


def _collect_cleanup_targets() -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen_proxy_pids: set[int] = set()

    for session_name in _tmux_list_sessions(socket_name="ai-mux"):
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
                    f"[tmux] session={session_name} "
                    f"(tool={tool}, cwd={cwd}{proxy_part})"
                ),
            }
        )
        if proxy_pid_raw.isdigit():
            seen_proxy_pids.add(int(proxy_pid_raw))

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
            lowered = cmd.lower()
            if "mitmdump" not in lowered and "mitmproxy" not in lowered:
                continue
            if pid in seen_proxy_pids:
                continue
            targets.append(
                {
                    "kind": "proxy",
                    "pid": pid,
                    "label": f"[proxy] pid={pid} cmd={cmd}",
                }
            )
            seen_proxy_pids.add(pid)

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
        print("  --list        Show detected tmux/proxy targets without killing")
        print("  --all         Select all detected targets")
        print("  --select      Comma-separated item numbers from the target list")
        print("  -y, --yes     Skip confirmation prompt")
        return 0

    if unknown:
        print(f"Unknown cleanup arguments: {' '.join(unknown)}", file=sys.stderr)
        return 1

    targets = _collect_cleanup_targets()
    if not targets:
        print("No ai-cli tmux/proxy cleanup targets found.")
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

    append_log(cleanup_log, f"Manual cleanup complete. killed={killed}")
    print(f"Cleanup complete. killed={killed}. log={cleanup_log}")
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
        # "ai-cli system prompt [model]" — cat a captured system prompt
        if len(sys.argv) > 2 and sys.argv[2] == "prompt":
            return _cmd_system_prompt(sys.argv[3] if len(sys.argv) > 3 else "")
        tool = sys.argv[2] if len(sys.argv) > 2 else ""
        config = ensure_config()
        if tool:
            registry = load_registry()
            if tool in registry:
                return edit_instructions(_resolve_tool_prompt_file(config, tool))
            print(f"Unknown tool: {tool}", file=sys.stderr)
            print(f"Available: {', '.join(registry.keys())}", file=sys.stderr)
            return 1
        return edit_instructions(config.get("instructions_file", ""))

    if subcommand == "prompt-edit":
        scope = sys.argv[2] if len(sys.argv) > 2 else ""
        tool_name = sys.argv[3] if len(sys.argv) > 3 else ""
        return _cmd_prompt_edit(scope, tool_name)

    if subcommand == "status":
        return cmd_status()

    if subcommand == "cleanup":
        return cmd_cleanup(sys.argv[2:])

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
        print("  ai-cli system [tool]      Edit system instructions")
        print("  ai-cli prompt-edit ...    Edit global/tool prompt files")
        print("  ai-cli system prompt [model]  Show captured system prompt for a model")
        print("  ai-cli status             Show installed tools and versions")
        print("  ai-cli cleanup [opts]     Kill stray ai-mux and mitmproxy processes")
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
    print(f"Run 'ai-cli --help' for usage.", file=sys.stderr)
    return 1


def main_cli() -> None:
    """Console script entry point (raises SystemExit)."""
    raise SystemExit(main())


if __name__ == "__main__":
    main_cli()

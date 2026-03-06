"""Helper functions extracted from ai_cli.main for maintainability."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ai_cli.log import append_log

CODEX_PROXY_WARN_HOLD = 5


def check_codex_proxy_compat(log_path: Path | None = None) -> None:
    """Detect Codex network-proxy settings that can break ai-cli MITM."""
    issues: list[str] = []

    codex_config = Path.home() / ".codex" / "config.toml"
    if codex_config.is_file():
        try:
            text = codex_config.read_text(encoding="utf-8")
            in_network = False
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("["):
                    in_network = line.strip("[] ").lower() == "network"
                    continue
                if not in_network:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'").lower()
                if key == "allow_upstream_proxy" and val == "false":
                    issues.append(
                        "allow_upstream_proxy = false in ~/.codex/config.toml\n"
                        "  -> Codex proxy will bypass ai-cli mitmproxy (no traffic capture or injection)\n"
                        "  Fix: set allow_upstream_proxy = true in ~/.codex/config.toml [network]"
                    )
                if key == "mitm" and val == "true":
                    issues.append(
                        "mitm = true in ~/.codex/config.toml\n"
                        "  -> Double-MITM: Codex terminates TLS with its own CA, breaking our injection\n"
                        "  Fix: set mitm = false in ~/.codex/config.toml [network]"
                    )
        except OSError:
            pass

    project_config = Path.cwd() / ".codex" / "config.toml"
    if project_config.is_file() and project_config != codex_config:
        try:
            text = project_config.read_text(encoding="utf-8")
            in_network = False
            for raw_line in text.splitlines():
                line = raw_line.strip()
                if line.startswith("["):
                    in_network = line.strip("[] ").lower() == "network"
                    continue
                if not in_network:
                    continue
                key, _, val = line.partition("=")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'").lower()
                if key == "allow_upstream_proxy" and val == "false":
                    issues.append(
                        f"allow_upstream_proxy = false in {project_config}\n"
                        "  -> Project-level override bypasses ai-cli proxy\n"
                        "  Fix: remove or set allow_upstream_proxy = true"
                    )
                if key == "mitm" and val == "true":
                    issues.append(
                        f"mitm = true in {project_config}\n"
                        "  -> Project-level double-MITM override\n"
                        "  Fix: remove or set mitm = false"
                    )
        except OSError:
            pass

    codex_ca = Path.home() / ".codex" / "proxy" / "ca.pem"
    if codex_ca.is_file():
        issues.append(
            f"Codex MITM CA exists at {codex_ca}\n"
            "  -> Codex has generated its own CA for TLS interception\n"
            "  If mitm=true is active, this creates double-MITM with ai-cli"
        )

    if not issues:
        return

    border = "=" * 72
    print(f"\n{border}", file=sys.stderr)
    print("  WARNING: CODEX PROXY COMPATIBILITY", file=sys.stderr)
    print(border, file=sys.stderr)
    for issue in issues:
        print(file=sys.stderr)
        print(f"  {issue}", file=sys.stderr)
    print(file=sys.stderr)
    print("  If ai-cli instruction injection or traffic capture stops working,", file=sys.stderr)
    print("  these are the likely causes. See the remediation steps above.", file=sys.stderr)
    print(f"{border}", file=sys.stderr)
    print(f"  (continuing in {CODEX_PROXY_WARN_HOLD}s...)", file=sys.stderr)

    if log_path:
        for issue in issues:
            append_log(log_path, f"CODEX PROXY WARNING: {issue.splitlines()[0]}")

    time.sleep(CODEX_PROXY_WARN_HOLD)


def session_id(tool_name: str) -> str:
    return f"ai-cli-{tool_name}-{os.getpid()}-{time.time_ns()}"


def write_session_files(session_id_value: str, port: int) -> None:
    pid_path = Path(f"/tmp/{session_id_value}.pid")
    port_path = Path(f"/tmp/{session_id_value}.port")
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        port_path.write_text(str(port), encoding="utf-8")
    except OSError:
        pass


def cleanup_session_files(session_id_value: str) -> None:
    for suffix in (".pid", ".port"):
        try:
            Path(f"/tmp/{session_id_value}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def spawn_detached_proxy_watcher(
    mitm_pid: int,
    session_id_value: str,
    tmux_sessions: list[str],
    log_path: Path,
) -> bool:
    python = sys.executable or shutil.which("python3") or "python3"
    cmd = [
        python,
        "-m",
        "ai_cli.detached_cleanup",
        "--mitm-pid",
        str(mitm_pid),
        "--session-id",
        session_id_value,
        "--wrapper-log-file",
        str(log_path),
        "--tmux-socket",
        "ai-mux",
    ]
    for session_name in tmux_sessions:
        cmd.extend(["--tmux-session", session_name])
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        append_log(log_path, f"Failed spawning detached proxy watcher: {exc}")
        return False
    append_log(log_path, f"Spawned detached proxy watcher for mitmdump pid {mitm_pid}")
    return True


def tmux_list_sessions(socket_name: str = "ai-mux") -> list[str]:
    try:
        probe = subprocess.run(
            ["tmux", "-L", socket_name, "list-sessions", "-F", "#{session_name}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return []
    if probe.returncode != 0:
        return []
    return [line.strip() for line in (probe.stdout or "").splitlines() if line.strip()]


def tmux_session_env(session_name: str, socket_name: str = "ai-mux") -> dict[str, str]:
    try:
        probe = subprocess.run(
            ["tmux", "-L", socket_name, "show-environment", "-t", session_name],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        return {}
    if probe.returncode != 0:
        return {}

    env: dict[str, str] = {}
    for raw in (probe.stdout or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("-") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value
    return env


def normalize_dir(path_value: str) -> str:
    if not path_value:
        return ""
    return os.path.realpath(os.path.expanduser(path_value))


def resolve_recv_context_file(cwd: Path) -> str:
    env_path = os.environ.get("AI_CLI_RECV_CONTEXT_FILE", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
    candidate = cwd / "received_instructions_context.txt"
    if candidate.is_file():
        return str(candidate.resolve())
    return ""


def find_reusable_tmux_session(
    tool_name: str,
    effective_cwd: Path,
    socket_name: str = "ai-mux",
) -> tuple[str, dict[str, str]] | None:
    target_dir = normalize_dir(str(effective_cwd))
    for session_name in tmux_list_sessions(socket_name=socket_name):
        env = tmux_session_env(session_name, socket_name=socket_name)
        if env.get("AI_CLI_TOOL", "") != tool_name:
            continue
        session_dir = normalize_dir(env.get("AI_CLI_WORKDIR", ""))
        if session_dir and session_dir == target_dir:
            return session_name, env
    return None


def terminate_pid(pid: int, timeout_seconds: float = 3.0) -> None:
    try:
        os.kill(pid, 0)
    except OSError:
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def kill_proxy_from_env(session_env: dict[str, str], log_path: Path) -> None:
    pid_raw = session_env.get("AI_CLI_PROXY_PID", "").strip()
    if pid_raw.isdigit():
        pid = int(pid_raw)
        terminate_pid(pid)
        append_log(log_path, f"Stopped existing proxy by PID: {pid}")
        return

    proxy_url = (
        session_env.get("HTTP_PROXY", "").strip()
        or session_env.get("HTTPS_PROXY", "").strip()
        or session_env.get("http_proxy", "").strip()
        or session_env.get("https_proxy", "").strip()
    )
    parsed = urlparse(proxy_url)
    if parsed.port is None:
        append_log(log_path, "Existing session proxy PID/port not found; skipping direct proxy stop")
        return

    try:
        probe = subprocess.run(
            ["lsof", "-n", f"-iTCP:{parsed.port}", "-sTCP:LISTEN", "-t"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError:
        append_log(log_path, "lsof unavailable; unable to stop existing proxy by port")
        return

    pids = [line.strip() for line in (probe.stdout or "").splitlines() if line.strip().isdigit()]
    if not pids:
        append_log(log_path, f"No listening proxy PID found on port {parsed.port}")
        return

    for raw in pids:
        terminate_pid(int(raw))
    append_log(log_path, f"Stopped existing proxy by port {parsed.port}: pids={','.join(pids)}")


def replace_existing_tmux_session(
    session_name: str,
    session_env: dict[str, str],
    log_path: Path,
    socket_name: str = "ai-mux",
) -> None:
    kill_proxy_from_env(session_env, log_path)
    try:
        subprocess.call(
            ["tmux", "-L", socket_name, "kill-session", "-t", session_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        append_log(log_path, f"Killed existing tmux session: {session_name}")
    except OSError as exc:
        append_log(log_path, f"Failed to kill existing tmux session {session_name}: {exc}")

    old_session_id = session_env.get("AI_CLI_SESSION", "").strip()
    if old_session_id:
        cleanup_session_files(old_session_id)


def parse_wrapper_overrides(args: list[str]) -> tuple[list[str], dict[str, Any]]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--ai-cli-system-instructions-file", dest="instructions_file")
    parser.add_argument("--ai-cli-system-instructions-text", dest="instructions_text")
    parser.add_argument("--ai-cli-canary-rule", dest="canary_rule")
    parser.add_argument(
        "--ai-cli-passthrough",
        dest="passthrough",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--ai-cli-debug-requests",
        dest="debug_requests",
        action="store_true",
        default=None,
    )
    parser.add_argument(
        "--ai-cli-rewrite-test-mode",
        dest="rewrite_test_mode",
        choices=("off", "outgoing", "incoming", "both"),
        default=None,
    )
    parser.add_argument(
        "--ai-cli-developer-instructions-mode",
        dest="developer_instructions_mode",
        choices=("overwrite", "append", "prepend"),
        default=None,
    )
    parser.add_argument("--ai-cli-rewrite-test-tag", dest="rewrite_test_tag")
    parser.add_argument(
        "--ai-cli-no-startup-context",
        dest="no_startup_context",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--app",
        dest="use_app_binary",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--ai-cli-remote-rsync",
        dest="remote_rsync",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--ai-cli-remote-init",
        dest="remote_init",
        default=None,
    )
    parser.add_argument(
        "--ai-cli-remote-session-name",
        dest="remote_session_name",
        default=None,
    )
    parser.add_argument(
        "--ai-cli-remote-no-package",
        dest="remote_no_package",
        action="store_true",
        default=False,
    )
    known, remaining = parser.parse_known_args(args)
    return remaining, {
        "instructions_file": known.instructions_file,
        "instructions_text": known.instructions_text,
        "canary_rule": known.canary_rule,
        "passthrough": known.passthrough,
        "debug_requests": known.debug_requests,
        "rewrite_test_mode": known.rewrite_test_mode,
        "developer_instructions_mode": known.developer_instructions_mode,
        "rewrite_test_tag": known.rewrite_test_tag,
        "no_startup_context": known.no_startup_context,
        "use_app_binary": known.use_app_binary,
        "remote_rsync": known.remote_rsync,
        "remote_init": known.remote_init,
        "remote_session_name": known.remote_session_name,
        "remote_no_package": known.remote_no_package,
    }


def extract_launch_cwd(
    args: list[str],
) -> tuple[Path | None, list[str], "RemoteSpec | None"]:
    """Extract the directory (or remote spec) from the head of *args*.

    Returns ``(local_path, remaining_args, remote_spec)``.  When the first arg
    matches ``user@host:/path``, *remote_spec* is populated and *local_path* is
    ``None`` (the caller is responsible for syncing down and setting up the
    local mirror).
    """
    from ai_cli.remote import RemoteSpec, parse_remote_spec

    if not args:
        return None, args, None
    first = args[0]
    if not first or first.startswith("-"):
        return None, args, None

    # Check for remote spec (user@host:/path) before local path
    remote = parse_remote_spec(first)
    if remote is not None:
        return None, args[1:], remote

    candidate = Path(first).expanduser()
    if not candidate.is_dir():
        return None, args, None
    resolved = candidate.resolve()
    return resolved, args[1:], None


def find_ai_mux() -> str | None:
    repo_root = Path(__file__).resolve().parent.parent
    packaged = Path(__file__).resolve().parent / "bin" / "ai-mux"
    candidates = [
        packaged,
        repo_root / "mux" / "target" / "release" / "ai-mux",
        Path("~/.local/bin/ai-mux").expanduser(),
    ]
    path_hit = shutil.which("ai-mux")
    if path_hit:
        candidates.insert(1, Path(path_hit))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ai_mux_status() -> tuple[str, str | None]:
    packaged = Path(__file__).resolve().parent / "bin" / "ai-mux"
    if packaged.is_file() and os.access(packaged, os.X_OK):
        return "packaged", str(packaged)

    path_hit = shutil.which("ai-mux")
    if path_hit:
        return "path", path_hit

    repo_build = Path(__file__).resolve().parent.parent / "mux" / "target" / "release" / "ai-mux"
    if repo_build.is_file() and os.access(repo_build, os.X_OK):
        return "repo-build", str(repo_build)

    return "python-fallback", None

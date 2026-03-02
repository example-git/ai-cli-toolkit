"""Detached proxy lifecycle watcher for tmux-backed sessions.

This process is spawned when ai-mux detaches while the wrapped tool is still
running. It keeps mitmdump alive and performs cleanup once the tool exits.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path


def _log(path_value: str, message: str) -> None:
    if not path_value:
        return
    try:
        p = Path(path_value).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            ts = datetime.now().isoformat(timespec="seconds")
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _tmux_session_alive(socket_name: str) -> bool:
    try:
        code = subprocess.call(
            ["tmux", "-L", socket_name, "has-session"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return code == 0


def _tmux_named_sessions_alive(socket_name: str, sessions: list[str]) -> bool:
    if not sessions:
        return _tmux_session_alive(socket_name)
    for session in sessions:
        try:
            code = subprocess.call(
                ["tmux", "-L", socket_name, "has-session", "-t", session],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return False
        if code == 0:
            return True
    return False


def _cleanup_session_files(session_id: str) -> None:
    for suffix in (".pid", ".port"):
        try:
            Path(f"/tmp/{session_id}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def _terminate_pid(pid: int) -> None:
    if not _pid_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Detached tmux proxy watcher.")
    parser.add_argument("--mitm-pid", type=int, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--wrapper-log-file", default="")
    parser.add_argument("--tmux-socket", default="ai-mux")
    parser.add_argument("--tmux-session", action="append", default=[])
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()

    _log(
        args.wrapper_log_file,
        f"Detached watcher start (mitmdump pid={args.mitm_pid}, "
        f"sessions={','.join(args.tmux_session) or 'any'})",
    )

    try:
        while True:
            if not _pid_alive(args.mitm_pid):
                _log(args.wrapper_log_file, "Detached watcher: mitmdump already exited")
                break

            if not _tmux_named_sessions_alive(args.tmux_socket, args.tmux_session):
                _log(
                    args.wrapper_log_file,
                    "Detached watcher: tmux session ownership ended",
                )
                break

            time.sleep(max(args.poll_seconds, 0.2))
    finally:
        _terminate_pid(args.mitm_pid)
        _cleanup_session_files(args.session_id)
        _log(args.wrapper_log_file, "Detached watcher stop (proxy/session cleanup complete)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

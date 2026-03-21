#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:48] or "project"


def _project_identity(project_cwd: Path, remote_spec: str = "") -> tuple[str, str]:
    if remote_spec.strip():
        identity = f"remote:{remote_spec.strip()}::{project_cwd}"
        label = f"{remote_spec.strip().split(':', 1)[0]} {project_cwd.name or 'project'}"
        return identity, label
    resolved = str(project_cwd.expanduser().resolve(strict=False))
    return resolved, project_cwd.name or "project"


def _project_prompt_path(project_cwd: Path, remote_spec: str = "") -> Path:
    identity, label = _project_identity(project_cwd=project_cwd, remote_spec=remote_spec)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    dirname = f"{_slugify(label)}-{digest}"
    return Path.home() / ".ai-cli" / "project-prompts" / dirname / "instructions.txt"


def _tmux_current_path() -> Path | None:
    pane = os.environ.get("TMUX_PANE", "").strip()
    if not pane:
        return None
    proc = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{pane_current_path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    current = proc.stdout.strip()
    if not current:
        return None
    return Path(current)


def _target_path(target: str) -> str:
    home = Path.home()
    tool_name = os.environ.get("AI_CLI_TOOL", "codex")
    remote_spec = os.environ.get("AI_CLI_REMOTE_SPEC", "")
    workdir = Path(os.environ.get("AI_CLI_WORKDIR") or str(_tmux_current_path() or Path.cwd()))
    defaults = {
        "global": str(home / ".ai-cli" / "system_instructions.txt"),
        "base": str(home / ".ai-cli" / "base_instructions.txt"),
        "tool": str(home / ".ai-cli" / "instructions" / f"{tool_name}.txt"),
        "project": str(_project_prompt_path(workdir, remote_spec=remote_spec)),
    }
    env_map = {
        "global": "AI_CLI_GLOBAL_PROMPT_FILE",
        "base": "AI_CLI_BASE_PROMPT_FILE",
        "tool": "AI_CLI_TOOL_PROMPT_FILE",
        "project": "AI_CLI_PROJECT_PROMPT_FILE",
    }
    return os.environ.get(env_map[target], defaults[target])


def _resolve_requested_file(file_arg: str | None, target: str | None) -> Path:
    if file_arg:
        return _resolve_file(file_arg)
    if target:
        return _resolve_file(_target_path(target))
    raise RuntimeError("missing prompt target")


def _resolve_file(path_arg: str) -> Path:
    return Path(path_arg).expanduser().resolve(strict=False)


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    if path.name == "instructions.txt" and path.parent.parent.name == "project-prompts":
        meta = path.parent / "meta.json"
        if not meta.exists():
            payload = {
                "instructions_file": str(path),
                "project_cwd": os.environ.get("AI_CLI_WORKDIR", str(path.parent)),
                "remote_spec": os.environ.get("AI_CLI_REMOTE_SPEC", ""),
            }
            meta.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _lock_dir() -> Path:
    return Path(
        os.environ.get(
            "AI_CLI_PROMPT_EDITOR_LOCK_DIR",
            str(Path.home() / ".ai-cli" / "locks" / "prompt-editors"),
        )
    ).expanduser()


def _lock_path(target: Path) -> Path:
    digest = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    return _lock_dir() / f"{digest}.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_lock(path: Path) -> dict[str, object] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_lock(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _replace_stale_lock(path: Path, payload: dict[str, object]) -> bool:
    current = _read_lock(path)
    current_pid_raw = current.get("pid") if isinstance(current, dict) else None
    if isinstance(current_pid_raw, (int, str)):
        current_pid = int(current_pid_raw)
    else:
        current_pid = 0
    if current and _pid_alive(current_pid):
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    try:
        _write_lock(path, payload)
    except FileExistsError:
        return False
    return True


def _acquire_lock(lock_path: Path, payload: dict[str, object]) -> bool:
    try:
        _write_lock(lock_path, payload)
        return True
    except FileExistsError:
        return _replace_stale_lock(lock_path, payload)


def _release_lock(lock_path: Path, token: str) -> None:
    current = _read_lock(lock_path)
    if isinstance(current, dict) and current.get("token") == token:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _tmux(socket_name: str | None, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = ["tmux"]
    if socket_name:
        cmd += ["-L", socket_name]
    cmd += list(args)
    return subprocess.run(cmd, check=False, text=True, capture_output=True)


def _resolve_editor() -> list[str]:
    configured = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if configured:
        return shlex.split(configured)
    for candidate in ("nano", "vi", "vim"):
        probe = subprocess.run(
            ["sh", "-lc", f"command -v {shlex.quote(candidate)} >/dev/null 2>&1"],
            check=False,
        )
        if probe.returncode == 0:
            return [candidate]
    raise RuntimeError("No editor found (set VISUAL or EDITOR)")


def _select_existing_window(socket_name: str | None, window_name: str) -> bool:
    if not socket_name:
        return False
    proc = _tmux(socket_name, "select-window", "-t", window_name)
    return proc.returncode == 0


def _display_message(socket_name: str | None, message: str) -> None:
    if socket_name:
        _tmux(socket_name, "display-message", message)


def _self_command(args: argparse.Namespace, lock_path: Path, token: str) -> str:
    python_bin = os.environ.get("AI_CLI_PYTHON") or sys.executable or "python3"
    script_path = Path(__file__).resolve()
    cmd = [
        python_bin,
        str(script_path),
        "edit",
        "--lock-file",
        str(lock_path),
        "--lock-token",
        token,
        "--window-name",
        args.window_name,
    ]
    if args.file:
        cmd += ["--file", args.file]
    if args.target:
        cmd += ["--target", args.target]
    if args.tmux_socket:
        cmd += ["--tmux-socket", args.tmux_socket]
    return " ".join(shlex.quote(part) for part in cmd)


def _open_editor_window(args: argparse.Namespace) -> int:
    target = _resolve_requested_file(args.file, args.target)
    _ensure_file(target)
    lock_path = _lock_path(target)
    token = f"{time.time_ns()}-{os.getpid()}"
    payload: dict[str, object] = {
        "file": str(target),
        "pid": os.getpid(),
        "token": token,
        "window_name": args.window_name,
    }
    if not _acquire_lock(lock_path, payload):
        if not _select_existing_window(args.tmux_socket, args.window_name):
            _display_message(args.tmux_socket, f"{target.name} already open")
        return 0

    try:
        cmd = [
            "new-window",
            "-n",
            args.window_name,
            "-c",
            str(target.parent),
            _self_command(args, lock_path, token),
        ]
        proc = _tmux(args.tmux_socket, *cmd)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or "tmux new-window failed")
    except Exception:
        _release_lock(lock_path, token)
        raise
    return 0


def _edit_file(args: argparse.Namespace) -> int:
    target = _resolve_requested_file(args.file, args.target)
    _ensure_file(target)
    lock_path = Path(args.lock_file).expanduser().resolve(strict=False)
    current = _read_lock(lock_path)
    if not isinstance(current, dict) or current.get("token") != args.lock_token:
        _display_message(args.tmux_socket, f"{target.name} is already managed elsewhere")
        return 0

    current["pid"] = os.getpid()
    current["file"] = str(target)
    current["window_name"] = args.window_name
    lock_path.write_text(json.dumps(current), encoding="utf-8")

    editor_cmd = _resolve_editor() + [str(target)]
    try:
        proc = subprocess.run(editor_cmd, cwd=str(target.parent), check=False)
        return proc.returncode
    finally:
        _release_lock(lock_path, args.lock_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open ai-cli prompt editors safely inside tmux.")
    subparsers = parser.add_subparsers(dest="action", required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--file")
    base.add_argument("--target", choices=["global", "base", "tool", "project"])
    base.add_argument("--window-name", required=True)
    base.add_argument("--tmux-socket")

    subparsers.add_parser("open", parents=[base])

    edit_parser = subparsers.add_parser("edit", parents=[base])
    edit_parser.add_argument("--lock-file", required=True)
    edit_parser.add_argument("--lock-token", required=True)

    args = parser.parse_args(argv)

    try:
        if args.action == "open":
            return _open_editor_window(args)
        return _edit_file(args)
    except RuntimeError as exc:
        _display_message(getattr(args, "tmux_socket", None), str(exc))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

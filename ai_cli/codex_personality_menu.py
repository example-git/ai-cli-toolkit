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

from prompt_toolkit import prompt
from prompt_toolkit.key_binding import KeyBindings

_SECTION_HEADINGS_RE = re.compile(
    r"^#+\s*(Personality|Values|Interaction Style|Escalation)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_EDITABLE_KEYS = ("personality", "interaction_style", "escalation")


def _resolve_file(path_arg: str | None) -> Path:
    if path_arg:
        return Path(path_arg).expanduser().resolve(strict=False)
    env_path = os.environ.get("AI_CLI_CODEX_PERSONALITY_PROMPT_FILE", "").strip()
    if env_path:
        return Path(env_path).expanduser().resolve(strict=False)
    return (Path.home() / ".ai-cli" / "instructions" / "codex-personality.txt").resolve(
        strict=False
    )


def _ensure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)


def _resolve_defaults_file(path: Path) -> Path:
    stem = path.stem or path.name or "codex-personality"
    return path.with_name(f"{stem}.defaults.json")


def _lock_dir() -> Path:
    return (Path.home() / ".ai-cli" / "locks" / "codex-personality").expanduser()


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
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


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
    current_pid = int(current_pid_raw) if isinstance(current_pid_raw, (int, str)) else 0
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
        "menu",
        "--lock-file",
        str(lock_path),
        "--lock-token",
        token,
        "--window-name",
        args.window_name,
    ]
    if args.file:
        cmd += ["--file", args.file]
    if args.tmux_socket:
        cmd += ["--tmux-socket", args.tmux_socket]
    return " ".join(shlex.quote(part) for part in cmd)


def _normalize_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _empty_sections() -> dict[str, str]:
    return {key: "" for key in _EDITABLE_KEYS}


def _parse_sections(raw: str) -> dict[str, str]:
    raw = raw.strip()
    if not raw:
        return _empty_sections()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {
            "personality": _normalize_text(payload.get("personality")),
            "interaction_style": _normalize_text(
                payload.get("interaction_style") or payload.get("interaction style")
            ),
            "escalation": _normalize_text(payload.get("escalation")),
        }

    found_headings = [m.group(1).lower() for m in _SECTION_HEADINGS_RE.finditer(raw)]
    if not found_headings:
        return {
            "personality": raw,
            "interaction_style": "",
            "escalation": "",
        }

    splits = _SECTION_HEADINGS_RE.split(raw)
    sections: dict[str, str] = {}
    for idx in range(1, len(splits), 2):
        key = splits[idx].lower().replace(" ", "_")
        body = splits[idx + 1].strip() if idx + 1 < len(splits) else ""
        sections[key] = body
    return {
        "personality": sections.get("personality", ""),
        "interaction_style": sections.get("interaction_style", ""),
        "escalation": sections.get("escalation", ""),
    }


def _has_values(sections: dict[str, str]) -> bool:
    return any(sections.get(key, "").strip() for key in _EDITABLE_KEYS)


def _load_sections_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return _empty_sections()
    return _parse_sections(path.read_text(encoding="utf-8"))


def _load_existing_sections(path: Path) -> dict[str, str]:
    override_sections = _load_sections_file(path)
    if _has_values(override_sections):
        return override_sections
    defaults_path = _resolve_defaults_file(path)
    default_sections = _load_sections_file(defaults_path)
    if _has_values(default_sections):
        return default_sections
    return override_sections


def _write_payload(path: Path, sections: dict[str, str]) -> None:
    payload = {
        "personality": sections["personality"].rstrip(),
        "interaction_style": sections["interaction_style"].rstrip(),
        "escalation": sections["escalation"].rstrip(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _field_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def _accept(event) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _newline(event) -> None:
        event.current_buffer.insert_text("\n")

    return bindings


def _prompt_field(label: str, default: str) -> str:
    return prompt(
        f"{label}\n",
        default=default,
        multiline=True,
        key_bindings=_field_key_bindings(),
    )


def _open_menu_window(args: argparse.Namespace) -> int:
    target = _resolve_file(args.file)
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


def _run_menu(args: argparse.Namespace) -> int:
    target = _resolve_file(args.file)
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

    try:
        existing = _load_existing_sections(target)
        print("Codex personality editor", flush=True)
        print("Enter saves each field and advances to the next one.", flush=True)
        print("Esc+Enter inserts a newline inside the current field.", flush=True)
        print("", flush=True)
        updated = {
            "personality": _prompt_field("Personality", existing["personality"]),
            "interaction_style": _prompt_field(
                "Interaction Style",
                existing["interaction_style"],
            ),
            "escalation": _prompt_field("Escalation", existing["escalation"]),
        }
        _write_payload(target, updated)
        print("", flush=True)
        print(f"Saved {target}", flush=True)
        return 0
    finally:
        _release_lock(lock_path, args.lock_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Open the managed Codex personality menu inside tmux."
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    base = argparse.ArgumentParser(add_help=False)
    base.add_argument("--file")
    base.add_argument("--window-name", required=True)
    base.add_argument("--tmux-socket")

    subparsers.add_parser("open", parents=[base])

    menu_parser = subparsers.add_parser("menu", parents=[base])
    menu_parser.add_argument("--lock-file", required=True)
    menu_parser.add_argument("--lock-token", required=True)

    args = parser.parse_args(argv)
    try:
        if args.action == "open":
            return _open_menu_window(args)
        return _run_menu(args)
    except RuntimeError as exc:
        _display_message(getattr(args, "tmux_socket", None), str(exc))
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

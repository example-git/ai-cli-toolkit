"""Curses-based interactive menu for tool management."""

from __future__ import annotations

import curses
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ai_cli import __version__
from ai_cli.config import ensure_config, get_tool_config
from ai_cli.instructions import resolve_instructions_file
from ai_cli.tools import load_registry


MenuAction = tuple[str, str]
TmuxSessionRow = tuple[str, str, str, str]


def _read_key(stdscr: curses.window) -> int:
    """Read one key, normalizing common ESC arrow sequences."""
    key = stdscr.getch()
    if key != 27:
        return key

    # Some terminals deliver arrows as raw ESC sequences even with keypad.
    stdscr.nodelay(True)
    try:
        nxt = stdscr.getch()
        if nxt == -1:
            return 27
        if nxt == 91:  # '['
            final = stdscr.getch()
            if final == 65:
                return curses.KEY_UP
            if final == 66:
                return curses.KEY_DOWN
            if final == 67:
                return curses.KEY_RIGHT
            if final == 68:
                return curses.KEY_LEFT
        return 27
    finally:
        stdscr.nodelay(False)


def _status_lines() -> list[str]:
    config = ensure_config()
    registry = load_registry()
    lines = [f"ai-cli v{__version__}"]
    for name, spec in registry.items():
        tool_cfg = get_tool_config(config, name)
        installed = spec.detect_installed(tool_cfg.get("binary", ""))
        enabled = "enabled" if tool_cfg.get("enabled", True) else "disabled"
        state = "installed" if installed else "missing"
        lines.append(f"{name:<8} {state:<9} {enabled}")
    return lines


def _editor_command() -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        for fallback in ("nano", "vi", "vim"):
            if shutil.which(fallback):
                editor = fallback
                break
    if not editor:
        return []
    return editor.split()


def _edit_instructions_blocking(tool: str = "") -> int:
    config = ensure_config()
    path_value = ""
    if tool:
        path_value = get_tool_config(config, tool).get("instructions_file", "")
    path = resolve_instructions_file(path_value)
    editor = _editor_command()
    if not editor:
        print(f"No editor found. Edit this file manually: {path}", file=sys.stderr)
        return 1
    return subprocess.call([*editor, path])


def _list_recent_sessions() -> int:
    from ai_cli import session as session_mod

    return session_mod.main(["--list"])


def _fetch_tmux_sessions() -> list[TmuxSessionRow]:
    """Fetch active ai-cli tmux sessions."""
    try:
        result = subprocess.run(
            ["tmux", "-L", "ai-mux", "list-sessions", "-F",
             "#{session_name}\t#{session_windows}\t#{?session_attached,attached,detached}\t#{session_created_string}"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    output = result.stdout.strip()
    if not output:
        return []
    rows: list[TmuxSessionRow] = []
    for raw in output.splitlines():
        parts = raw.split("\t")
        if len(parts) != 4:
            continue
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def _draw_tmux_picker(stdscr: curses.window, rows: list[TmuxSessionRow], selected: int) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    stdscr.addnstr(0, 2, "Active ai-cli tmux sessions", max(1, width - 4), curses.A_BOLD)
    stdscr.addnstr(1, 2, "Up/Down move, Enter attach, x kill selected, q cancel", max(1, width - 4))
    stdscr.addnstr(3, 2, f"{'Session':<28} {'Windows':<7} {'State':<9} Created", max(1, width - 4), curses.A_DIM)
    stdscr.addnstr(4, 2, "-" * max(1, min(80, width - 4)), max(1, width - 4), curses.A_DIM)

    start = 5
    for idx, (name, windows, state, created) in enumerate(rows):
        row = start + idx
        if row >= height - 1:
            break
        label = f"{name:<28} {windows:<7} {state:<9} {created}"
        attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
        stdscr.addnstr(row, 2, label, max(1, width - 4), attr)
    stdscr.refresh()


def _kill_tmux_session(name: str) -> int:
    try:
        return subprocess.call(
            ["tmux", "-L", "ai-mux", "kill-session", "-t", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 1


def _pick_tmux_session_curses(rows: list[TmuxSessionRow]) -> str | None:
    def _inner(stdscr: curses.window) -> str | None:
        curses.curs_set(0)
        stdscr.keypad(True)
        selected = 0
        while True:
            if not rows:
                return None
            _draw_tmux_picker(stdscr, rows, selected)
            key = _read_key(stdscr)
            if key in (ord("q"), 27):
                return None
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(rows)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(rows)
                continue
            if key in (ord("x"), ord("X"), curses.KEY_DC):
                doomed = rows[selected][0]
                _kill_tmux_session(doomed)
                rows.pop(selected)
                if not rows:
                    return None
                selected = min(selected, len(rows) - 1)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                return rows[selected][0]
    return curses.wrapper(_inner)


def _pick_tmux_session_text(rows: list[TmuxSessionRow]) -> str | None:
    while rows:
        print("Active ai-cli sessions:")
        print()
        for idx, (name, windows, state, created) in enumerate(rows, 1):
            print(f"{idx}. {name:<24} {windows} windows  {state:<8} created {created}")
        print()
        try:
            choice = input("Enter number to attach, k<number> to kill, blank to cancel: ").strip()
        except EOFError:
            return None
        if not choice:
            return None
        if choice.startswith(("k", "K")):
            suffix = choice[1:].strip()
            if suffix.isdigit():
                index = int(suffix) - 1
                if 0 <= index < len(rows):
                    doomed = rows[index][0]
                    _kill_tmux_session(doomed)
                    rows.pop(index)
            continue
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(rows):
                return rows[index][0]
    return None


def _list_tmux_sessions() -> int:
    """Interactive tmux session picker; Enter attaches to selected session."""
    rows = _fetch_tmux_sessions()
    if not rows:
        print("No active ai-cli sessions.", file=sys.stderr)
        return 0

    selected: str | None
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            selected = _pick_tmux_session_curses(rows)
        except curses.error:
            selected = _pick_tmux_session_text(rows)
    else:
        selected = _pick_tmux_session_text(rows)

    if not selected:
        return 0

    try:
        return subprocess.call(["tmux", "-L", "ai-mux", "attach-session", "-t", selected])
    except OSError:
        print("tmux not available.", file=sys.stderr)
        return 1


def _run_update(tool: str) -> int:
    from ai_cli import update as update_mod

    return update_mod.update_tool(tool)


def _run_tool(tool: str, tool_args: list[str] | None = None) -> int:
    from ai_cli.main import run_tool

    return run_tool(tool, tool_args or [])


def _actions() -> list[MenuAction]:
    items: list[MenuAction] = []
    for tool in load_registry().keys():
        items.append((f"Launch {tool}", f"launch:{tool}"))
    items.extend(
        [
            ("Status", "status"),
            ("Edit global instructions", "edit:global"),
            ("Edit claude instructions", "edit:claude"),
            ("Edit codex instructions", "edit:codex"),
            ("Edit copilot instructions", "edit:copilot"),
            ("Edit gemini instructions", "edit:gemini"),
            ("Update claude", "update:claude"),
            ("Update codex", "update:codex"),
            ("Update copilot", "update:copilot"),
            ("Update gemini", "update:gemini"),
            ("Active sessions", "sessions"),
            ("Session history", "history"),
            ("Browse traffic (all)", "traffic"),
            ("Browse traffic (API only)", "traffic:api"),
            ("Browse traffic (Anthropic)", "traffic:anthropic"),
            ("Browse traffic (OpenAI)", "traffic:openai"),
            ("Browse traffic (Copilot)", "traffic:copilot"),
            ("Browse traffic (Google)", "traffic:google"),
            ("Browse system prompts", "prompts"),
            ("Quit", "quit"),
        ]
    )
    return items


def _draw_menu(
    stdscr: curses.window,
    actions: list[MenuAction],
    selected: int,
    top_index: int,
) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()

    title = f"ai-cli Menu v{__version__}"
    stdscr.addnstr(0, 2, title, max(1, width - 4), curses.A_BOLD)
    stdscr.addnstr(1, 2, "Up/Down or j/k to move, Enter to select, q to quit", max(1, width - 4))

    lines = _status_lines()
    for idx, line in enumerate(lines):
        row = 3 + idx
        if row >= height - 1:
            break
        stdscr.addnstr(row, 2, line, max(1, width - 4), curses.A_DIM)

    start_row = 5 + len(lines)
    visible = max(1, height - start_row - 1)
    visible_actions = actions[top_index: top_index + visible]
    for rel_idx, (label, _) in enumerate(visible_actions):
        idx = top_index + rel_idx
        row = start_row + rel_idx
        if row >= height - 1:
            break
        attr = curses.A_REVERSE if idx == selected else curses.A_NORMAL
        stdscr.addnstr(row, 4, label, max(1, width - 8), attr)

    stdscr.refresh()


def _select_action_curses(actions: list[MenuAction]) -> str:
    def _inner(stdscr: curses.window) -> str:
        curses.curs_set(0)
        stdscr.keypad(True)
        selected = 0
        top_index = 0
        while True:
            height, _ = stdscr.getmaxyx()
            visible = max(1, height - (5 + len(_status_lines())) - 1)
            if selected < top_index:
                top_index = selected
            elif selected >= top_index + visible:
                top_index = selected - visible + 1

            _draw_menu(stdscr, actions, selected, top_index)
            key = _read_key(stdscr)
            if key in (ord("q"), 27):
                return "quit"
            if key in (curses.KEY_UP, ord("k")):
                selected = (selected - 1) % len(actions)
                continue
            if key in (curses.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(actions)
                continue
            if key in (10, 13, curses.KEY_ENTER):
                return actions[selected][1]

    return curses.wrapper(_inner)


def _select_action_text(actions: list[MenuAction]) -> str:
    while True:
        print("ai-cli menu")
        print()
        for line in _status_lines():
            print(f"  {line}")
        print()
        for idx, (label, _) in enumerate(actions, 1):
            print(f"{idx}. {label}")
        print()
        try:
            choice = input("Select an action (q to quit): ").strip().lower()
        except EOFError:
            return "quit"
        if not choice:
            continue
        if choice == "q":
            return "quit"
        if not choice.isdigit():
            print("Invalid selection.")
            print()
            continue
        index = int(choice) - 1
        if index < 0 or index >= len(actions):
            print("Selection out of range.")
            print()
            continue
        return actions[index][1]


def _browse_system_prompts() -> int:
    """List captured system prompts and let the user pick one to view."""
    from ai_cli.addons.system_prompt_addon import _DEFAULT_DB_DIR, _DEFAULT_DB_NAME
    import sqlite3

    db_path = _DEFAULT_DB_DIR / _DEFAULT_DB_NAME
    if not db_path.is_file():
        print("No system prompts captured yet.", file=sys.stderr)
        print(f"(Expected database at {db_path})", file=sys.stderr)
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, provider, model, role, char_count, seen_count, last_seen "
        "FROM system_prompts ORDER BY last_seen DESC"
    ).fetchall()
    if not rows:
        print("No system prompts captured yet.", file=sys.stderr)
        conn.close()
        return 0

    print(f"{'#':<4} {'Provider':<12} {'Model':<28} {'Role':<14} {'Chars':>7} {'Seen':>5}  Last Seen")
    print("-" * 110)
    for idx, r in enumerate(rows, 1):
        last = (r["last_seen"] or "?")[:19]
        role = r["role"] or "system"
        print(f"{idx:<4} {r['provider']:<12} {r['model']:<28} {role:<14} {r['char_count']:>7} {r['seen_count']:>5}  {last}")

    print()
    try:
        choice = input("Enter number to view full prompt (blank to cancel): ").strip()
    except EOFError:
        conn.close()
        return 0

    if not choice.isdigit():
        conn.close()
        return 0

    index = int(choice) - 1
    if index < 0 or index >= len(rows):
        print("Invalid selection.", file=sys.stderr)
        conn.close()
        return 1

    row_id = rows[index]["id"]
    full = conn.execute(
        "SELECT provider, model, role, content, char_count, first_seen, last_seen, seen_count "
        "FROM system_prompts WHERE id = ?",
        (row_id,),
    ).fetchone()
    conn.close()

    if not full:
        print("Prompt not found.", file=sys.stderr)
        return 1

    print()
    print(f"Provider: {full['provider']}")
    print(f"Model:    {full['model']}")
    print(f"Role:     {full['role'] or 'system'}")
    print(f"Chars:    {full['char_count']}")
    print(f"First:    {full['first_seen']}")
    print(f"Last:     {full['last_seen']}")
    print(f"Seen:     {full['seen_count']} time(s)")
    print("─" * 80)
    print(full["content"])
    return 0


def _browse_traffic(provider: str = "", api_only: bool = False) -> int:
    """Launch the traffic viewer in an isolated subprocess.

    Keep traffic in a clean terminal context while forcing this repo's module
    resolution to avoid stale globally-installed ai-cli binaries.
    """
    python = sys.executable or "python3"
    repo_root = Path(__file__).resolve().parent.parent

    env = os.environ.copy()
    current_pp = env.get("PYTHONPATH", "")
    root_str = str(repo_root)
    env["PYTHONPATH"] = f"{root_str}{os.pathsep}{current_pp}" if current_pp else root_str

    cmd = [python, "-m", "ai_cli", "traffic"]
    if provider:
        cmd.extend(["--provider", provider])
    if api_only:
        cmd.append("--api")
    return subprocess.call(cmd, env=env)


def _run_action(action: str) -> int:
    if action == "quit":
        return 0
    if action == "status":
        print("\n".join(_status_lines()))
        return 0
    if action == "sessions":
        return _list_tmux_sessions()
    if action == "history":
        return _list_recent_sessions()
    if action == "prompts":
        return _browse_system_prompts()
    if action == "traffic":
        return _browse_traffic()
    if action.startswith("traffic:"):
        suffix = action.split(":", 1)[1]
        if suffix == "api":
            return _browse_traffic(api_only=True)
        return _browse_traffic(provider=suffix)
    if action.startswith("launch:"):
        return _run_tool(action.split(":", 1)[1])
    if action.startswith("edit:"):
        suffix = action.split(":", 1)[1]
        tool = "" if suffix == "global" else suffix
        return _edit_instructions_blocking(tool)
    if action.startswith("update:"):
        return _run_update(action.split(":", 1)[1])
    return 1


def interactive_menu() -> int:
    """Open the interactive menu.

    Returns command exit code. Launch actions exit immediately into the selected
    tool flow; management actions return to the menu until the user quits.
    """
    actions = _actions()

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("\n".join(_status_lines()))
        return 0

    while True:
        try:
            action = _select_action_curses(actions)
        except curses.error:
            action = _select_action_text(actions)

        if action == "quit":
            return 0

        rc = _run_action(action)

        if action.startswith("launch:"):
            return rc
        # Always return directly to the menu after non-launch actions.
        # Avoiding an extra prompt keeps menu navigation fluid.
        _ = rc


if __name__ == "__main__":
    raise SystemExit(interactive_menu())

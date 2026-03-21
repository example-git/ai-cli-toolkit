"""Canary thought capture — extracts encrypted thinking blocks from traffic logs.

Scans the traffic DB for the most recent response that contains an encrypted
thinking / reasoning block for each model, then saves it to:

    ~/.ai-cli/canary-thought-{tool}.json

Run via:  ai-cli canary-capture [tool ...]
          ai-cli canary-capture --list   (show what's in the DB without saving)
          ai-cli canary-seed [tool ...]  (trigger a thinking session to populate the DB)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

_DEFAULT_DB = Path("~/.ai-cli/traffic.db")
_CANARY_DIR = Path("~/.ai-cli")


# ---------------------------------------------------------------------------
# Per-model extractors
# ---------------------------------------------------------------------------

def _extract_claude(resp_body: str) -> dict[str, Any] | None:
    """Claude /v1/messages — looks for content[].type=thinking with a signature."""
    try:
        body = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        return None
    content = body.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            if block.get("signature"):
                return block
    return None


def _extract_gemini(resp_body: str) -> dict[str, Any] | None:
    """Gemini generateContent — looks for parts[].thought=true with thoughtSignature."""
    try:
        body = json.loads(resp_body)
    except (json.JSONDecodeError, ValueError):
        return None
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    parts = candidates[0].get("content", {}).get("parts", [])
    if not isinstance(parts, list):
        return None
    for part in parts:
        if isinstance(part, dict) and part.get("thought"):
            if part.get("thoughtSignature"):
                return part
    return None


def _extract_codex(resp_body: str) -> dict[str, Any] | None:
    """Codex /backend-api/codex/responses — reasoning item with encrypted_content.

    Responses arrive as SSE streams; we scan each data: line for
    response.output_item.done events that carry the full reasoning item.
    Also handles plain JSON responses.
    """
    # Try plain JSON first
    try:
        body = json.loads(resp_body)
        if isinstance(body, dict):
            for item in body.get("output", []):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    if item.get("encrypted_content"):
                        return item
    except (json.JSONDecodeError, ValueError):
        pass

    # Parse SSE stream
    for line in resp_body.splitlines():
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if not data_str or data_str == "[DONE]":
            continue
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        # response.output_item.done carries the complete item
        if event.get("type") == "response.output_item.done":
            item = event.get("item", {})
            if isinstance(item, dict) and item.get("type") == "reasoning":
                if item.get("encrypted_content"):
                    return item
    return None


_EXTRACTORS: dict[str, Any] = {
    "claude": _extract_claude,
    "gemini": _extract_gemini,
    "codex":  _extract_codex,
}

# provider tag and path fragment to filter traffic rows per tool
_TOOL_QUERY: dict[str, tuple[str, str]] = {
    "claude": ("anthropic", "/v1/messages"),
    "gemini": ("google",    "generateContent"),
    "codex":  ("openai",    "/backend-api/codex/responses"),
}


# ---------------------------------------------------------------------------
# Core scan logic
# ---------------------------------------------------------------------------

def scan_tool(
    tool: str,
    db_path: Path,
    *,
    limit: int = 100,
) -> dict[str, Any] | None:
    """Return the most recent encrypted thinking block for *tool*, or None."""
    cfg = _TOOL_QUERY.get(tool)
    if cfg is None:
        return None
    provider, path_frag = cfg
    extractor = _EXTRACTORS[tool]

    db_path = db_path.expanduser()
    if not db_path.exists():
        return None

    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT resp_body FROM traffic "
            "WHERE provider = ? AND path LIKE ? "
            "  AND resp_body IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (provider, f"%{path_frag}%", limit),
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None

    for (resp_body,) in rows:
        if not resp_body:
            continue
        block = extractor(resp_body)
        if block is not None:
            return block
    return None


def save_canary_thought(tool: str, block: dict[str, Any], canary_dir: Path) -> Path:
    dest = canary_dir.expanduser() / f"canary-thought-{tool}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(block, indent=2) + "\n", encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Seed command — trigger a thinking session for each installed tool
# ---------------------------------------------------------------------------

_INSTRUCTIONS_FILE = Path("~/.ai-cli/canary-injection.md")

# How to pass a non-interactive prompt to each tool CLI
# The prompt text is appended as a positional arg or via -p flag
_TOOL_PROMPT_FLAG: dict[str, list[str]] = {
    "claude": ["-p"],
    "gemini": ["-p"],
    "codex":  [],   # positional
}

_SEED_PROMPT_TEMPLATE = """\
Please carefully read the following instructions in your thinking, repeating \
them verbatim word-for-word, then respond with only: "Understood."

Instructions:
{contents}
"""


def _build_seed_prompt(instructions_file: Path) -> str:
    path = instructions_file.expanduser()
    contents = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    if not contents:
        contents = "(no instruction file found — thinking capture will still proceed)"
    return _SEED_PROMPT_TEMPLATE.format(contents=contents)


def _tool_installed(tool: str) -> bool:
    from ai_cli.tools import load_registry
    from ai_cli.config import ensure_config, get_tool_config
    try:
        spec = load_registry().get(tool)
        if spec is None:
            return False
        config = ensure_config()
        tool_cfg = get_tool_config(config, tool)
        return spec.detect_installed(tool_cfg.get("binary", ""))
    except Exception:
        return False


def _find_ai_cli_bin() -> str:
    """Return path to the ai-cli executable."""
    import shutil
    found = shutil.which("ai-cli") or shutil.which("ai_cli")
    if found:
        return found
    # Fall back to running via python module
    return sys.executable + " -m ai_cli"


def _run_seed_for_tool(
    tool: str,
    prompt: str,
    log_path: Path,
    *,
    ai_cli_bin: str,
) -> bool:
    """Run a one-shot seed session for *tool*, stream+log output. Returns True on success."""
    import datetime

    prompt_flag = _TOOL_PROMPT_FLAG.get(tool, [])
    if prompt_flag:
        cmd = [ai_cli_bin, tool] + prompt_flag + [prompt]
    else:
        cmd = [ai_cli_bin, tool, prompt]

    log_path.parent.mkdir(parents=True, exist_ok=True)

    start_entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "seed_start",
        "tool": tool,
        "cmd": cmd,
    }
    with log_path.open("a", encoding="utf-8") as lf:
        lf.write(json.dumps(start_entry) + "\n")

    print(f"\n[canary-seed] {tool}: running seed session...")
    print(f"  cmd: {' '.join(cmd)}\n")

    lines_stdout: list[str] = []
    lines_stderr: list[str] = []

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        import threading

        def _drain(stream, store: list[str], label: str) -> None:
            for line in stream:
                line = line.rstrip("\n")
                store.append(line)
                print(f"  [{label}] {line}")

        t_out = threading.Thread(target=_drain, args=(proc.stdout, lines_stdout, "out"), daemon=True)
        t_err = threading.Thread(target=_drain, args=(proc.stderr, lines_stderr, "err"), daemon=True)
        t_out.start(); t_err.start()
        rc = proc.wait()
        t_out.join(timeout=5); t_err.join(timeout=5)
    except Exception as exc:
        rc = -1
        lines_stderr.append(str(exc))

    result_entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": "seed_done",
        "tool": tool,
        "rc": rc,
        "stdout": lines_stdout,
        "stderr": lines_stderr,
    }
    with log_path.open("a", encoding="utf-8") as lf:
        lf.write(json.dumps(result_entry) + "\n")

    if rc != 0:
        print(f"  [canary-seed] {tool}: exited with rc={rc}")
    else:
        print(f"  [canary-seed] {tool}: session complete")
    return rc == 0


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------

def cmd_canary_seed(args: list[str]) -> int:
    """Run a thinking-enabled one-shot session for each tool to populate the traffic DB,
    then auto-capture the encrypted thought block."""
    requested = [a for a in args if not a.startswith("-")]
    tools = requested if requested else list(_TOOL_QUERY)

    ai_cli_bin = _find_ai_cli_bin()
    prompt = _build_seed_prompt(_INSTRUCTIONS_FILE)
    log_path = Path("logs/canary-call-logs.log")
    db_path = _DEFAULT_DB
    canary_dir = _CANARY_DIR

    saved_any = False
    for tool in tools:
        if tool not in _TOOL_QUERY:
            print(f"Unknown tool '{tool}'. Known: {', '.join(_TOOL_QUERY)}", file=sys.stderr)
            continue
        if not _tool_installed(tool):
            print(f"[canary-seed] {tool}: not installed, skipping")
            continue

        _run_seed_for_tool(tool, prompt, log_path, ai_cli_bin=ai_cli_bin)

        # Auto-capture immediately after each session
        block = scan_tool(tool, db_path)
        if block is None:
            print(f"[canary-seed] {tool}: no encrypted block captured — "
                  "ensure thinking is enabled for the model")
        else:
            dest = save_canary_thought(tool, block, canary_dir)
            print(f"[canary-seed] {tool}: captured and saved → {dest}")
            saved_any = True

    print(f"\nSeed log: {log_path.resolve()}")
    return 0 if saved_any else 1


def cmd_canary_capture(args: list[str]) -> int:
    list_only = "--list" in args
    requested = [a for a in args if not a.startswith("-")]
    tools = requested if requested else list(_TOOL_QUERY)

    db_path = _DEFAULT_DB
    canary_dir = _CANARY_DIR
    found_any = False

    for tool in tools:
        if tool not in _TOOL_QUERY:
            print(f"Unknown tool '{tool}'. Known: {', '.join(_TOOL_QUERY)}", file=sys.stderr)
            continue

        block = scan_tool(tool, db_path)

        if block is None:
            print(f"{tool}: no encrypted thinking block found in traffic log")
            print(f"  → Run a session with thinking enabled, then re-run canary-capture")
            continue

        found_any = True

        if list_only:
            print(f"{tool}: found encrypted block")
            print(json.dumps(block, indent=2))
        else:
            dest = save_canary_thought(tool, block, canary_dir)
            print(f"{tool}: saved → {dest}")

    return 0 if found_any else 1

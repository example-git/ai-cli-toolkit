"""Tool install/update commands."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ai_cli.config import ensure_config, get_tool_config, save_config
from ai_cli.tools import load_registry

homedir = os.environ.get("HOME", "")

userpath = os.environ.get("PATH", "")
nvm_path = Path(f"{homedir}/.nvm/nvm.sh").expanduser()
nvm_present = nvm_path.is_file()
ALIAS_DIR = str(Path(f"{homedir}/.ai-cli/bin").expanduser())


def _path_without_alias_dir(path_value: str | None = None) -> str:
    """Return PATH with the ai-cli alias directory removed."""
    raw = path_value if path_value is not None else os.environ.get("PATH", "")
    dirs = [d for d in raw.split(os.pathsep) if d != ALIAS_DIR]
    return os.pathsep.join(dirs)


moddedpath = _path_without_alias_dir(userpath)


def _run_shell(command: str) -> tuple[int, str]:
    """Run a shell command and return (exit_code, combined_output).

    Strips the ai-cli alias directory from PATH *inside* the shell so
    that ``bash -lc`` (which re-sources the profile) doesn't resolve
    tool names to the ai-cli wrapper instead of the real binary.
    """
    # Inject a PATH cleanup at the head of the command so it takes
    # effect even after bash's login-profile re-adds the alias dir.
    sanitised = (
        f'export PATH=f"{moddedpath}:{ALIAS_DIR}"; '
        f"{f"source {nvm_path} if nvm_present else ''"}; "
        f"{command}"
    )

    result = subprocess.run(
        ["bash", "-lc", sanitised],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output.strip()


def available_targets() -> list[str]:
    """Return updatable tool names sorted by registry order."""
    return list(load_registry().keys())


def _regenerate_completions() -> None:
    """Regenerate shell completions to pick up newly installed tool flags."""
    try:
        from ai_cli.completion_gen import generate

        print("\nRegenerating shell completions...")
        generate(shell="all")
    except Exception as exc:
        print(f"Warning: could not regenerate completions: {exc}", file=sys.stderr)


def _persist_managed_binary(config: dict, tool_name: str, binary: str) -> None:
    """Persist a managed binary path into tool config."""
    tools = config.setdefault("tools", {})
    tool_cfg = tools.setdefault(tool_name, {})
    tool_cfg["binary"] = binary
    save_config(config)


def update_tool(
    tool_name: str, dry_run: bool = False, method: str | None = None, regen_completions: bool = True
) -> int:
    """Install or update one tool using its ToolSpec install command."""
    registry = load_registry()
    spec = registry.get(tool_name)
    if spec is None:
        print(f"Unknown tool: {tool_name}", file=sys.stderr)
        return 1

    if method and method not in spec.install_methods:
        available = ", ".join(spec.install_methods.keys()) or "(none)"
        print(
            f"Unknown install method '{method}' for {tool_name}. Available: {available}",
            file=sys.stderr,
        )
        return 1

    # Resolve method (explicit, auto-detected, or default)
    effective_method = method
    if not effective_method and spec.install_methods:
        effective_method = spec.detect_best_method()

    command = (spec.get_install_command(method) or "").strip()
    if not command:
        if not method:
            print(f"No install/update command configured for {tool_name}.", file=sys.stderr)
        return 1

    config = ensure_config()
    tool_cfg = get_tool_config(config, tool_name)

    installed_before = spec.detect_installed(tool_cfg.get("binary", ""))
    version_before = spec.get_version(tool_cfg.get("binary", "")) if installed_before else None

    status = "update" if installed_before else "install"
    method_label = f" via {effective_method}" if effective_method else ""
    print(f"{tool_name}: running {status}{method_label}")
    print(f"  $ {command}")

    if dry_run:
        return 0

    code, output = _run_shell(command)
    if output:
        print(output)
    if code != 0:
        print(f"{tool_name}: command failed with exit code {code}", file=sys.stderr)
        return code

    managed_binary = spec.managed_binary
    if managed_binary:
        managed_path = Path(spec.resolve_binary(managed_binary))
        if managed_path.is_file():
            _persist_managed_binary(config, tool_name, managed_binary)
            tool_cfg["binary"] = managed_binary

    installed_after = spec.detect_installed(tool_cfg.get("binary", ""))
    version_after = spec.get_version(tool_cfg.get("binary", "")) if installed_after else None

    if installed_after:
        before = version_before or "unknown"
        after = version_after or "unknown"
        print(f"{tool_name}: done ({before} -> {after})")
        if regen_completions:
            _regenerate_completions()
        return 0

    print(f"{tool_name}: command succeeded but binary still not found", file=sys.stderr)
    return 1


def update_many(tool_names: list[str], dry_run: bool = False, method: str | None = None) -> int:
    """Update multiple tools and return a combined exit status."""
    if not tool_names:
        print("No tools selected for update.", file=sys.stderr)
        return 1

    failed: list[str] = []
    succeeded = 0
    for name in tool_names:
        print()
        rc = update_tool(name, dry_run=dry_run, method=method, regen_completions=False)
        if rc != 0:
            failed.append(name)
        else:
            succeeded += 1

    if succeeded and not dry_run:
        _regenerate_completions()

    if failed:
        print(f"\nFailed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `ai-cli update`."""
    parser = argparse.ArgumentParser(
        prog="ai-cli update",
        description="Install or update wrapped CLI tools.",
    )
    parser.add_argument(
        "tool",
        nargs="?",
        choices=available_targets(),
        help="Tool to update.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Update all tools.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show commands without running them.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Show configured update commands.",
    )
    parser.add_argument(
        "--method",
        "-m",
        help="Install method to use (e.g. npm, brew, macports, curl). "
        "Use --list-methods to see available methods per tool.",
    )
    parser.add_argument(
        "--list-methods",
        action="store_true",
        help="Show available install methods per tool.",
    )
    args = parser.parse_args(argv)

    registry = load_registry()

    if args.list_methods:
        for name, spec in registry.items():
            best = spec.detect_best_method()
            default = spec.install_command or "(none)"
            print(f"{name}:")
            print(f"  default: {default}")
            for method, cmd in spec.install_methods.items():
                marker = " <- auto-detected" if method == best else ""
                print(f"  {method}: {cmd}{marker}")
            print()
        return 0

    if args.list:
        for name, spec in registry.items():
            cmd = spec.install_command or "(none)"
            print(f"{name:<8} {cmd}")
        return 0

    if args.all:
        return update_many(list(registry.keys()), dry_run=args.dry_run, method=args.method)

    target = args.tool
    if not target:
        print("Specify a tool or use --all.", file=sys.stderr)
        return 1

    return update_tool(target, dry_run=args.dry_run, method=args.method)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

from ai_cli import completion_gen


def test_extract_flags_parses_short_long_and_value_hints() -> None:
    help_text = """
Options:
  -c, --config <FILE>   Path to config
  --mode=VALUE          Execution mode
  --dry-run             Skip writes
"""

    parsed = completion_gen._extract_flags(help_text)

    assert ("-c", "--config", True) in parsed
    assert (None, "--mode", True) in parsed
    assert (None, "--dry-run", False) in parsed


def test_extract_commands_reads_commands_block() -> None:
    help_text = """
Available Commands:
  run      Execute task
  status   Show status

Options:
  -h, --help  Show help
"""

    commands = completion_gen._extract_commands(help_text)

    assert commands == ["run", "status"]


def test_classify_flags_detects_file_and_directory_flags() -> None:
    flags = [
        ("-c", "--config", True),
        (None, "--worktree", True),
        (None, "--schema", True),
        (None, "--verbose", False),
    ]

    file_flags, dir_flags = completion_gen._classify_flags(flags)

    assert file_flags == ["--config", "--schema"]
    assert dir_flags == ["--worktree"]

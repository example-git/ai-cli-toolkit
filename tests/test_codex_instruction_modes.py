from __future__ import annotations

import pytest

from ai_cli.main_helpers import parse_wrapper_overrides
from ai_cli.proxy import build_mitmdump_cmd


def test_parse_wrapper_overrides_reads_developer_mode() -> None:
    remaining, overrides = parse_wrapper_overrides(
        [
            "--ai-cli-developer-instructions-mode",
            "append",
            "--flag-for-tool",
        ]
    )

    assert remaining == ["--flag-for-tool"]
    assert overrides["developer_instructions_mode"] == "append"


def test_build_mitmdump_cmd_sets_developer_mode_option() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        developer_instructions_mode="prepend",
    )

    assert "--set" in cmd
    assert "developer_instructions_mode=prepend" in cmd


def test_build_mitmdump_cmd_sets_codex_prompt_file_option() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        codex_developer_prompt_file="/tmp/codex.txt",
    )

    assert "codex_developer_prompt_file=/tmp/codex.txt" in cmd


def test_parse_wrapper_overrides_rejects_invalid_developer_mode() -> None:
    with pytest.raises(SystemExit):
        parse_wrapper_overrides(
            [
                "--ai-cli-developer-instructions-mode",
                "invalid",
            ]
        )

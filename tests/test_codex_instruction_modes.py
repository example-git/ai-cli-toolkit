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


def test_build_mitmdump_cmd_sets_tool_instructions_file_option() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        tool_instructions_file="/tmp/tool.txt",
    )

    assert "tool_instructions_file=/tmp/tool.txt" in cmd


def test_build_mitmdump_cmd_sets_layered_guideline_options() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        base_instructions_file="/tmp/base.txt",
        project_instructions_file="/tmp/project.txt",
        tool_instructions_file="/tmp/tool.txt",
        canary_rule="CANARY",
    )

    assert "base_instructions_file=/tmp/base.txt" in cmd
    assert "project_instructions_file=/tmp/project.txt" in cmd
    assert "tool_instructions_file=/tmp/tool.txt" in cmd
    assert "canary_rule=CANARY" in cmd


def test_build_mitmdump_cmd_preserves_explicit_empty_inline_override() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        instructions_text="",
        instructions_text_explicit=True,
    )

    assert "system_instructions_text=" in cmd
    assert "system_instructions_text_explicit=true" in cmd


def test_parse_wrapper_overrides_reads_personality_injection_file() -> None:
    remaining, overrides = parse_wrapper_overrides(
        [
            "--ai-cli-gemini-canary-thought-injection",
            "off",
            "--flag-for-tool",
        ]
    )
    assert remaining == ["--flag-for-tool"]
    assert overrides["gemini_canary_thought_injection"] == "off"


def test_build_mitmdump_cmd_sets_personality_injection_options() -> None:
    cmd = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/backend-api/codex/responses",
        wrapper_log_file="/tmp/wrapper.log",
        codex_personality_file="/tmp/personality.txt",
    )
    assert "codex_personality_file=/tmp/personality.txt" in cmd

    cmd2 = build_mitmdump_cmd(
        mitmdump_bin="mitmdump",
        host="127.0.0.1",
        port=8080,
        addon_paths=["/tmp/addon.py"],
        target_path="/v1internal:generateContent",
        wrapper_log_file="/tmp/wrapper.log",
        gemini_canary_thought_injection_enabled=False,
    )
    assert "gemini_canary_thought_injection_enabled=false" in cmd2


def test_parse_wrapper_overrides_rejects_invalid_developer_mode() -> None:
    with pytest.raises(SystemExit):
        parse_wrapper_overrides(
            [
                "--ai-cli-developer-instructions-mode",
                "invalid",
            ]
        )

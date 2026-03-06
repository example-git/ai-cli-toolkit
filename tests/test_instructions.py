from __future__ import annotations

import json
from pathlib import Path

from ai_cli import instructions


def test_compose_simple_variants() -> None:
    assert instructions.compose_simple("base", "canary") == "canary\n\nbase"
    assert instructions.compose_simple("", "canary") == "canary"
    assert instructions.compose_simple("base", "") == "base"


def test_compose_instructions_uses_expected_layer_order(monkeypatch) -> None:
    monkeypatch.setattr(instructions, "resolve_base_instructions", lambda: "BASE")
    monkeypatch.setattr(
        instructions,
        "resolve_tool_instructions",
        lambda tool_name: f"TOOL:{tool_name}",
    )
    monkeypatch.setattr(
        instructions,
        "resolve_project_instructions",
        lambda project_cwd="", remote_spec="": "PROJECT",
    )
    monkeypatch.setattr(
        instructions,
        "resolve_user_instructions",
        lambda custom_path="": "USER",
    )

    composed = instructions.compose_instructions(
        canary_rule="CANARY",
        tool_name="codex",
        instructions_file="/tmp/custom.txt",
        project_cwd="/tmp/project",
        remote_spec="example@host:/tmp/project",
    )

    assert composed == "CANARY\n\nBASE\n\nTOOL:codex\n\nPROJECT\n\nUSER"


def test_compose_instructions_prefers_inline_user_text(monkeypatch) -> None:
    monkeypatch.setattr(instructions, "resolve_base_instructions", lambda: "BASE")
    monkeypatch.setattr(
        instructions,
        "resolve_project_instructions",
        lambda project_cwd="", remote_spec="": "",
    )
    monkeypatch.setattr(instructions, "resolve_user_instructions", lambda custom_path="": "USER")

    composed = instructions.compose_instructions(
        canary_rule="",
        tool_name="",
        instructions_text="INLINE",
    )

    assert composed == "BASE\n\nINLINE"


def test_ensure_project_instructions_file_uses_central_project_prompts_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(instructions, "DEFAULT_AI_CLI_DIR", str(tmp_path / ".ai-cli"))
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    legacy_dir = project_dir / ".ai-cli"
    legacy_dir.mkdir()
    legacy_file = legacy_dir / "project_instructions.txt"
    legacy_file.write_text("legacy\n", encoding="utf-8")

    resolved = Path(
        instructions.ensure_project_instructions_file(project_cwd=str(project_dir))
    )

    assert resolved.parent.parent == tmp_path / ".ai-cli" / "project-prompts"
    assert resolved.read_text(encoding="utf-8") == "legacy\n"
    meta = json.loads((resolved.parent / "meta.json").read_text(encoding="utf-8"))
    assert meta["instructions_file"] == str(resolved)
    assert meta["remote_spec"] == ""


def test_resolve_instructions_file_creates_default_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(instructions, "DEFAULT_AI_CLI_DIR", str(tmp_path))

    resolved = instructions.resolve_instructions_file("")
    resolved_path = Path(resolved)

    assert resolved_path == tmp_path / instructions.DEFAULT_INSTRUCTIONS_FILE
    assert resolved_path.is_file()
    assert resolved_path.read_text(encoding="utf-8") == ""


def test_resolve_base_system_text_from_inline_and_file(tmp_path) -> None:
    source, text = instructions.resolve_base_system_text(" inline ", "")
    assert (source, text) == ("inline text", "inline")

    p = tmp_path / "system.txt"
    p.write_text("from-file\n", encoding="utf-8")

    source, text = instructions.resolve_base_system_text("", str(p))
    assert source == f"file {p}"
    assert text == "from-file"

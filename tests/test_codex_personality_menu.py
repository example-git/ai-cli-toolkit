from __future__ import annotations

import json

from prompt_toolkit.keys import Keys

from ai_cli import codex_personality_menu


def test_load_existing_sections_reads_json_payload(tmp_path) -> None:
    path = tmp_path / "codex-personality.txt"
    path.write_text(
        json.dumps(
            {
                "personality": "Be blunt.",
                "interaction_style": "Stay direct.",
                "escalation": "Challenge weak assumptions.",
            }
        ),
        encoding="utf-8",
    )

    loaded = codex_personality_menu._load_existing_sections(path)

    assert loaded == {
        "personality": "Be blunt.",
        "interaction_style": "Stay direct.",
        "escalation": "Challenge weak assumptions.",
    }


def test_load_existing_sections_reads_markdown_prompt(tmp_path) -> None:
    path = tmp_path / "codex-personality.txt"
    path.write_text(
        "# Personality\n\nA\n\n## Interaction Style\nB\n\n## Escalation\nC\n",
        encoding="utf-8",
    )

    loaded = codex_personality_menu._load_existing_sections(path)

    assert loaded["personality"] == "A"
    assert loaded["interaction_style"] == "B"
    assert loaded["escalation"] == "C"


def test_load_existing_sections_falls_back_to_defaults_snapshot(tmp_path) -> None:
    path = tmp_path / "codex-personality.txt"
    path.write_text("", encoding="utf-8")
    defaults = tmp_path / "codex-personality.defaults.json"
    defaults.write_text(
        json.dumps(
            {
                "personality": "API personality",
                "interaction_style": "API interaction",
                "escalation": "API escalation",
            }
        ),
        encoding="utf-8",
    )

    loaded = codex_personality_menu._load_existing_sections(path)

    assert loaded == {
        "personality": "API personality",
        "interaction_style": "API interaction",
        "escalation": "API escalation",
    }


def test_load_existing_sections_prefers_saved_override_over_defaults(tmp_path) -> None:
    path = tmp_path / "codex-personality.txt"
    path.write_text(
        json.dumps(
            {
                "personality": "Saved personality",
                "interaction_style": "Saved interaction",
                "escalation": "Saved escalation",
            }
        ),
        encoding="utf-8",
    )
    defaults = tmp_path / "codex-personality.defaults.json"
    defaults.write_text(
        json.dumps(
            {
                "personality": "API personality",
                "interaction_style": "API interaction",
                "escalation": "API escalation",
            }
        ),
        encoding="utf-8",
    )

    loaded = codex_personality_menu._load_existing_sections(path)

    assert loaded == {
        "personality": "Saved personality",
        "interaction_style": "Saved interaction",
        "escalation": "Saved escalation",
    }


def test_write_payload_emits_stable_json_shape(tmp_path) -> None:
    path = tmp_path / "codex-personality.txt"

    codex_personality_menu._write_payload(
        path,
        {
            "personality": "One",
            "interaction_style": "Two",
            "escalation": "Three",
        },
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "personality": "One",
        "interaction_style": "Two",
        "escalation": "Three",
    }


def test_field_key_bindings_support_enter_and_escape_enter() -> None:
    bindings = codex_personality_menu._field_key_bindings()

    assert bindings.get_bindings_for_keys((Keys.ControlM,))
    assert bindings.get_bindings_for_keys((Keys.Escape, Keys.ControlM))

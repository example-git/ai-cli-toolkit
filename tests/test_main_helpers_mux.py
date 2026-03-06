from __future__ import annotations

from pathlib import Path

from ai_cli import main_helpers


def test_ensure_installed_mux_refreshes_stale_binary(monkeypatch, tmp_path: Path) -> None:
    installed = tmp_path / "installed-ai-mux"
    packaged = tmp_path / "packaged-ai-mux"
    installed.write_text("old-build\n", encoding="utf-8")
    packaged.write_text("new-build\n", encoding="utf-8")
    installed.chmod(0o755)
    packaged.chmod(0o755)

    monkeypatch.setattr(main_helpers, "_INSTALLED_MUX", installed)
    monkeypatch.setattr(main_helpers, "_packaged_mux_binary", lambda: packaged)
    monkeypatch.setattr(
        main_helpers.subprocess,
        "run",
        lambda *args, **kwargs: None,
    )

    resolved = main_helpers._ensure_installed_mux()

    assert resolved == installed
    assert installed.read_text(encoding="utf-8") == "new-build\n"


def test_ensure_installed_mux_keeps_matching_binary(monkeypatch, tmp_path: Path) -> None:
    installed = tmp_path / "installed-ai-mux"
    packaged = tmp_path / "packaged-ai-mux"
    installed.write_text("same-build\n", encoding="utf-8")
    packaged.write_text("same-build\n", encoding="utf-8")
    installed.chmod(0o755)
    packaged.chmod(0o755)

    monkeypatch.setattr(main_helpers, "_INSTALLED_MUX", installed)
    monkeypatch.setattr(main_helpers, "_packaged_mux_binary", lambda: packaged)

    copy_calls: list[tuple[Path, Path]] = []

    def _copy2(src: Path, dst: Path) -> Path:
        copy_calls.append((src, dst))
        raise AssertionError("copy2 should not run when binaries match")

    monkeypatch.setattr(main_helpers.shutil, "copy2", _copy2)

    resolved = main_helpers._ensure_installed_mux()

    assert resolved == installed
    assert copy_calls == []

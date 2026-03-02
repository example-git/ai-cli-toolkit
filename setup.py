from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.develop import develop as _develop


ROOT = Path(__file__).resolve().parent
MUX_DIR = ROOT / "mux"
MUX_TARGET = MUX_DIR / "target" / "release" / "ai-mux"
PKG_BIN_DIR = ROOT / "ai_cli" / "bin"
PKG_BIN = PKG_BIN_DIR / "ai-mux"


def _build_and_stage_ai_mux() -> None:
    cargo = shutil.which("cargo")
    if not cargo or not MUX_DIR.is_dir():
        return

    try:
        subprocess.run([cargo, "build", "--release"], cwd=MUX_DIR, check=True)
    except Exception as exc:
        print(f"warning: failed to build ai-mux: {exc}")
        return

    if not MUX_TARGET.is_file():
        return

    PKG_BIN_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MUX_TARGET, PKG_BIN)
    os.chmod(PKG_BIN, 0o755)


class build_py(_build_py):
    def run(self) -> None:
        _build_and_stage_ai_mux()
        super().run()


class develop(_develop):
    def run(self) -> None:
        _build_and_stage_ai_mux()
        super().run()


setup(cmdclass={"build_py": build_py, "develop": develop})

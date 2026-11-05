"""Builds the wheel and imports it from a clean venv, away from src/.

``portal`` loads its templates at import, so a wheel without them fails on import.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_a_built_wheel_installs_with_its_templates_and_its_command(tmp_path: Path) -> None:
    def uv(*args: str) -> None:
        subprocess.run(["uv", *args], check=True, capture_output=True, text=True, cwd=tmp_path)

    uv("build", "--wheel", "--out-dir", str(tmp_path / "dist"), str(PROJECT))
    wheels = list((tmp_path / "dist").glob("*.whl"))
    assert len(wheels) == 1
    uv("venv", str(tmp_path / "venv"))
    python = tmp_path / "venv/bin/python"
    uv("pip", "install", "--python", str(python), str(wheels[0]))

    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import cameraprovision.portal as p;"
            "from cameraprovision.portal import render_form;"
            "assert 'name=\"broker_host\"' in render_form([]);"
            "print(p.__file__)",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert str(tmp_path / "venv") in probe.stdout

    command = subprocess.run(
        [str(tmp_path / "venv/bin/camera-provision"), "run", "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert "--bridge-config" in command.stdout

"""Exercises scripts/smoke_test.py itself against the dev install (SPEC
§13's CI smoke test, run here as a real subprocess round trip). Slow: it
launches a real `maier` server and polls it over HTTP.
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"


@pytest.mark.slow
def test_smoke_script_passes_against_dev_install(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            f"{sys.executable} -m maier.cli",
            str(tmp_path / "folder"),
            "--timeout",
            "30",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout


@pytest.mark.slow
def test_smoke_script_fails_for_a_bogus_command(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "false --not-a-real-maier-binary",
            str(tmp_path / "folder"),
            "--timeout",
            "3",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "FAIL" in result.stdout

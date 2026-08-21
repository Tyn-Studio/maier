"""CLI is exercised as a subprocess: settings.py reads CULLER_FOLDER at
Django-settings import time, so each invocation needs a fresh process to
pick up a different working folder.
"""

import subprocess
import sys


def test_status_on_empty_folder_exits_zero(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "culler.cli", "status", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "optional: 0" in result.stdout
    assert "selected: 0" in result.stdout
    assert "rejected: 0" in result.stdout


def test_status_on_missing_folder_errors(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = subprocess.run(
        [sys.executable, "-m", "culler.cli", "status", str(missing)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1


def test_bare_cli_prints_usage_hint():
    result = subprocess.run(
        [sys.executable, "-m", "culler.cli"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "culler open" in result.stdout

"""CLI is exercised as a subprocess: settings.py reads CULLER_FOLDER at
Django-settings import time, so each invocation needs a fresh process to
pick up a different working folder.

Window-mode paths (pywebview) can't run headless in a test process, so
these tests set CULLER_FORCE_NO_WINDOW=1 to force the browser-mode
fallback -- the same flag CI/headless smoke tests use (see cli.py).
"""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

FORCE_NO_WINDOW = {"CULLER_FORCE_NO_WINDOW": "1"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_healthz(port: int, timeout: float = 20.0) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"server on port {port} never answered /healthz: {last_error}")


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
    # Force the no-window fallback: bare `culler` now tries a pywebview
    # home screen first, which must not actually run in a headless test.
    result = subprocess.run(
        [sys.executable, "-m", "culler.cli"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **FORCE_NO_WINDOW},
    )
    assert result.returncode == 0
    assert "culler open" in result.stdout


def test_bare_cli_browser_flag_prints_browser_hint():
    result = subprocess.run(
        [sys.executable, "-m", "culler.cli", "--browser"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "culler open PATH --browser" in result.stdout


def test_open_browser_mode_serves_healthz(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "culler.cli",
            "open",
            str(tmp_path),
            "--browser",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_healthz(port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_open_default_mode_falls_back_to_browser_when_window_forced_off(tmp_path):
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "culler.cli", "open", str(tmp_path), "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **FORCE_NO_WINDOW},
    )
    try:
        _wait_for_healthz(port)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_open_records_recent_folder(tmp_path):
    port = _free_port()
    config_dir = tmp_path / "config"
    working_folder = tmp_path / "photos"
    working_folder.mkdir()

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "culler.cli",
            "open",
            str(working_folder),
            "--browser",
            "--port",
            str(port),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CULLER_CONFIG_DIR": str(config_dir)},
    )
    try:
        _wait_for_healthz(port)
        recents_file = config_dir / "recent_folders.json"
        deadline = time.monotonic() + 5
        while not recents_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert recents_file.exists()
        data = json.loads(recents_file.read_text())
        assert len(data) == 1
        assert data[0]["path"] == str(working_folder.resolve())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

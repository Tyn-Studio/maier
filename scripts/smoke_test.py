#!/usr/bin/env python3
"""CI smoke test (SPEC §13): launch a built app bundle headless, hit
`/healthz`, assert 200. Catches PyInstaller missing-data-file breakage
before it reaches a GitHub Release.

Usage:
    python scripts/smoke_test.py <binary-or-command> <working-folder> [--port N] [--timeout SECS]

`<binary-or-command>` is normally a path to the built app (e.g.
`dist/Culler/Culler` or `dist/Culler.app/Contents/MacOS/Culler`), but it
also accepts a *space-separated command* so the script can be exercised
against the dev install today, e.g.:

    python scripts/smoke_test.py "uv run culler" /tmp/somefolder

(shlex-split, so quote it as one argv[1] if it contains spaces).

Always terminates the child process, and always prints a final
PASS/FAIL line. Exit code 0 on success, 1 on failure.
"""

from __future__ import annotations

import argparse
import os
import shlex
import socket
import subprocess
import time
import urllib.error
import urllib.request


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_healthz(port: int, timeout: float) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.monotonic() + timeout
    last_error = "never attempted"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True, "ok"
                last_error = f"unexpected status {resp.status}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    return False, last_error


def run_smoke_test(binary: str, folder: str, *, port: int | None, timeout: float) -> bool:
    port = port or _free_port()
    cmd = shlex.split(binary) + ["open", folder, "--browser", "--port", str(port)]

    env = {**os.environ, "CULLER_FORCE_NO_WINDOW": "1"}

    print(f"smoke_test: launching: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    try:
        ok, detail = _wait_for_healthz(port, timeout)
        if ok:
            print(f"PASS: /healthz responded 200 on port {port}")
        else:
            print(f"FAIL: /healthz never responded on port {port}: {detail}")
        return ok
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            output = proc.stdout.read()
            if output:
                print("---- child output ----")
                print(output)
                print("-----------------------")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "binary", help="path to the built binary, or a command like 'uv run culler'"
    )
    parser.add_argument("folder", help="working folder to open (created if it doesn't exist)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    os.makedirs(args.folder, exist_ok=True)

    ok = run_smoke_test(args.binary, args.folder, port=args.port, timeout=args.timeout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

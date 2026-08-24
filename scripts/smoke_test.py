#!/usr/bin/env python3
"""CI smoke test (SPEC §13): launch a built app bundle headless and hit
every top-level page — /healthz, /grid, /summary, /dupes, and /accounts
with a seeded iCloud account-state file. Catches PyInstaller
missing-data-file breakage (templates, statics) before it reaches a
GitHub Release; the accounts-with-state check exists because a missing
template only surfaced on that page's non-empty path (2026-08-24).

Usage:
    python scripts/smoke_test.py <binary-or-command> <working-folder> [--port N] [--timeout SECS]

`<binary-or-command>` is normally a path to the built app (e.g.
`dist/Maier/Maier` or `dist/Maier.app/Contents/MacOS/Maier`), but it
also accepts a *space-separated command* so the script can be exercised
against the dev install today, e.g.:

    python scripts/smoke_test.py "uv run maier" /tmp/somefolder

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


# Every page a user can reach from the nav, with a substring its template
# must render. /accounts is checked with a seeded account-state file (see
# _seed_account_state) so the per-account row partials render too — the
# empty page alone would miss include-level template breakage. /setup is
# checked unconditionally (PLAN T29): the wizard renders regardless of the
# working-range gate, so this catches its own template breakage the same
# way. /grid needs a seeded working range (see _seed_settings) — T29's
# setup-wizard gate would otherwise redirect a fresh, never-configured
# folder's /grid to /setup instead of rendering the real grid.
_PAGE_CHECKS = [
    ("/grid", "filter-bar"),
    ("/setup", "Set up Maier"),
    ("/summary", "Summary"),
    ("/dupes", "unresolved"),
    ("/accounts", "smoke-test@example.com"),
]


def _seed_account_state(folder: str) -> None:
    state_dir = os.path.join(folder, "icloud-state")
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "smoke-test-example-com.json"), "w") as f:
        f.write(
            '{"account": "smoke-test@example.com", "cursor": null,'
            ' "decisions": {}, "downloaded": {}, "version": 1}'
        )


def _seed_settings(folder: str) -> None:
    """PLAN T29: a fresh folder has no working date range configured, which
    now gates /grid behind the setup wizard (see `_PAGE_CHECKS`'s comment
    above). Seed an "everything" range so the smoke test keeps exercising
    the real grid template, same rationale as `_seed_account_state`.
    """
    with open(os.path.join(folder, "maier-settings.json"), "w") as f:
        f.write(
            '{"export_destination": "", "export_mode": "manual",'
            ' "export_date_prefix": false, "working_from": "1970-01-01",'
            ' "working_to": "", "version": 1}'
        )


def _check_pages(port: int) -> tuple[bool, str]:
    for path, must_contain in _PAGE_CHECKS:
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if resp.status != 200:
                    return False, f"{path} returned {resp.status}"
                if must_contain not in body:
                    return False, f"{path} rendered without expected content {must_contain!r}"
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            return False, f"{path} failed: {exc}"
    return True, "ok"


def run_smoke_test(binary: str, folder: str, *, port: int | None, timeout: float) -> bool:
    port = port or _free_port()
    cmd = shlex.split(binary) + ["open", folder, "--browser", "--port", str(port)]

    env = {**os.environ, "MAIER_FORCE_NO_WINDOW": "1"}

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
        if not ok:
            print(f"FAIL: /healthz never responded on port {port}: {detail}")
            return False
        ok, detail = _check_pages(port)
        if ok:
            print(f"PASS: /healthz + {len(_PAGE_CHECKS)} pages OK on port {port}")
        else:
            print(f"FAIL: {detail}")
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
    parser.add_argument("binary", help="path to the built binary, or a command like 'uv run maier'")
    parser.add_argument("folder", help="working folder to open (created if it doesn't exist)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    os.makedirs(args.folder, exist_ok=True)
    _seed_account_state(args.folder)
    _seed_settings(args.folder)

    ok = run_smoke_test(args.binary, args.folder, port=args.port, timeout=args.timeout)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point for the PyInstaller-bundled desktop app (SPEC §13 Tier 3).

Not used by the PyPI-installed CLI (that's `culler.cli:main` directly, run
by the `culler` console script) -- this thin wrapper exists purely because
frozen/multiprocessing apps on Windows must call
`multiprocessing.freeze_support()` before anything else happens, and
PyInstaller needs a standalone script (not a package `__main__`) to point
its `Analysis` at.
"""

from __future__ import annotations

import multiprocessing
import sys


def run() -> int:
    multiprocessing.freeze_support()

    from culler.cli import main

    return main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(run())

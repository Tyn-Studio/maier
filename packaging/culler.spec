# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Tier-3 double-click bundle (SPEC §13).

One-dir build (not one-file): faster startup, and lets us drop an
unpacked exiftool binary + Django's templates/static/migrations next to
the executable instead of unpacking them into a temp dir on every launch.

Not run as part of this task -- exercised by `.github/workflows/release.yml`
on tag pushes, via `uv run pyinstaller packaging/culler.spec`
(`uv sync --group build` installs pyinstaller first). Build locally with the
same two commands.
"""

import sys
from pathlib import Path

# SPECPATH is injected by PyInstaller into the spec's exec globals.
REPO_ROOT = Path(SPECPATH).parent  # noqa: F821
SRC_ROOT = REPO_ROOT / "src"
CULLER_PKG = SRC_ROOT / "culler"

block_cipher = None

# --- Data files -------------------------------------------------------
# (source, destination-relative-to-bundle) pairs. Destinations mirror the
# package layout so `culler.settings` (APP_DIRS template loading,
# staticfiles finders, migration discovery) finds them exactly where it
# would in a normal `pip install`.
datas = [
    (str(CULLER_PKG / "core" / "templates"), "culler/core/templates"),
    (str(CULLER_PKG / "core" / "static"), "culler/core/static"),
    (str(CULLER_PKG / "core" / "migrations"), "culler/core/migrations"),
]

# TODO(exiftool-bundling): SPEC §13 Tier 3 ships a per-platform exiftool
# binary inside the bundle. Not wired yet -- exiftool auto-download (T12,
# core/exiftool.py) covers Tier 1/2 for now. Once per-platform binaries are
# fetched in CI before this spec runs, add e.g.:
#   datas.append((str(REPO_ROOT / "packaging" / "vendor" / "exiftool" / sys.platform), "exiftool"))

# --- Hidden imports -----------------------------------------------------
# Django loads almost everything by dotted string (settings module, installed
# apps, middleware, DB backend, template backend, management commands), so
# PyInstaller's static analysis misses it all -- the first bundle build died
# with `No module named 'culler.settings'`. Collect the whole culler package
# plus every Django subsystem the settings reference dynamically.
# pywebview additionally picks its backend at runtime via platform detection;
# list the relevant backend explicitly per target platform.
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    *collect_submodules("culler"),
    *collect_submodules("django.contrib.staticfiles"),
    *collect_submodules("django.core.management"),
    *collect_submodules("django.db.backends.sqlite3"),
    *collect_submodules("django.template"),
    "django.middleware.security",
    "django.middleware.common",
    "django.middleware.csrf",
    "whitenoise.middleware",
    "whitenoise.storage",
]

if sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
elif sys.platform == "win32":
    hiddenimports += ["webview.platforms.winforms", "webview.platforms.edgechromium"]
else:
    hiddenimports += ["webview.platforms.gtk"]

a = Analysis(
    [str(REPO_ROOT / "packaging" / "launcher.py")],
    pathex=[str(SRC_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# TODO(name): rename the bundle away from "Culler" once the CTO picks the
# final product name (this drives the .app / .exe filename on every
# platform, so it should change alongside the pyproject/README renames).
APP_NAME = "Culler"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    # TODO(signing): v1 ships unsigned (SPEC §13). Developer-ID signing
    # (macOS codesign_identity / entitlements_file) and Windows Authenticode
    # signing are a fast-follow -- see release.yml for the stubbed env vars.
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=None,
        bundle_identifier=None,  # TODO(name): reverse-DNS id, e.g. "com.example.culler"
    )

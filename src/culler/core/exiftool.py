"""exiftool detection + RAW embedded-preview extraction (SPEC §12, §6 Phase
B item 1). exiftool is the only non-Python dependency and is optional at
runtime: absent-on-PATH with no auto-download yet (that's T12) is the normal
dev/test state. Every function here degrades to `None`/`False` rather than
raising -- callers (previews.py) fall back to the placeholder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from django.conf import settings

_EXTRACT_TIMEOUT_SECONDS = 30

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see previews.py/phaseb.py for the same workaround).
_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired)

# Cache is a (populated, path) pair rather than a single `Path | None`
# sentinel value, so a resolved-to-absent result (None) is distinguishable
# from "not looked up yet" -- avoids re-probing PATH/the data dir on every
# preview request.
_cache_populated = False
_cached_path: Path | None = None


def _reset_cache() -> None:
    """Test hook: forces the next `find_exiftool()` call to re-probe."""
    global _cache_populated, _cached_path
    _cache_populated = False
    _cached_path = None


def _probe_data_dir() -> Path | None:
    """Where T12's auto-download will land (SPEC §11: global data dir)."""
    candidate = settings.GLOBAL_DATA_DIR / "exiftool"
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def find_exiftool() -> Path | None:
    global _cache_populated, _cached_path
    if _cache_populated:
        return _cached_path

    which = shutil.which("exiftool")
    _cached_path = Path(which) if which else _probe_data_dir()
    _cache_populated = True
    return _cached_path


def _run(exiftool_path: Path, src: Path, flag: str) -> bytes:
    """Run `exiftool -b <flag> <src>`, returning stdout bytes (empty on any
    failure). No shell=True; args passed as a list.
    """
    try:
        result = subprocess.run(
            [str(exiftool_path), "-b", flag, str(src)],
            capture_output=True,
            timeout=_EXTRACT_TIMEOUT_SECONDS,
        )
    except _SUBPROCESS_ERRORS:
        return b""
    if result.returncode != 0:
        return b""
    return result.stdout


def extract_embedded_preview(exiftool_path: Path, src: Path, dest: Path) -> bool:
    """Extract a RAW file's embedded JPEG preview (SPEC §6 Phase B item 1):
    `-JpgFromRaw` first (larger, camera-generated preview on most RAW
    formats), `-PreviewImage` as a retry when that's empty (some
    formats/models only embed the smaller one). Never raises.
    """
    data = _run(exiftool_path, src, "-JpgFromRaw")
    if not data:
        data = _run(exiftool_path, src, "-PreviewImage")
    if not data:
        return False
    try:
        dest.write_bytes(data)
    except OSError:
        return False
    return True

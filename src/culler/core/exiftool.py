"""exiftool detection, pinned auto-download, and RAW embedded-preview
extraction (SPEC §12, §6 Phase B item 1). exiftool is the only non-Python
dependency and is optional at runtime: `find_exiftool()` degrades to `None`
when it's absent everywhere (PATH and the global data dir), and
`extract_embedded_preview()` degrades to `False` rather than raising --
callers (previews.py) fall back to the placeholder.

`ensure_exiftool()` (T12) is a standalone entry point for the pinned,
checksum-verified auto-download described in SPEC §12. It is deliberately
NOT wired into cli.py here -- the lead engineer wires the trigger once T11
(pywebview window/recents) also lands, to avoid two concurrent agents
touching cli.py.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("culler.exiftool")

_EXTRACT_TIMEOUT_SECONDS = 30
_DOWNLOAD_TIMEOUT_SECONDS = 60

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see previews.py/phaseb.py for the same workaround).
_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired)
_EXTRACT_ERRORS = (tarfile.TarError, OSError)

# --- Pinned exiftool download (SPEC §12) -----------------------------------
#
# exiftool.org distributes via SourceForge, which keeps every version's
# file available (exiftool.org's own /Image-ExifTool-X.Y.tar.gz URL only
# serves the current release and 404s after the next one ships). The sha256
# below is the tarball's hash as downloaded, cross-checked against the
# value published at https://exiftool.org/checksums.txt on 2026-08-21.
# When bumping the version, update BOTH constants from that checksums file.
EXIFTOOL_VERSION = "13.59"
EXIFTOOL_URL = (
    "https://sourceforge.net/projects/exiftool/files/"
    f"Image-ExifTool-{EXIFTOOL_VERSION}.tar.gz/download"
)
EXIFTOOL_SHA256: str | None = "668ea3acececb7235fbd0f4900e72d5f12c9b07e5c778fd36cb1e9b5828fd65a"

_download_lock = threading.Lock()

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
    """Single-file layout: `GLOBAL_DATA_DIR/exiftool` (SPEC §11: global data
    dir). Not what `ensure_exiftool()` produces (that's a versioned
    directory, see `_probe_versioned_dir`) but kept as a probe target so a
    manually-placed or future-packaged single-file binary is still found.
    """
    candidate = settings.GLOBAL_DATA_DIR / "exiftool"
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    except OSError:
        return None
    return None


def _versioned_exiftool_path() -> Path:
    """Where `ensure_exiftool()`'s pinned download lands and extracts (the
    tarball's own top-level directory name, kept verbatim so the script can
    resolve `lib/` relative to itself).
    """
    return (
        settings.GLOBAL_DATA_DIR
        / f"exiftool-{EXIFTOOL_VERSION}"
        / f"Image-ExifTool-{EXIFTOOL_VERSION}"
        / "exiftool"
    )


def _probe_versioned_dir() -> Path | None:
    candidate = _versioned_exiftool_path()
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
    _cached_path = Path(which) if which else (_probe_data_dir() or _probe_versioned_dir())
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


# --- Pinned auto-download (SPEC §12) ----------------------------------------


def _fetch(url: str, dest: Path) -> None:
    """Network seam: the only function that touches the network. Tests
    monkeypatch this directly rather than urllib internals.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "culler-exiftool-downloader"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response:
        with dest.open("wb") as f:
            shutil.copyfileobj(response, f)


def _verify_sha256(path: Path, expected: str) -> bool:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return hmac.compare_digest(digest.hexdigest(), expected.lower())


def _download_windows() -> Path | None:
    """The official Windows exiftool distribution is a .zip containing
    `exiftool(-k).exe`, which must itself be renamed to `exiftool.exe` to
    run non-interactively -- different enough from the macOS/Linux tarball
    flow to warrant its own implementation. Out of scope for M3; Windows is
    covered by SPEC §13 Tier 3 (bundled per-platform binary) instead.
    """
    raise NotImplementedError(
        "Windows exiftool auto-download is not implemented; see SPEC §13 Tier 3 (bundled binary)"
    )


def _download_and_extract() -> Path | None:
    """Blocking pinned download + checksum verify + safe extract. Caller
    (`_ensure_exiftool_locked`) holds `_download_lock` for the duration.
    Never raises -- every failure mode logs and returns None, cleaning up
    any partial extraction directory it created.
    """
    if os.name == "nt":
        try:
            return _download_windows()
        except NotImplementedError:
            logger.warning(
                "exiftool auto-download is not implemented on Windows "
                "(SPEC §13 Tier 3 ships a bundled binary instead)"
            )
            return None

    allow_unpinned = os.environ.get("CULLER_ALLOW_UNPINNED_EXIFTOOL") == "1"
    if EXIFTOOL_SHA256 is None and not allow_unpinned:
        logger.warning(
            "exiftool checksum is not pinned (EXIFTOOL_SHA256 is None); refusing to "
            "auto-download. Set CULLER_ALLOW_UNPINNED_EXIFTOOL=1 to override for dev use."
        )
        return None

    target = _versioned_exiftool_path()
    if target.is_file() and os.access(target, os.X_OK):
        return target

    extract_dir = target.parent.parent  # GLOBAL_DATA_DIR / exiftool-{VERSION}
    settings.GLOBAL_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=settings.GLOBAL_DATA_DIR) as tmp_name:
        tarball = Path(tmp_name) / "exiftool.tar.gz"
        try:
            _fetch(EXIFTOOL_URL, tarball)
        except OSError:
            logger.exception("exiftool download failed")
            return None

        if EXIFTOOL_SHA256 is not None:
            if not _verify_sha256(tarball, EXIFTOOL_SHA256):
                logger.error("exiftool tarball checksum mismatch; refusing to extract")
                return None
        else:
            logger.warning("CULLER_ALLOW_UNPINNED_EXIFTOOL=1: skipping checksum verification")

        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tarfile.open(tarball, "r:gz") as tf:
                tf.extractall(path=extract_dir, filter="data")
        except _EXTRACT_ERRORS:
            logger.exception("exiftool extraction failed")
            shutil.rmtree(extract_dir, ignore_errors=True)
            return None

    if not target.is_file():
        logger.error("exiftool extraction did not produce the expected binary at %s", target)
        shutil.rmtree(extract_dir, ignore_errors=True)
        return None

    try:
        target.chmod(target.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass

    return target


def _ensure_exiftool_locked() -> Path | None:
    with _download_lock:
        # Double-checked locking: a concurrent caller may have finished the
        # download while we were waiting for the lock, or a prior process
        # left a completed extraction behind -- re-probe before fetching so
        # a single flight never downloads twice.
        _reset_cache()
        found = find_exiftool()
        if found is not None:
            return found
        result = _download_and_extract()
        _reset_cache()
        return result


def ensure_exiftool(background: bool = False) -> Path | None:
    """Public entry point for SPEC §12's pinned, checksum-verified
    auto-download. Returns an already-present exiftool immediately (PATH or
    a prior completed download); otherwise attempts a fresh download.

    `background=True` kicks off the download on a daemon thread and returns
    None immediately -- callers that want a UI notice on completion should
    poll `find_exiftool()` (call `_reset_cache()` first, the cache doesn't
    auto-invalidate). `background=False` blocks until the download finishes
    or fails. Never raises.
    """
    found = find_exiftool()
    if found is not None:
        return found

    if background:
        threading.Thread(target=_ensure_exiftool_locked, daemon=True).start()
        return None

    return _ensure_exiftool_locked()

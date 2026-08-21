"""Capture-date fallback chain (SPEC §9): EXIF -> filename -> file mtime.

Pillow handles JPEG/HEIC/PNG/TIFF directly (fast, no subprocess). RAW and
video extensions aren't readable by Pillow's EXIF path, so for those we try
exiftool first (T13, SPEC §6 Phase A "via exiftool") when it's detected on
this machine; any failure (absent, timeout, unparseable output) falls
through to the existing Pillow/filename/mtime chain unchanged -- exiftool
must never be a hard dependency (CLAUDE.md rule 6).
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from PIL import ExifTags, Image

from . import exiftool as exiftool_module

_EXIF_DATETIME_ORIGINAL = 36867  # Exif IFD
_EXIF_DATETIME = 306  # 0th IFD fallback

_MIN_YEAR = 1990
_MAX_YEAR = 2100

# Redefined here rather than imported from scan.py: scan.py imports
# capture_datetime from this module, so importing scan.py back would be
# circular. Kept in sync manually with scan.py's IMAGE_EXTENSIONS/
# VIDEO_EXTENSIONS -- flagging as a known duplication (T13 brief allowed
# either "import ... or redefine locally, flag which").
_RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
_VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}
_EXIFTOOL_ONLY_EXTENSIONS = _RAW_EXTENSIONS | _VIDEO_EXTENSIONS

_EXIFTOOL_TIMEOUT_SECONDS = 10

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see exiftool.py/previews.py for the same workaround).
_EXIFTOOL_SUBPROCESS_ERRORS = (OSError, subprocess.TimeoutExpired)

# Conservative filename timestamp patterns, tried in order. Matched with
# `search` (not `fullmatch`) so prefixes like IMG_/PXL_ and suffixes like
# extensions or ".MP" don't need to be enumerated.
_FILENAME_PATTERNS = [
    # IMG_20250614_183012, 20250614_183012, PXL_20250614_183012...
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})_?(\d{2})(\d{2})(\d{2})(?!\d)"),
    # 2025-06-14 18.30.12, 2025-06-14_18-30-12
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ _](\d{2})[.:-](\d{2})[.:-](\d{2})(?!\d)"),
]


def capture_datetime(path: Path) -> tuple[datetime, str]:
    if path.suffix.lower() in _EXIFTOOL_ONLY_EXTENSIONS:
        dt = _from_exiftool(path)
        if dt is not None:
            return dt, "exif"

    dt = _from_exif(path)
    if dt is not None:
        return dt, "exif"

    dt = _from_filename(path.name)
    if dt is not None:
        return dt, "filename"

    return _from_mtime(path), "file_mtime"


def _from_exiftool(path: Path) -> datetime | None:
    """RAW/video capture date via exiftool (no -stay_open batching -- one
    process per file; flagged as future perf work, see PLAN T13 brief).
    Tries -DateTimeOriginal / -CreateDate / -MediaCreateDate in that order
    (`-s3` prints bare values, one per requested tag, empty line when a tag
    is absent) and parses the first non-empty line. Never raises.
    """
    exiftool_path = exiftool_module.find_exiftool()
    if exiftool_path is None:
        return None

    try:
        result = subprocess.run(
            [
                str(exiftool_path),
                "-s3",
                "-d",
                "%Y:%m:%d %H:%M:%S",
                "-DateTimeOriginal",
                "-CreateDate",
                "-MediaCreateDate",
                str(path),
            ],
            capture_output=True,
            timeout=_EXIFTOOL_TIMEOUT_SECONDS,
            text=True,
        )
    except _EXIFTOOL_SUBPROCESS_ERRORS:
        return None

    if result.returncode != 0:
        return None

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            naive = datetime.strptime(line, "%Y:%m:%d %H:%M:%S")
        except ValueError:
            return None
        return naive.astimezone(UTC)

    return None


def _from_exif(path: Path) -> datetime | None:
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            raw = None
            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            except Exception:
                exif_ifd = {}
            if exif_ifd:
                raw = exif_ifd.get(_EXIF_DATETIME_ORIGINAL)
            if not raw:
                raw = exif.get(_EXIF_DATETIME)
            if not raw:
                return None
            naive = datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S")
            # No timezone in EXIF -> interpret as local time, convert to UTC.
            return naive.astimezone(UTC)
    except Exception:
        return None


def _from_filename(name: str) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        match = pattern.search(name)
        if not match:
            continue
        year, month, day, hour, minute, second = (int(g) for g in match.groups())
        if not (_MIN_YEAR <= year <= _MAX_YEAR):
            continue
        try:
            naive = datetime(year, month, day, hour, minute, second)
        except ValueError:
            continue
        return naive.astimezone(UTC)
    return None


def _from_mtime(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

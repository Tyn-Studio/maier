"""Capture-date fallback chain (SPEC §9): EXIF -> filename -> file mtime.

Pillow only in M1 (exiftool absent, per PLAN decisions log / SPEC §12): must
degrade gracefully on unreadable/corrupt images, never raise.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from PIL import ExifTags, Image

_EXIF_DATETIME_ORIGINAL = 36867  # Exif IFD
_EXIF_DATETIME = 306  # 0th IFD fallback

_MIN_YEAR = 1990
_MAX_YEAR = 2100

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
    dt = _from_exif(path)
    if dt is not None:
        return dt, "exif"

    dt = _from_filename(path.name)
    if dt is not None:
        return dt, "filename"

    return _from_mtime(path), "file_mtime"


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

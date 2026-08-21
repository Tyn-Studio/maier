"""Fixture-folder builder for integration tests: creates tiny real JPEGs
(plus assorted non-media junk files) under a given root, matching a `spec`
dict. Nothing here is committed to the repo -- fixtures are generated at
test time, per CLAUDE.md convention.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

# Exif IFD pointer tag (0x8769) on the 0th IFD; DateTimeOriginal (36867)
# lives inside that sub-IFD. Mirrors the fallback chain's own read path in
# culler.core.metadata.
_EXIF_IFD_TAG = 0x8769
_DATETIME_ORIGINAL = 36867

_DEFAULT_SIZE = (8, 8)
_DEFAULT_COLOR = (100, 150, 200)


def build_fixture_folder(root: Path, spec: dict[str, dict | None]) -> None:
    """Build a tree of tiny fixture files under `root`.

    `spec` maps a POSIX-style relative path (e.g. "apple-luis/IMG_1.jpg",
    "selected/apple-luis/IMG_2.jpg", "notes.txt") to an options dict (or
    `None` for defaults). Recognized options:

      - "datetime_original": "YYYY:MM:DD HH:MM:SS" -- sets EXIF
        DateTimeOriginal (Exif IFD, tag 36867). Omit for no EXIF date, so
        the capture-date fallback chain falls through to a filename
        timestamp (if the name matches) or file mtime.
      - "mtime": float POSIX timestamp -- applied via os.utime after
        writing the file.
      - "junk": True -- write raw (non-image) bytes instead of a JPEG,
        for non-media files (e.g. a stray ".txt").
      - "content": bytes -- raw bytes to write when "junk" is set
        (default b"junk").
      - "size": (w, h) pixel size for the generated JPEG (default (8, 8)).
    """
    for rel_path, opts in spec.items():
        opts = opts or {}
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)

        if opts.get("junk"):
            path.write_bytes(opts.get("content", b"junk"))
        else:
            _make_jpeg(
                path,
                size=opts.get("size", _DEFAULT_SIZE),
                datetime_original=opts.get("datetime_original"),
            )

        mtime = opts.get("mtime")
        if mtime is not None:
            os.utime(path, (mtime, mtime))


def _make_jpeg(path: Path, size: tuple[int, int], datetime_original: str | None) -> None:
    img = Image.new("RGB", size, color=_DEFAULT_COLOR)
    if datetime_original:
        exif = img.getexif()
        exif_ifd = exif.get_ifd(_EXIF_IFD_TAG)
        exif_ifd[_DATETIME_ORIGINAL] = datetime_original
        img.save(path, "jpeg", exif=exif)
    else:
        img.save(path, "jpeg")

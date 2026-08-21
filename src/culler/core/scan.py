"""Phase A indexing (SPEC §6): tree walk, status-from-location, diff cache,
simple (size, mtime) move reconciliation. Idempotent; safe to run repeatedly
and safe to run in a background thread alongside request threads.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from django.db import connection

from .metadata import capture_datetime
from .models import Photo

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".tif",
    ".tiff",
    ".dng",
    ".cr2",
    ".cr3",
    ".nef",
    ".arw",
    ".raf",
    ".orf",
    ".rw2",
}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}
ALL_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

_MTIME_TOLERANCE = 1e-6


@dataclass
class ScanProgress:
    total: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False


def _status_and_provenance(rel_path: Path) -> tuple[str, str]:
    parts = rel_path.parts
    if parts and parts[0] == "selected":
        status = Photo.STATUS_SELECTED
        rest = parts[1:]
    elif parts and parts[0] == "rejected":
        status = Photo.STATUS_REJECTED
        rest = parts[1:]
    else:
        status = Photo.STATUS_OPTIONAL
        rest = parts
    provenance = rest[0] if len(rest) > 1 else ""
    return status, provenance


def _media_type(rel_path: Path) -> str:
    ext = rel_path.suffix.lower()
    return Photo.MEDIA_VIDEO if ext in VIDEO_EXTENSIONS else Photo.MEDIA_IMAGE


def _walk_candidates(folder: Path) -> list[Path]:
    candidates: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(folder):
        # Skip .culler/ and any other hidden dirs (safe default).
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel_dir = Path(dirpath).relative_to(folder)
        for fname in filenames:
            if fname.startswith("."):
                continue
            ext = Path(fname).suffix.lower()
            if ext not in ALL_EXTENSIONS:
                continue
            rel_path = fname if str(rel_dir) == "." else str(rel_dir / fname)
            candidates.append(Path(rel_path))
    return candidates


def scan(folder: Path, progress: ScanProgress) -> None:
    folder = Path(folder)
    try:
        candidates = _walk_candidates(folder)
        progress.total = len(candidates)

        existing = {p.relative_path: p for p in Photo.objects.all()}
        seen_paths: set[str] = set()
        # (size, mtime) -> [relative_path, ...] for files with no pre-existing
        # DB row, seen during this scan -- reconciliation candidates.
        new_by_key: dict[tuple[int, float], list[str]] = {}

        for rel_path in candidates:
            rel_str = rel_path.as_posix()
            seen_paths.add(rel_str)
            try:
                abspath = folder / rel_path
                st = abspath.stat()
                size = st.st_size
                mtime = st.st_mtime

                existing_photo = existing.get(rel_str)
                if (
                    existing_photo is not None
                    and not existing_photo.missing
                    and existing_photo.file_size == size
                    and abs(existing_photo.file_mtime - mtime) < _MTIME_TOLERANCE
                ):
                    continue  # unchanged, skip re-read

                status, provenance = _status_and_provenance(rel_path)
                media_type = _media_type(rel_path)
                captured_at, captured_at_source = capture_datetime(abspath)

                Photo.objects.update_or_create(
                    relative_path=rel_str,
                    defaults={
                        "status": status,
                        "provenance": provenance,
                        "file_size": size,
                        "file_mtime": mtime,
                        "media_type": media_type,
                        "captured_at": captured_at,
                        "captured_at_source": captured_at_source,
                        "missing": False,
                    },
                )

                if existing_photo is None:
                    new_by_key.setdefault((size, mtime), []).append(rel_str)
            except Exception as exc:  # per-file errors never abort the scan
                progress.errors.append(f"{rel_path}: {exc}")
            finally:
                progress.done += 1

        _reconcile_missing(existing, seen_paths, new_by_key)
    finally:
        progress.finished = True


def _reconcile_missing(
    existing: dict[str, Photo],
    seen_paths: set[str],
    new_by_key: dict[tuple[int, float], list[str]],
) -> None:
    for rel_str, photo in existing.items():
        if rel_str in seen_paths:
            continue  # still present (possibly just updated above)

        key = (photo.file_size, photo.file_mtime)
        matches = new_by_key.get(key, [])
        if len(matches) == 1:
            new_rel = matches[0]
            new_photo = Photo.objects.filter(relative_path=new_rel).first()
            if new_photo is None:
                Photo.objects.filter(pk=photo.pk).update(missing=True)
                continue
            status, provenance = _status_and_provenance(Path(new_rel))
            # Drop the placeholder row created for the "new" path this scan,
            # then re-link the original row (keeps id, sha256, captured_at).
            new_photo.delete()
            Photo.objects.filter(pk=photo.pk).update(
                relative_path=new_rel,
                status=status,
                provenance=provenance,
                missing=False,
            )
            # Consume the candidate: a second vanished row with the same
            # (size, mtime) must fall through to missing, not steal this path.
            del new_by_key[key]
        else:
            Photo.objects.filter(pk=photo.pk).update(missing=True)


_scan_state_lock = threading.Lock()
_current_scan: ScanProgress | None = None


def start_background_scan(folder: Path) -> ScanProgress:
    global _current_scan
    with _scan_state_lock:
        if _current_scan is not None and not _current_scan.finished:
            return _current_scan
        progress = ScanProgress()
        _current_scan = progress

    def _run() -> None:
        try:
            scan(folder, progress)
        finally:
            connection.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return progress

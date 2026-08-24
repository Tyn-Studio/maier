"""Export: COPY `selected/` to an external destination (PLAN T25). This is a
deliberate new capability beyond CLAUDE.md's normal "moves only, same
volume" rule -- export never touches the working folder or anything already
in it; it only ever ADDS files at the destination (never deletes/overwrites
there either, per the same "never overwrite" collision idiom `moves.py`
uses for in-folder moves).

Two entry points:
- `export_selected` -- manual "Export now" / the full one-shot copy.
- `export_one` -- a single file, used by `maybe_auto_export`'s per-select
  hook (culling.py) and per-download hook (downloads.py) so automatic mode
  doesn't need to re-walk the whole `selected/` tree on every select.

Background-thread wiring (`start_background_export`) mirrors
`scan.py`/`pull.py`'s single-flight progress-object pattern, but
`ExportResult` itself (the PLAN-pinned return type of `export_selected`/
`export_one`) is deliberately left with just `copied`/`skipped`/`errors` --
`ExportProgress` (not part of the pinned interface) is the thin wrapper that
adds the `finished` flag a poll partial needs, same shape as
`scan.ScanProgress` (flagged: brief calls for "ExportResult+running flag",
interpreted here as a wrapper around it rather than adding fields to the
pinned dataclass, since other code may come to depend on ExportResult's
exact shape as the plain return value of the two functions above).
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from django.db import connection

from . import folder_settings, moves
from .models import Photo

logger = logging.getLogger("maier.export")

# mtime comparison tolerance: shutil.copy2 preserves the source mtime on the
# copy (sub-second precision on most filesystems), but a destination on a
# coarser-resolution filesystem (e.g. FAT32's 2s ticks) can round it -- a
# couple of seconds of slack keeps the "already exported, identical" check
# stable across such destinations without risking false-positive skips of
# genuinely different files (which would need a much larger mtime drift to
# collide with plausible re-export timing).
_MTIME_TOLERANCE = 2.0


@dataclass
class ExportResult:
    copied: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


def _validate_dest(folder: Path, dest: Path) -> None:
    folder = Path(folder).resolve()
    dest = Path(dest).resolve()
    try:
        inside = dest == folder or dest.is_relative_to(folder)
    except ValueError:
        inside = False
    if inside:
        raise ValueError(
            f"export destination {dest} must not be inside the working folder {folder}"
        )


def _dest_filename(filename: str, *, date_prefix: bool, captured_at: datetime | None) -> str:
    if date_prefix and captured_at is not None:
        return f"{captured_at:%Y-%m-%d}_{filename}"
    return filename


def _same_file(dest_path: Path, src_size: int, src_mtime: float) -> bool:
    try:
        st = dest_path.stat()
    except OSError:
        return False
    return st.st_size == src_size and abs(st.st_mtime - src_mtime) < _MTIME_TOLERANCE


def _copy_file(
    src: Path, dest_dir: Path, *, date_prefix: bool, captured_at: datetime | None
) -> tuple[bool, bool, str | None]:
    """Returns (copied, skipped, error). Never overwrites: an existing file
    with the same size+mtime is treated as "already exported" (skipped); an
    existing file that differs gets a ' (n)'-suffixed name instead (reusing
    `moves._unique_path`'s collision idiom) -- the original at the
    destination is never touched.
    """
    filename = _dest_filename(src.name, date_prefix=date_prefix, captured_at=captured_at)
    dest_path = dest_dir / filename
    try:
        src_stat = src.stat()
        if dest_path.exists():
            if _same_file(dest_path, src_stat.st_size, src_stat.st_mtime):
                return False, True, None
            dest_path = moves._unique_path(dest_path)
        shutil.copy2(src, dest_path)
        return True, False, None
    except OSError as exc:
        return False, False, f"{src}: {exc}"


def export_selected(folder: Path, dest: Path, *, date_prefix: bool = False) -> ExportResult:
    """Copy every file under `{folder}/selected/` to `dest` (flat, per T24 --
    but `rglob` also picks up any legacy mirrored subfolder content, since
    `flatten_selected` may not have run yet on an old cache). Dot-files are
    ignored. `dest` must not be inside the working folder -- raises
    ValueError (a recursive selected/-into-selected/ copy would be absurd
    and, worse, keep growing on every re-run).
    """
    folder = Path(folder)
    dest = Path(dest)
    _validate_dest(folder, dest)

    selected_root = folder / Photo.STATUS_SELECTED
    result = ExportResult()
    if not selected_root.is_dir():
        return result

    dest.mkdir(parents=True, exist_ok=True)

    for src in sorted(selected_root.rglob("*")):
        if not src.is_file() or src.name.startswith("."):
            continue

        captured_at = None
        if date_prefix:
            # Only needed to name the file -- skip the DB round-trip
            # entirely when no prefix was requested (also keeps
            # `export_selected` usable without a DB connection at all in
            # that mode).
            rel = src.relative_to(folder).as_posix()
            photo = Photo.objects.filter(relative_path=rel).first()
            if photo is not None:
                captured_at = photo.captured_at

        copied, skipped, error = _copy_file(
            src, dest, date_prefix=date_prefix, captured_at=captured_at
        )
        if error:
            result.errors.append(error)
        elif copied:
            result.copied += 1
        elif skipped:
            result.skipped += 1

    return result


def export_one(
    folder: Path,
    dest: Path,
    relative_path: str,
    *,
    date_prefix: bool = False,
    captured_at: datetime | None = None,
) -> str | None:
    """Export a single already-selected file (used by `maybe_auto_export`).
    Returns an error message, or None on success (including "skipped,
    already identical at the destination" -- not an error).
    """
    folder = Path(folder)
    dest = Path(dest)
    _validate_dest(folder, dest)

    src = folder / relative_path
    if not src.exists():
        return f"{relative_path}: source file not found"

    dest.mkdir(parents=True, exist_ok=True)
    _copied, _skipped, error = _copy_file(
        src, dest, date_prefix=date_prefix, captured_at=captured_at
    )
    return error


def maybe_auto_export(folder: Path, photo: Photo) -> None:
    """Fire-and-forget auto-export hook (culling.py after a local select,
    downloads.py after an iCloud original lands): no-op unless the folder's
    settings have automatic mode + a destination configured, and this photo
    is actually selected with a file on disk. Never raises -- errors are
    logged, not propagated, so a misconfigured/unreachable export
    destination can never break culling itself.
    """
    try:
        folder = Path(folder)
        settings = folder_settings.load_settings(folder)
        if settings.export_mode != folder_settings.MODE_AUTOMATIC:
            return
        if not settings.export_destination:
            return
        if photo.status != Photo.STATUS_SELECTED:
            return
        if not (folder / photo.relative_path).exists():
            return

        error = export_one(
            folder,
            Path(settings.export_destination),
            photo.relative_path,
            date_prefix=settings.export_date_prefix,
            captured_at=photo.captured_at,
        )
        if error:
            logger.error("auto-export failed: %s", error)
    except Exception:
        logger.exception("auto-export failed for %s", getattr(photo, "relative_path", "<unknown>"))


# --- background "Export now" run (PLAN T25 UI) ------------------------------


@dataclass
class ExportProgress:
    """Poll-partial-facing wrapper (mirrors `scan.ScanProgress`'s shape) --
    not the PLAN-pinned `ExportResult` itself, see module docstring.
    """

    copied: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False


_export_lock = threading.Lock()
_current_export: ExportProgress | None = None


def start_background_export(
    folder: Path, dest: Path, *, date_prefix: bool = False
) -> ExportProgress:
    global _current_export
    with _export_lock:
        if _current_export is not None and not _current_export.finished:
            return _current_export
        progress = ExportProgress()
        _current_export = progress

    def _run() -> None:
        try:
            result = export_selected(folder, dest, date_prefix=date_prefix)
            progress.copied = result.copied
            progress.skipped = result.skipped
            progress.errors = result.errors
        except ValueError as exc:
            progress.errors.append(str(exc))
        finally:
            progress.finished = True
            connection.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return progress

"""File-move engine: status changes are implemented as atomic same-volume
renames. See SPEC.md §3/§4 -- the filesystem is the source of truth, the DB
row is updated only after the rename(s) succeed.
"""

import os
from pathlib import Path, PurePosixPath

from django.utils import timezone

from .models import Photo

VALID_STATUSES = {Photo.STATUS_OPTIONAL, Photo.STATUS_SELECTED, Photo.STATUS_REJECTED}
_STATUS_FOLDERS = {Photo.STATUS_SELECTED, Photo.STATUS_REJECTED}


def _status_from_relpath(relative_path: str) -> str:
    parts = PurePosixPath(relative_path).parts
    if parts and parts[0] in _STATUS_FOLDERS:
        return parts[0]
    return Photo.STATUS_OPTIONAL


def _source_rel_path(relative_path: str) -> PurePosixPath:
    p = PurePosixPath(relative_path)
    if p.parts and p.parts[0] in _STATUS_FOLDERS:
        return PurePosixPath(*p.parts[1:])
    return p


def dest_for(photo: Photo, new_status: str) -> PurePosixPath:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")
    source_rel = _source_rel_path(photo.relative_path)
    if new_status == Photo.STATUS_OPTIONAL:
        return source_rel
    return PurePosixPath(new_status) / source_rel


def _unique_path(path: Path) -> Path:
    """Never overwrite: append ' (n)' before the suffix until free."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def apply_status(folder: Path, photo: Photo, new_status: str) -> Photo:
    """Atomic os.rename (collision -> ' (n)' suffix), moves live-photo companion,
    updates relative_path/status/status_changed_at, saves. Never overwrites/deletes.
    """
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")

    current_status = _status_from_relpath(photo.relative_path)
    if new_status == current_status:
        return photo

    src_path = folder / photo.relative_path
    if not src_path.exists():
        raise FileNotFoundError(f"source file not found: {src_path}")

    dest_rel = dest_for(photo, new_status)
    dest_path = folder / dest_rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    final_dest = _unique_path(dest_path)
    os.rename(src_path, final_dest)

    if photo.live_photo_video_path:
        companion_src = folder / photo.live_photo_video_path
        if companion_src.exists():
            companion_dest = final_dest.parent / PurePosixPath(photo.live_photo_video_path).name
            companion_final = _unique_path(companion_dest)
            os.rename(companion_src, companion_final)
            photo.live_photo_video_path = companion_final.relative_to(folder).as_posix()
        # else: companion missing on disk -- move the image anyway, leave the
        # recorded path for the scanner to reconcile.

    photo.relative_path = final_dest.relative_to(folder).as_posix()
    photo.status = new_status
    photo.status_changed_at = timezone.now()
    photo.save()
    return photo

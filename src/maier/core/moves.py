"""File-move engine: status changes are implemented as atomic same-volume
renames. See SPEC.md §3/§4 -- the filesystem is the source of truth, the DB
row is updated only after the rename(s) succeed.

PLAN T24 (CTO decision, 2026-08-24): `selected/` is FLAT -- selecting a
photo moves it straight to `selected/{filename}`, never mirroring the
source substructure (two phones both dropping "IMG_0001.jpg" is now common;
collision suffixes handle the clash). `rejected/` is unchanged: discards
stay mirrored by source, so unflag-from-rejected is still derivable from
location alone. Because a flat select can no longer be reversed by just
stripping the "selected/" prefix, `Photo.original_path` records the
pre-select path (see `apply_status`) and `_resolve_source_rel` is the single
place that reconstructs "where did this come from" for both unflag and
reject-from-selected (SPEC/PLAN T24 rules 3 and 5).
"""

import os
from pathlib import Path, PurePosixPath

from django.utils import timezone

from . import remote_state
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


def _resolve_source_rel(photo: Photo) -> PurePosixPath:
    """The "source-relative" path a photo currently sitting in `selected/`
    or `rejected/` came from -- used both to unflag it and to mirror
    `rejected/` when rejecting directly out of (flat) `selected/`
    (PLAN T24 rules 3 and 5). Resolution order, first match wins:

    (a) `photo.original_path`, if recorded -- the exact pre-move path.
    (b) legacy mirrored layout: the current path already has a substructure
        under the status prefix (e.g. a pre-existing `selected/a/x.jpg`
        dropped straight into the folder before the app ever touched it, or
        one moved externally) -- strip the prefix, same as before T24.
    (c) last resort, only reachable for a flat select with no recorded
        `original_path` (i.e. `.maier/` cache loss wiped the DB row that
        carried it): `{provenance}/{filename}` if provenance is known, else
        the bare filename at the root. This is an accepted degradation --
        once a flat select's DB row is gone, its origin subfolder can no
        longer be recovered from location alone (SPEC hard rule 1 still
        holds: status itself is still fully derivable, just not the
        pre-select path for a *flat* select).
    """
    if photo.original_path:
        return PurePosixPath(photo.original_path)

    current = PurePosixPath(photo.relative_path)
    parts = current.parts
    if parts and parts[0] in _STATUS_FOLDERS:
        rest = parts[1:]
        if len(rest) > 1:
            return PurePosixPath(*rest)
        filename = current.name
        if photo.provenance:
            return PurePosixPath(photo.provenance) / filename
        return PurePosixPath(filename)
    return current


def dest_for(photo: Photo, new_status: str) -> PurePosixPath:
    if new_status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")

    if new_status == Photo.STATUS_SELECTED:
        # Flat: no provenance subfolder, no mirrored substructure -- just
        # the filename directly under selected/ (PLAN T24 rule 1).
        filename = PurePosixPath(photo.relative_path).name
        return PurePosixPath(Photo.STATUS_SELECTED) / filename

    if new_status == Photo.STATUS_OPTIONAL:
        return _resolve_source_rel(photo)

    # new_status == "rejected"
    current_status = _status_from_relpath(photo.relative_path)
    if current_status == Photo.STATUS_SELECTED:
        # Moving out of flat selected/ -- mirror the ORIGINAL structure,
        # not the flat one (PLAN T24 rule 5).
        return PurePosixPath(Photo.STATUS_REJECTED) / _resolve_source_rel(photo)
    # From optional (root, still mirroring the source substructure as
    # always) or already-rejected (no-op, filtered out by the caller).
    source_rel = _source_rel_path(photo.relative_path)
    return PurePosixPath(Photo.STATUS_REJECTED) / source_rel


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

    # PLAN T24 rule 3: record where a photo moved FROM the moment it leaves
    # a non-status location, since a flat select can't be reversed by
    # location alone. Set once, never overwritten (rule 4 -- stable round
    # trips even after multiple select/unflag/reject cycles).
    if current_status == Photo.STATUS_OPTIONAL and not photo.original_path:
        photo.original_path = photo.relative_path

    dest_rel = dest_for(photo, new_status)
    dest_path = folder / dest_rel
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    final_dest = _unique_path(dest_path)
    os.rename(src_path, final_dest)

    if photo.live_photo_video_path:
        companion_src = folder / photo.live_photo_video_path
        if companion_src.exists():
            old_companion_rel = photo.live_photo_video_path
            companion_dest = final_dest.parent / PurePosixPath(old_companion_rel).name
            companion_final = _unique_path(companion_dest)
            os.rename(companion_src, companion_final)
            new_companion_rel = companion_final.relative_to(folder).as_posix()
            photo.live_photo_video_path = new_companion_rel
            # The companion has its own Photo row (scan indexes the .mov as
            # a standalone video). Keep it in step with the file so it stays
            # hidden behind the image instead of transiently reappearing in
            # the grid with a stale path until the next scan.
            from .scan import _status_and_provenance

            comp_status, comp_provenance = _status_and_provenance(Path(new_companion_rel))
            Photo.objects.filter(relative_path=old_companion_rel).exclude(pk=photo.pk).update(
                relative_path=new_companion_rel,
                status=comp_status,
                provenance=comp_provenance,
                status_changed_at=timezone.now(),
            )
        # else: companion missing on disk -- move the image anyway, leave the
        # recorded path for the scanner to reconcile.

    photo.relative_path = final_dest.relative_to(folder).as_posix()
    photo.status = new_status
    photo.status_changed_at = timezone.now()
    photo.save()
    return photo


def flatten_selected(folder: Path) -> int:
    """One-time-per-file convergence of a legacy mirrored `selected/` tree
    to the flat layout (PLAN T24 CTO follow-up, 2026-08-24): every file
    found deeper than one level under `selected/` (e.g.
    `selected/apple-luis/IMG.jpg`) is moved to flat `selected/{filename}`
    (collision-suffixed via `_unique_path`, same rule as every other move).

    Filesystem first, then DB bookkeeping, mirroring `apply_status`:
    - a Photo row at the old path has its `relative_path` updated to the
      new flat path, and `original_path` set to the pre-flatten
      source-relative path (old path with the `selected/` prefix stripped)
      if `original_path` is still empty -- preserves unflag-restore for
      photos selected before this migration existed.
    - any row's `live_photo_video_path` pointing at a moved file is
      rewritten too (keeps the video-migration and image-migration
      independent of walk order).
    - every account's `remote_state` `downloaded` map has matching values
      rewritten, since those are relative paths into `selected/` as well.

    Files with no DB row are still moved on disk (filesystem is the source
    of truth) -- the next scan indexes them at the new path; its own
    (size, mtime)/hash reconciliation re-links any row that might exist
    elsewhere.

    Idempotent: called at the start of every `scan()` (PLAN T24), so the
    flat layout is a converging invariant, not a one-off migration. Returns
    the number of files moved (0 on a no-op run).
    """
    folder = Path(folder)
    selected_root = folder / Photo.STATUS_SELECTED
    if not selected_root.is_dir():
        return 0

    moved = 0
    accounts = remote_state.list_accounts(folder)
    state_cache: dict[str, remote_state.AccountState] = {}
    dirty_accounts: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(selected_root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirpath_path = Path(dirpath)
        if dirpath_path == selected_root:
            continue  # already flat, nothing to do at this level

        for fname in filenames:
            if fname.startswith("."):
                continue
            src = dirpath_path / fname
            old_rel = src.relative_to(folder).as_posix()
            source_rel = src.relative_to(selected_root).as_posix()

            dest = _unique_path(selected_root / fname)
            os.rename(src, dest)
            new_rel = dest.relative_to(folder).as_posix()
            moved += 1

            row = Photo.objects.filter(relative_path=old_rel).first()
            if row is not None:
                update_fields = {"relative_path": new_rel}
                if not row.original_path:
                    update_fields["original_path"] = source_rel
                Photo.objects.filter(pk=row.pk).update(**update_fields)

            Photo.objects.filter(live_photo_video_path=old_rel).update(
                live_photo_video_path=new_rel
            )

            for account in accounts:
                state = state_cache.get(account)
                if state is None:
                    state = remote_state.load_state(folder, account)
                    state_cache[account] = state
                for remote_id, path in state.downloaded.items():
                    if path == old_rel:
                        state.downloaded[remote_id] = new_rel
                        dirty_accounts.add(account)

    for account in dirty_accounts:
        remote_state.save_state(folder, state_cache[account])

    # Empty dirs only -- rmdir raises (and is swallowed) if anything is
    # still inside, so this never touches a non-empty directory or a file.
    for dirpath, _dirnames, _filenames in os.walk(selected_root, topdown=False):
        dirpath_path = Path(dirpath)
        if dirpath_path == selected_root:
            continue
        try:
            dirpath_path.rmdir()
        except OSError:
            pass

    return moved

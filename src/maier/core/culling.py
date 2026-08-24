"""Status-change dispatcher (SPEC §18, PLAN T17): routes every cull action
through the right engine depending on the photo's source. Local photos keep
going through `phaseb.apply_status_to_group` (exact-dupe-aware group cull,
unchanged). Remote (iCloud) photos never touch the filesystem or network
synchronously (SPEC §18 rule 1/2 -- browsing/rejecting/undeciding never
calls iCloud): reject/undecide only update the per-account durable decision
state; select flips the DB status immediately for instant UI feedback and
enqueues an async original download (`core/downloads.py`) without blocking
the request. Once that download lands, the row is `source="local"` and every
later call to `apply_status_any` for it flows through the local branch
automatically -- no special-casing needed here for "already downloaded".
"""

from __future__ import annotations

from pathlib import Path

from django.utils import timezone

from . import downloads, moves, phaseb, remote_state
from .models import Photo


class AccountSessionExpired(Exception):
    """Carries `.account`. Defined for the public interface (PLAN T17) but
    NOT currently raised by `apply_status_any` (flagged design choice): a
    synchronous session check on every "select" would mean either blocking
    the request on a network round-trip (`ICloudClient.from_session`
    constructs a `PyiCloudService`, which can hit the network) or lying
    about whether the session is actually valid. Selecting a remote photo
    is designed to never block on the download -- session problems surface
    asynchronously instead, as a `download-pending` row plus a
    "needs re-authentication" entry in the worker's progress errors (T18
    polls/display). Kept here so callers (views, or a future synchronous
    entry point) have a single exception type to catch.
    """

    def __init__(self, account: str) -> None:
        super().__init__(f"iCloud session expired for {account}; re-authentication needed")
        self.account = account


def apply_status_any(folder: Path, photo: Photo, new_status: str) -> Photo:
    if new_status not in moves.VALID_STATUSES:
        raise ValueError(f"invalid status: {new_status!r}")

    if photo.source != Photo.SOURCE_ICLOUD:
        updated = phaseb.apply_status_to_group(folder, photo, new_status)
        if updated.status == Photo.STATUS_SELECTED:
            # PLAN T25: fire-and-forget auto-export hook -- no-op unless the
            # folder is configured for automatic export (never raises).
            from . import export

            export.maybe_auto_export(folder, updated)
        return updated

    return _apply_remote_status(Path(folder), photo, new_status)


def _apply_remote_status(folder: Path, photo: Photo, new_status: str) -> Photo:
    if new_status == Photo.STATUS_SELECTED:
        # Immediate UI feedback; the original lands asynchronously (SPEC
        # §18 rule 2). No file I/O, no network call on this thread.
        photo.status = Photo.STATUS_SELECTED
        photo.status_changed_at = timezone.now()
        photo.save(update_fields=["status", "status_changed_at"])
        downloads.enqueue_original(folder, photo)
        return photo

    # rejected / optional: durable per-account decision state only -- no
    # file I/O, no network (the photo has no local file to move yet).
    state = remote_state.load_state(folder, photo.account)
    if new_status == Photo.STATUS_REJECTED:
        state.decisions[photo.remote_id] = remote_state.DECISION_REJECTED
    else:
        # "optional" is pull.py's own default when no decision is recorded
        # (see `_process_asset`: `decisions.get(remote_id, DECISION_OPTIONAL)`)
        # -- deleting the key is equivalent to writing "optional" and keeps
        # the decisions map minimal (flagged: brief allowed either choice).
        state.decisions.pop(photo.remote_id, None)
    remote_state.save_state(folder, state)

    photo.status = new_status
    photo.status_changed_at = timezone.now()
    photo.save(update_fields=["status", "status_changed_at"])
    return photo

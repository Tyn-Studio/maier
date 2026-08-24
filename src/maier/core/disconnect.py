"""Disconnect an iCloud account (SPEC §18, PLAN M5 T21).

"Disconnect" removes everything about an account that is either secret
(session tokens) or a rebuildable cache (remote DB rows, cached previews) --
it deliberately KEEPS the two things that are durable/user-owned:

- `{folder}/icloud-state/{slug}.json` -- untouched. Re-attaching the same
  account later restores its rejections (and `state.downloaded` still
  protects already-downloaded remote_ids from resurrecting as a phantom
  `source="icloud"` row on the next pull -- see `pull.py::_process_asset`).
- `{folder}/selected/{slug}/...` -- untouched. Anything already downloaded
  is an ordinary local file (`source="local"`) by the time it lands there;
  disconnecting an account must never touch local files (CLAUDE.md hard
  rule 2/9).

Only `Photo` rows with `source="icloud"` for this account are removed --
never filter on `account` alone: a downloaded-and-converted row is
`source="local"` but still carries the original `account`/`remote_id`
(`downloads.py::_convert_to_local` leaves them in place so accounts.html's
"total items" count stays stable across that transition -- PLAN T18 log)
and MUST survive disconnect.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import icloud
from .models import Photo
from .remote_state import account_slug


@dataclass
class DisconnectResult:
    rows_removed: int
    previews_removed: int


class PullInFlight(Exception):
    """Raised by `disconnect_account` when a pull for this account is still
    running. Deliberately NOT a hard lock/kill: `pull_account` (pull.py) runs
    in a daemon thread this module never touches (brief: don't touch
    pull.py), so in-flight work can still re-create rows/previews after this
    check passes but before/while a disconnect races it. This is a cheap,
    good-enough guard for the common case (user clicks disconnect while a
    pull banner is visible) -- the message tells them to retry once it's
    done, and disconnecting again afterward is always safe (idempotent).
    """


def pull_in_flight(account: str) -> bool:
    """Read-only peek at `pull.py`'s module-level `_current_pulls` dict --
    same isolated-seam pattern as `views.py`'s `_pull_progress_for`/
    `_in_flight_scan_progress` (pull.py exposes no public accessor for "is
    this account pulling right now"). Imported lazily so this module has no
    load-time dependency on pull.py (which this task does not own/touch).
    """
    from . import pull as pull_module

    progress = pull_module._current_pulls.get(account)
    return progress is not None and not progress.finished


def _previews_dir(folder: Path) -> Path:
    return Path(folder) / ".maier" / "previews"


def disconnect_account(folder: Path, account: str) -> DisconnectResult:
    """Idempotent: an unknown or already-disconnected account is a no-op
    returning zeros -- never raises for that case. Raises `PullInFlight`
    (not idempotent-safe to ignore, since it signals a live race) when a
    pull for this account is currently running; callers should surface that
    as an error rather than deleting anything.

    Order: session tokens first (a stale token is harmless to remove even
    if the rest below fails partway), then remote DB rows, then cached
    previews. `icloud-state/{slug}.json` and `selected/{slug}/` are never
    touched.
    """
    folder = Path(folder)
    if pull_in_flight(account):
        raise PullInFlight(
            f"A pull for {account} is currently running -- wait for it to "
            "finish, then disconnect again."
        )

    icloud.ICloudClient.forget_session(account)

    remote_rows = Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=account)
    rows_removed = remote_rows.count()
    remote_rows.delete()

    previews_removed = 0
    previews_dir = _previews_dir(folder)
    if previews_dir.is_dir():
        slug = account_slug(account)
        for path in previews_dir.glob(f"icloud-{slug}-*.jpg"):
            try:
                path.unlink()
                previews_removed += 1
            except OSError:
                pass  # cache cleanup is best-effort; a stray leftover is harmless

    return DisconnectResult(rows_removed=rows_removed, previews_removed=previews_removed)

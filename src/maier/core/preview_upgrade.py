"""On-demand preview upgrades for remote (iCloud) photos (PLAN T22 + T34).

Two tiers share this machinery, keyed independently so a thumb and a medium
fetch for the same photo can be in flight at once:

  - `enqueue_medium` (T22, CTO-approved design, 2026-08-24): review-screen
    quality. thumb-first instant render -> background medium fetch ->
    swap-in, plus a small filmstrip-neighbour prefetch. Data cost is ~1MB
    per reviewed photo, fetched at most once, cached forever.
  - `enqueue_thumb` (T34, CTO requirement 2026-08-25: "if I filter by date,
    the previews should load almost instantly"): grid-cell quality for
    photos the whole-library backfill (`core/pull.py`) hasn't reached yet.
    `core/views.py`'s `grid()` calls this for visible-page remote rows
    whose bulk-synced thumb is still missing, so filtering straight to a
    not-yet-backfilled date range doesn't leave a page of gray placeholders
    waiting on the sweep's own ordering.

The bulk sync (`core/pull.py`) only ever fetches the small "thumb" tier
(~60KB) so the grid is fully browsable without hours of downloading at
real-library scale (PLAN 2026-08-24 "Round 2"/"Round 3"). This module fills
the gap for photos actually on screen right now: `core/views.py`'s
`review()` calls `enqueue_medium` for the current photo and its nearest
filmstrip neighbours; `grid()` calls `enqueue_thumb` for visible rows still
missing their thumb. `core/previews.py`'s `best_remote_preview` (medium) and
`remote_preview_dest` (thumb) read back whatever has landed so far.

Mirrors `core/downloads.py`'s worker shape (a `_client_for_account` seam so
tests never touch real `pyicloud`, one client cached per account) but is
deliberately simpler: this is a latency optimization, not durable state, so
the in-memory "pending" set is fine to lose on a restart -- a lost enqueue
just gets re-issued the next time the photo is on screen (grid page reload
or review open). Concurrency is capped low (2-3 workers, shared by both
tiers) so this never competes with the bulk pull's own 8-worker preview
backlog. Both tiers fetch through this module's OWN `_client_for_account`
seam (its own per-account `ICloudClient`/pyicloud session, cached
separately from `pull.py`'s and `downloads.py`'s) so an on-demand fetch
never contends with a running pull's own enumeration/backlog session --
album lookups are the slow, tarpit-bounded path (PLAN v0.1.3: process-wide
60s socket timeout, uncontended lookups probed at ~1-3s), and sharing a
session would mean queuing behind whichever request got there first.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import previews as previews_module
from .models import Photo

logger = logging.getLogger("maier.preview_upgrade")

MAX_WORKERS = 3


def _client_for_account(account: str):
    """Seam for tests (flagged, mirrors `downloads._client_for_account`):
    the single place that resolves an account email to a live
    `ICloudClient`. Imported lazily so this module doesn't force a
    `pyicloud` import at Django-startup time. Returns `None` when the
    stored session is missing/expired -- callers drop the item silently
    (SPEC §18 UI surfaces re-auth elsewhere; retry happens on the next
    `enqueue_medium` call for the same photo).
    """
    from .icloud import ICloudClient

    return ICloudClient.from_session(account)


_pending_lock = threading.Lock()
# (account, remote_id, tier) -- single-flight per tier, so a thumb and a
# medium fetch for the same photo can be in flight at the same time (T34).
_pending: set[tuple[str, str, str]] = set()

_clients_lock = threading.Lock()
_clients: dict[str, object] = {}  # account -> client; only successes are cached

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=MAX_WORKERS, thread_name_prefix="preview-upgrade"
            )
        return _executor


def _resolve_client(account: str):
    with _clients_lock:
        cached = _clients.get(account)
    if cached is not None:
        return cached
    client = _client_for_account(account)
    if client is not None:
        with _clients_lock:
            _clients[account] = client
    return client


def _fetch_one(
    folder: Path, account: str, remote_id: str, tier: str, version: str, dest: Path
) -> None:
    try:
        client = _resolve_client(account)
        if client is None:
            logger.warning(
                "preview_upgrade: no session for account %s, dropping %s (%s)",
                account,
                remote_id,
                tier,
            )
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download(remote_id, version, dest)
    except Exception:
        logger.exception("preview_upgrade: %s fetch failed for %s", tier, remote_id)
    finally:
        with _pending_lock:
            _pending.discard((account, remote_id, tier))


def _enqueue(folder: Path, photo: Photo, tier: str, dest: Path, version: str) -> None:
    key = (photo.account, photo.remote_id, tier)
    with _pending_lock:
        if key in _pending:
            return
        _pending.add(key)

    _get_executor().submit(_fetch_one, folder, photo.account, photo.remote_id, tier, version, dest)


def enqueue_medium(folder: Path, photo: Photo) -> None:
    """No-op if the photo isn't a remote row, its medium is already cached,
    or a fetch for it is already in flight. Never blocks on the network --
    it only ensures a background task is submitted; the actual download
    happens on a worker thread.
    """
    if photo.source != Photo.SOURCE_ICLOUD or not photo.remote_id:
        return

    folder = Path(folder)
    dest = previews_module.remote_medium_dest(folder, photo.account, photo.remote_id)
    if dest.exists():
        return

    # Videos only expose an MP4 for "medium" -- the JPEG poster variant
    # is "medium_image" (same rule pull.py uses for its thumb tier).
    version = "medium_image" if photo.media_type == Photo.MEDIA_VIDEO else "medium"
    _enqueue(folder, photo, "medium", dest, version)


def enqueue_thumb(folder: Path, photo: Photo) -> None:
    """T34: on-demand fetch of the bulk-sync "thumb" tier for a remote row
    the backfill hasn't reached yet -- called from `core/views.py`'s
    `grid()` for visible-page rows whose `previews.remote_preview_dest`
    doesn't exist. Same no-op/single-flight contract as `enqueue_medium`,
    keyed separately (tier="thumb") so it never collides with a concurrent
    medium fetch for the same photo. Same version rule as `core/pull.py`:
    videos fetch the JPEG poster ("thumb_image"), not the "thumb" video
    rendition.
    """
    if photo.source != Photo.SOURCE_ICLOUD or not photo.remote_id:
        return

    folder = Path(folder)
    dest = previews_module.remote_preview_dest(folder, photo.account, photo.remote_id)
    if dest.exists():
        return

    version = "thumb_image" if photo.media_type == Photo.MEDIA_VIDEO else "thumb"
    _enqueue(folder, photo, "thumb", dest, version)

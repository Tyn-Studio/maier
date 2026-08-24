"""On-demand "medium" preview upgrade for remote (iCloud) photos opened in
the review screen (PLAN T22, CTO-approved design, 2026-08-24):

    thumb-first instant render -> background medium fetch -> swap-in,
    plus a small filmstrip-neighbour prefetch. Data cost is ~1MB per
    reviewed photo, fetched at most once, cached forever.

The bulk sync (`core/pull.py`) only ever fetches the small "thumb" tier
(~60KB) so the grid is fully browsable without hours of downloading at
real-library scale (PLAN 2026-08-24 "Round 2"/"Round 3"). This module fills
the gap for the one photo (plus a couple of neighbours) actually being
looked at closely: `core/views.py`'s `review()` calls `enqueue_medium` for
the current photo and its nearest filmstrip neighbours; `core/previews.py`'s
`best_remote_preview` reads back whatever has landed so far.

Mirrors `core/downloads.py`'s worker shape (a `_client_for_account` seam so
tests never touch real `pyicloud`, one client cached per account) but is
deliberately simpler: this is a latency optimization, not durable state, so
the in-memory "pending" set is fine to lose on a restart -- a lost enqueue
just gets re-issued the next time the photo is opened in review. Concurrency
is capped low (2-3 workers) so this never competes with the bulk pull's own
8-worker preview backlog.
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
_pending: set[tuple[str, str]] = set()  # (account, remote_id) -- single-flight

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


def _fetch_one(folder: Path, account: str, remote_id: str, media_type: str, dest: Path) -> None:
    try:
        client = _resolve_client(account)
        if client is None:
            logger.warning(
                "preview_upgrade: no session for account %s, dropping %s", account, remote_id
            )
            return
        # Videos only expose an MP4 for "medium" -- the JPEG poster variant
        # is "medium_image" (same rule pull.py uses for its thumb tier).
        version = "medium_image" if media_type == Photo.MEDIA_VIDEO else "medium"
        dest.parent.mkdir(parents=True, exist_ok=True)
        client.download(remote_id, version, dest)
    except Exception:
        logger.exception("preview_upgrade: medium fetch failed for %s", remote_id)
    finally:
        with _pending_lock:
            _pending.discard((account, remote_id))


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

    key = (photo.account, photo.remote_id)
    with _pending_lock:
        if key in _pending:
            return
        _pending.add(key)

    _get_executor().submit(
        _fetch_one, folder, photo.account, photo.remote_id, photo.media_type, dest
    )

"""Background original-download worker (SPEC §18 rules 2-3, PLAN T17):
selecting a remote (iCloud) photo enqueues its original for download into
`selected/{account-slug}/...`; once it lands the Photo row converts to an
ordinary local row (`source="local"`) and every further status change flows
through the normal move engine (`core/moves.py` via `core/culling.py`).

The queue is DB-derived, not an in-memory list (crash-safe, matches
scan.py/pull.py's own background-thread pattern): a "pending" item is any
`Photo(source="icloud", status="selected")` row whose `remote_id` isn't
already recorded in that account's `state.downloaded` map. `enqueue_original`
never blocks on the network -- it only ensures a single daemon worker thread
is running; the actual download happens on that thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from django.db import connection

from . import moves, remote_state
from .models import Photo


@dataclass
class DownloadProgress:
    pending: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)


def _client_for_account(account: str):
    """Seam for tests (flagged, per brief): the single place that resolves
    an account email to a live `ICloudClient`. Imported lazily so this
    module doesn't force a `pyicloud` import at Django-startup time for
    every request -- mirrors `pull.py`'s TYPE_CHECKING-only import of
    `core.icloud`. Returns `None` when the stored session is missing/expired
    (needs re-authentication) -- callers never raise for this, they just
    skip the account's items for this worker run (T18 surfaces it).
    """
    from .icloud import ICloudClient

    return ICloudClient.from_session(account)


def pending_remote_ids(account: str) -> set[str]:
    """remote_ids for this account that are selected but not yet
    downloaded -- for T18's UI "download pending" indicator. A row that
    finished downloading is already `source="local"`, so no separate
    `state.downloaded` check is needed here (unlike the worker's own
    queue query, which also treats a `state.downloaded` entry as "already
    done" even if the row conversion somehow hasn't happened yet -- see
    `_pending_rows`).
    """
    return set(
        Photo.objects.filter(
            source=Photo.SOURCE_ICLOUD, status=Photo.STATUS_SELECTED, account=account
        )
        .exclude(remote_id=None)
        .values_list("remote_id", flat=True)
    )


def _pending_rows(folder: Path) -> list[Photo]:
    rows = list(
        Photo.objects.filter(source=Photo.SOURCE_ICLOUD, status=Photo.STATUS_SELECTED)
        .exclude(remote_id=None)
        .order_by("account", "pk")
    )
    if not rows:
        return rows

    state_by_account: dict[str, remote_state.AccountState] = {}
    filtered = []
    for photo in rows:
        state = state_by_account.get(photo.account)
        if state is None:
            state = remote_state.load_state(folder, photo.account)
            state_by_account[photo.account] = state
        if photo.remote_id in state.downloaded:
            continue  # already landed locally; row conversion just hasn't been re-read yet
        filtered.append(photo)
    return filtered


def _dest_for(folder: Path, photo: Photo, slug: str) -> Path:
    filename = PurePosixPath(photo.remote_filename).name if photo.remote_filename else ""
    if not filename:
        filename = f"{photo.remote_id}.jpg"
    dest_dir = folder / "selected" / slug
    # `moves._unique_path` is a private helper (flagged, per brief): reused
    # here rather than duplicated so the collision-suffix rule (SPEC §3/§4:
    # " (n)" before the extension, never overwrite) stays a single
    # implementation shared by ordinary moves and remote downloads.
    return moves._unique_path(dest_dir / filename)


def _convert_to_local(photo: Photo, dest: Path, folder: Path, slug: str) -> None:
    st = dest.stat()
    Photo.objects.filter(pk=photo.pk).update(
        source=Photo.SOURCE_LOCAL,
        relative_path=dest.relative_to(folder).as_posix(),
        provenance=slug,
        file_size=st.st_size,
        file_mtime=st.st_mtime,
        # captured_at is left untouched -- the API date is authoritative,
        # not a fallback-chain guess (see pull.py's own comment). sha256
        # stays NULL: it's already NULL on every remote row, and Phase B
        # (excluded only for source="icloud") picks the now-local row up
        # on the next background hashing run.
    )


def _download_one(folder: Path, client, photo: Photo, progress: DownloadProgress) -> None:
    slug = remote_state.account_slug(photo.account)
    dest = _dest_for(folder, photo, slug)

    try:
        client.download(photo.remote_id, "original", dest)
    except Exception as exc:  # per-item errors never abort the worker run
        progress.errors.append(f"{photo.remote_id}: {exc}")
        return

    try:
        _convert_to_local(photo, dest, folder, slug)
        state = remote_state.load_state(folder, photo.account)
        state.downloaded[photo.remote_id] = dest.relative_to(folder).as_posix()
        state.decisions.pop(photo.remote_id, None)
        remote_state.save_state(folder, state)
        progress.done += 1
    except Exception as exc:
        progress.errors.append(f"{photo.remote_id}: post-download bookkeeping failed: {exc}")


def _worker_loop(folder: Path, progress: DownloadProgress) -> None:
    """Single-pass-per-item drain: each (account, remote_id) is attempted at
    most once per worker run (tracked in `attempted`), so a persistently
    failing item can't spin the loop forever, but items enqueued *during*
    this run (a fresh `_pending_rows` query each iteration) still get
    picked up without needing a second `enqueue_original` call. A row that
    fails stays selected+remote in the DB; the next `enqueue_original` call
    (new selection, or a retry-triggering caller) starts a fresh run that
    retries it (flagged design choice, per brief: no in-run retry/backoff).
    """
    attempted: set[tuple[str, str]] = set()
    errored_accounts: set[str] = set()
    # One client per account per run: from_session constructs a fresh
    # PyiCloudService (network round-trip), so resolving it per photo would
    # cost a session validation for every pending item.
    clients: dict[str, object] = {}
    try:
        while True:
            rows = [
                p
                for p in _pending_rows(folder)
                if (p.account, p.remote_id) not in attempted and p.account not in errored_accounts
            ]
            if not rows:
                return
            progress.pending = len(rows)
            for photo in rows:
                client = clients.get(photo.account)
                if client is None and photo.account not in clients:
                    client = _client_for_account(photo.account)
                    clients[photo.account] = client
                if client is None:
                    if photo.account not in errored_accounts:
                        progress.errors.append(f"account {photo.account} needs re-authentication")
                        errored_accounts.add(photo.account)
                    continue
                attempted.add((photo.account, photo.remote_id))
                _download_one(folder, client, photo, progress)
    finally:
        connection.close()


_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
# Test/introspection seam (flagged, not part of the PLAN interface): the
# most recent worker run's progress, so tests can assert on errors/done
# counts without `start_worker` needing to return a handle (its interface
# is `-> None`, unlike scan.py/pull.py's progress-returning starters).
_last_progress: DownloadProgress | None = None


def start_worker(folder: Path) -> None:
    global _worker_thread, _last_progress
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        progress = DownloadProgress()
        _last_progress = progress
        thread = threading.Thread(target=_run_worker, args=(folder, progress), daemon=True)
        _worker_thread = thread
        thread.start()


def _run_worker(folder: Path, progress: DownloadProgress) -> None:
    try:
        _worker_loop(folder, progress)
    finally:
        connection.close()


def enqueue_original(folder: Path, photo: Photo) -> None:
    start_worker(Path(folder))

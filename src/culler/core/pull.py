"""Incremental iCloud pull pipeline (SPEC §18, PLAN T16): enumerate the
account's full remote metadata, upsert `Photo` rows (source="icloud") for
remote_ids not yet known, prefetch medium previews (with a repair pass for
previously-failed ones), then record the last-pull watermark. Incrementality
is keyed on remote_id, NOT capture date -- old photos added to iCloud later
(device syncs, imports) must still be picked up. Mirrors `scan.py`'s
background-thread pattern (daemon thread, `connection.close()` in `finally`)
but is single-flight *per account* rather than globally, since multiple
accounts may legitimately pull concurrently.

Deliberately duck-typed against `core/icloud.py`'s `ICloudClient` /
`RemoteAsset` interface (PLAN "M5 interfaces") without importing that module
at runtime -- it is being built by a concurrent agent in this same
milestone. Only imported under `TYPE_CHECKING` for hints; tests exercise
this module against fakes implementing the same duck-typed surface.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.db import connection

from . import previews as previews_module
from . import remote_state
from .models import Photo

if TYPE_CHECKING:
    from .icloud import ICloudClient, RemoteAsset


@dataclass
class PullProgress:
    account: str = ""
    total: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False


def _process_asset(
    folder: Path,
    client: ICloudClient,
    state: remote_state.AccountState,
    asset: RemoteAsset,
) -> None:
    # The original for this remote_id already landed locally (T17 select
    # workflow); its Photo row is source="local" now -- don't resurrect a
    # phantom source="icloud" row for the same item.
    if asset.remote_id in state.downloaded:
        return

    decision = state.decisions.get(asset.remote_id, remote_state.DECISION_OPTIONAL)
    status = (
        Photo.STATUS_REJECTED
        if decision == remote_state.DECISION_REJECTED
        else Photo.STATUS_OPTIONAL
    )

    Photo.objects.update_or_create(
        account=client.account,
        remote_id=asset.remote_id,
        defaults={
            "source": Photo.SOURCE_ICLOUD,
            # Sentinel, never a real path (SPEC §18 / PLAN interface):
            # keeps the (account, remote_id) unique constraint meaningful
            # while `relative_path` stays globally unique too.
            "relative_path": f"@icloud/{client.account}/{asset.remote_id}",
            "captured_at": asset.captured_at,
            # API dates are authoritative -- not a fallback-chain guess.
            "captured_at_source": "exif",
            "media_type": asset.media_type,
            "file_size": asset.size,
            "file_mtime": 0.0,
            # Deliberate choice (flagged): reuse `provenance` for the
            # account email rather than adding a separate remote-provenance
            # concept, so the existing provenance filter dropdown (grid,
            # queries.distinct_provenances) works for iCloud accounts with
            # no further UI changes.
            "provenance": client.account,
            "status": status,
        },
    )

    dest = previews_module.remote_preview_dest(folder, client.account, asset.remote_id)
    if not dest.exists():
        client.download(asset.remote_id, "medium", dest)


def pull_account(folder: Path, client: ICloudClient, progress: PullProgress) -> None:
    folder = Path(folder)
    progress.account = client.account
    try:
        state = remote_state.load_state(folder, client.account)

        known_ids = set(
            Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=client.account)
            .exclude(remote_id=None)
            .values_list("remote_id", flat=True)
        )

        assets: list[Any] = []
        iteration_failed = False
        try:
            # Full metadata enumeration on every pull, deliberately NOT
            # filtered by the cursor: iCloud libraries gain OLD photos later
            # (device syncs, imports from other libraries), and a
            # capture-date filter would hide those forever. The web API
            # enumerates all metadata regardless (no server-side date
            # filter), so this costs nothing extra. "Incremental" per SPEC
            # §18 = skip already-known remote_ids and re-downloads.
            for asset in client.list_assets(since=None):
                assets.append(asset)
        except Exception as exc:
            progress.errors.append(f"list_assets: {exc}")
            iteration_failed = True

        new_assets = [
            a
            for a in assets
            if a.remote_id not in known_ids and a.remote_id not in state.downloaded
        ]

        # Preview repair: rows from earlier pulls whose medium preview never
        # landed (failed download, wiped .culler/ cache) get retried on
        # every pull rather than staying placeholders forever.
        repair_ids = [
            rid
            for rid in sorted(known_ids)
            if not previews_module.remote_preview_dest(folder, client.account, rid).exists()
        ]

        # Materialized up front (SPEC/PLAN: "metadata is smallish") so
        # total/done are meaningful even though downloads happen per item.
        progress.total = len(new_assets) + len(repair_ids)

        max_captured_at: datetime | None = state.cursor

        for asset in new_assets:
            try:
                _process_asset(folder, client, state, asset)
                if max_captured_at is None or asset.captured_at > max_captured_at:
                    max_captured_at = asset.captured_at
            except Exception as exc:  # per-asset errors never abort the pull
                progress.errors.append(f"{asset.remote_id}: {exc}")
            finally:
                progress.done += 1

        for rid in repair_ids:
            try:
                dest = previews_module.remote_preview_dest(folder, client.account, rid)
                client.download(rid, "medium", dest)
            except Exception as exc:
                progress.errors.append(f"{rid}: preview refetch failed: {exc}")
            finally:
                progress.done += 1

        # The cursor is an informational last-pull watermark (max capture
        # date fully processed) -- it no longer gates listing. Only advance
        # it after an uninterrupted iteration; per-asset failures don't
        # count, only the listing iterator itself raising mid-stream does.
        if not iteration_failed:
            state.cursor = max_captured_at

        remote_state.save_state(folder, state)
    finally:
        progress.finished = True


_pull_lock = threading.Lock()
_current_pulls: dict[str, PullProgress] = {}


def start_background_pull(folder: Path, client: ICloudClient) -> PullProgress:
    """Daemon-thread pull, single-flight per account (a second call for an
    account already pulling returns the in-flight `PullProgress`; a
    different account starts its own thread independently).
    """
    with _pull_lock:
        existing = _current_pulls.get(client.account)
        if existing is not None and not existing.finished:
            return existing
        progress = PullProgress(account=client.account)
        _current_pulls[client.account] = progress

    def _run() -> None:
        try:
            pull_account(folder, client, progress)
        finally:
            connection.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return progress

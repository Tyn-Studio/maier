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

T29: at large-library scale, the *preview fetch* (backlog repair pass and
newly-discovered assets alike) is scoped to the user's working date range
(`core/folder_settings.py`) -- metadata enumeration/row-upsert above stays
whole-library regardless, per SPEC ("cheap; keeps timeline + dupe detection
complete"). An unset range (setup wizard never completed, or pre-M6 tests
that never touch `folder_settings`) disables the filter entirely -- current
behavior is preserved.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import connection

from . import folder_settings, remote_state
from . import previews as previews_module
from .models import Photo
from .queries import _day_end, _day_start  # no import cycle: queries never imports pull

if TYPE_CHECKING:
    from .icloud import ICloudClient, RemoteAsset


def _is_jpeg(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(2) == b"\xff\xd8"
    except OSError:
        return False


@dataclass
class PullProgress:
    account: str = ""
    scanned: int = 0  # assets enumerated this pull (known + new alike)
    total: int = 0  # preview fetches queued so far (backlog + new items)
    done: int = 0  # preview fetches completed
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
            # Deliberate choice (flagged, PLAN T17): `provenance` is the
            # account SLUG, not the raw email. T16 originally used the raw
            # email here; T17 changed it so a downloaded original's local
            # row (whose provenance/directory MUST be the filesystem-safe
            # slug per SPEC §18 "selected/{account}/...") keeps the same
            # provenance value before and after conversion -- filtering by
            # provenance stays consistent across that transition instead of
            # silently changing when a photo is selected.
            "provenance": remote_state.account_slug(client.account),
            "remote_filename": asset.filename,
            "status": status,
        },
    )


def pull_account(folder: Path, client: ICloudClient, progress: PullProgress) -> None:
    folder = Path(folder)
    progress.account = client.account
    try:
        state = remote_state.load_state(folder, client.account)

        # T29: heavy work (preview fetches) is scoped to the user's working
        # date range at large-library scale -- metadata enumeration/upsert
        # below is deliberately NOT filtered by this (SPEC "stays
        # whole-library; keeps timeline + dupe detection complete"). Read
        # once per pull, not per-asset: this only ever reads a small JSON
        # file, but there's no reason to hit it thousands of times.
        wrange = folder_settings.working_range(folder_settings.load_settings(folder))
        range_start = range_end = None
        if wrange is not None:
            range_from, range_to = wrange
            if range_from is not None:
                range_start = _day_start(range_from)
            if range_to is not None:
                range_end = _day_end(range_to)

        def _in_range(captured_at: datetime) -> bool:
            if range_start is not None and captured_at < range_start:
                return False
            return not (range_end is not None and captured_at > range_end)

        known_ids = set(
            Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=client.account)
            .exclude(remote_id=None)
            .values_list("remote_id", flat=True)
        )

        counters_lock = threading.Lock()

        def _fetch_preview(rid: str, media_type: str) -> None:
            # The bulk sync fetches the small "thumb" tier (~60KB, plenty for
            # grid cells; the ~700KB "medium" tier meant ~28GB at 41k scale).
            # Videos need the JPEG poster variants -- their plain thumb/medium
            # renditions are MP4s. Callers submit a fetch ONLY for assets the
            # enumeration has already cached (direct CDN download, the one
            # path that has never failed live) -- except the post-enumeration
            # leftovers, whose album lookups are bounded by the process-wide
            # socket timeout (icloud.py) instead of hanging forever.
            version = "thumb_image" if media_type == Photo.MEDIA_VIDEO else "thumb"
            err = None
            try:
                dest = previews_module.remote_preview_dest(folder, client.account, rid)
                if not dest.exists():
                    client.download(rid, version, dest)
            except Exception as exc:
                err = f"{rid}: preview fetch failed: {exc}"
            with counters_lock:
                progress.done += 1
                if err is not None:
                    progress.errors.append(err)

        # Fetch strategy (rebuilt from live findings, 2026-08-24):
        #  - Apple TARPITS throttled album-lookup queries (no response, no
        #    error) and pyicloud has no timeouts -- bulk fetching via lookups
        #    wedged all 8 workers for the whole day ("previews 0 / 2788").
        #  - The enumeration itself caches every asset it scans, and cached
        #    assets download via a direct CDN URL -- the only reliable path.
        #  - list_assets enumerates NEWEST-first (icloud.py), so a recent
        #    working range is cached within the first minutes.
        # So: previews are fetched FROM the enumeration stream -- the moment
        # an asset in the pending sets is scanned, its fetch is submitted.
        # In-range items fetch immediately; out-of-range items queue up and
        # backfill after the enumeration (range = priority, not fence).
        backlog_pending: dict[str, str] = {}  # rid -> media_type, in-range
        rest_pending: dict[str, str] = {}  # rid -> media_type, out-of-range
        for rid, media_type, captured_at in (
            Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=client.account)
            .exclude(remote_id=None)
            .order_by("captured_at", "pk")
            .values_list("remote_id", "media_type", "captured_at")
        ):
            dest = previews_module.remote_preview_dest(folder, client.account, rid)
            if dest.exists():
                if media_type == Photo.MEDIA_VIDEO and not _is_jpeg(dest):
                    # Cache repair: earlier pulls saved videos' MP4 "medium"
                    # rendition under the .jpg preview name.
                    dest.unlink(missing_ok=True)
                else:
                    continue
            if _in_range(captured_at):
                backlog_pending[rid] = media_type
            else:
                rest_pending[rid] = media_type

        rest_ready: list[tuple[str, str]] = []
        iteration_failed = False
        max_captured_at: datetime | None = state.cursor
        with ThreadPoolExecutor(max_workers=8) as pool:
            with counters_lock:
                progress.total += len(backlog_pending)

            try:
                for asset in client.list_assets(since=None):
                    with counters_lock:
                        progress.scanned += 1
                    rid = asset.remote_id

                    if rid in backlog_pending:
                        media_type = backlog_pending.pop(rid)
                        pool.submit(_fetch_preview, rid, media_type)
                    elif rid in rest_pending:
                        rest_ready.append((rid, rest_pending.pop(rid)))

                    if rid in known_ids or rid in state.downloaded:
                        continue
                    try:
                        # Row upsert always happens regardless of the range
                        # (metadata stays whole-library, per SPEC).
                        _process_asset(folder, client, state, asset)
                        known_ids.add(rid)
                        if max_captured_at is None or asset.captured_at > max_captured_at:
                            max_captured_at = asset.captured_at
                    except Exception as exc:  # per-asset errors never abort the pull
                        progress.errors.append(f"{rid}: {exc}")
                        continue
                    if _in_range(asset.captured_at):
                        with counters_lock:
                            progress.total += 1
                        pool.submit(_fetch_preview, rid, asset.media_type)
                    else:
                        rest_ready.append((rid, asset.media_type))
            except Exception as exc:
                progress.errors.append(f"list_assets: {exc}")
                iteration_failed = True

            # In-range items the enumeration never yielded (deleted remotely,
            # or the enumeration died early): try the album-lookup path --
            # bounded by the socket timeout, so a tarpitted lookup costs one
            # worker 60s, not forever.
            for rid, media_type in backlog_pending.items():
                pool.submit(_fetch_preview, rid, media_type)

            # Whole-library backfill, newest-first (enumeration order),
            # strictly behind the in-range work in the pool's FIFO order.
            with counters_lock:
                progress.total += len(rest_ready)
            for rid, media_type in rest_ready:
                pool.submit(_fetch_preview, rid, media_type)
        # Pool context exit drains all queued preview fetches.

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


def resume_pulls(folder: Path) -> None:
    """Boot-time resume (CTO pain point, 2026-08-24: every restart left the
    preview backlog dead until a manual "Pull now"): for every attached
    account whose stored session is still valid, start a background pull.
    Pulls are incremental + the preview backlog self-heals, so this is
    always safe to fire. Session validation itself hits the network, so the
    whole sweep runs on a daemon thread -- boot never blocks on it.
    """
    folder = Path(folder)

    def _run() -> None:
        try:
            from . import remote_state
            from .icloud import ICloudClient

            for account in remote_state.list_accounts(folder):
                try:
                    client = ICloudClient.from_session(account)
                except Exception:
                    continue
                if client is not None:
                    start_background_pull(folder, client)
        finally:
            connection.close()

    threading.Thread(target=_run, daemon=True).start()

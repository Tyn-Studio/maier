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

        # Cache-miss preview downloads walk the album via `photos.all.get(id)`,
        # which CONTENDS with the active enumeration over the album's shared
        # pagination and starves every worker (observed live: "previews
        # 0 / 2788" for minutes, 2026-08-24). Two-layer fix:
        #  1. Preferred: give the workers their OWN client (independent
        #     pyicloud session + album) via from_session -- uncontended
        #     `.get(id)` lookups are fast (~2s incl. download, probed live),
        #     so in-range previews flow from the first seconds even while
        #     the enumeration runs. (Deferring them instead just reproduced
        #     the gray-grid for the whole enumeration -- v0.1.1 regression.)
        #  2. Fallback (no second session available -- expired mid-pull, or
        #     a test fake without from_session): defer cache misses until
        #     the enumeration has cached them, via `has_asset_cached`.
        preview_client = client
        from_session = getattr(type(client), "from_session", None)
        if from_session is not None:
            try:
                second = from_session(client.account)
            except Exception:
                second = None
            if second is not None:
                preview_client = second
        has_cached = (
            None if preview_client is not client else getattr(client, "has_asset_cached", None)
        )
        deferred: list[tuple[str, str]] = []
        deferred_lock = threading.Lock()

        def _fetch_preview(rid: str, media_type: str, allow_defer: bool = True) -> None:
            # The bulk sync fetches the small "thumb" tier (~60KB, plenty for
            # grid cells): at 41k real-library scale the ~700KB "medium" tier
            # meant ~28GB and hours of downloading before the grid was fully
            # browsable (2026-08-24). Review-screen quality upgrades come on
            # demand later. Videos need the JPEG poster variants -- their
            # plain thumb/medium renditions are MP4s, which saved under a
            # .jpg preview name render as broken thumbnails.
            version = "thumb_image" if media_type == Photo.MEDIA_VIDEO else "thumb"
            err = None
            try:
                dest = previews_module.remote_preview_dest(folder, client.account, rid)
                if not dest.exists():
                    if allow_defer and has_cached is not None and not has_cached(rid):
                        with deferred_lock:
                            deferred.append((rid, media_type))
                        return  # done is counted when the deferred fetch runs
                    preview_client.download(rid, version, dest)
            except Exception as exc:
                err = f"{rid}: preview fetch failed: {exc}"
            with counters_lock:
                progress.done += 1
                if err is not None:
                    progress.errors.append(err)

        # Single concurrent pass (real-library findings, 2026-08-24: ~30
        # assets/s enumeration, 41k items, hours of serial previews):
        #  - the enumeration thread streams metadata, upserting new rows as
        #    they arrive so the timeline fills progressively;
        #  - four preview workers run THROUGHOUT, starting immediately on
        #    the known backlog (rows whose preview never landed -- failed
        #    fetch, wiped .maier/, or an interrupted earlier pull) and
        #    picking up each new row as it's discovered. A re-pull after an
        #    interruption therefore shows thumbnails right away instead of
        #    after another full enumeration. The workers share the client's
        #    one requests.Session -- urllib3's pool handles concurrent use.
        #
        # The enumeration is deliberately NOT filtered by the cursor: iCloud
        # libraries gain OLD photos later (device syncs, imports), and a
        # capture-date filter would hide those forever. "Incremental" per
        # SPEC §18 = skip already-known remote_ids and re-downloads.
        # CTO decision (2026-08-24, supersedes T29's hard fence): the working
        # range is a PRIORITY, not a filter -- in-range previews fetch first
        # (capture-date ascending, matching the grid's scroll order so the
        # visible screen fills top-down), then EVERYTHING else backfills
        # (newest-first) after the enumeration finishes.
        backlog: list[tuple[str, str]] = []  # in-range, fetched immediately
        rest: list[tuple[str, str]] = []  # out-of-range, fetched post-enum
        backlog_qs = (
            Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=client.account)
            .exclude(remote_id=None)
            .order_by("captured_at", "pk")
        )
        for rid, media_type, captured_at in backlog_qs.values_list(
            "remote_id", "media_type", "captured_at"
        ):
            dest = previews_module.remote_preview_dest(folder, client.account, rid)
            if dest.exists():
                if media_type == Photo.MEDIA_VIDEO and not _is_jpeg(dest):
                    # Cache repair: earlier pulls saved videos' "medium"
                    # rendition (an MP4) under the .jpg preview name.
                    # Previews are cache -- discard and refetch the poster.
                    dest.unlink(missing_ok=True)
                else:
                    continue
            if _in_range(captured_at):
                backlog.append((rid, media_type))
            else:
                rest.append((rid, media_type))
        rest.reverse()  # backfill newest-first (query was ascending)

        iteration_failed = False
        max_captured_at: datetime | None = state.cursor
        with ThreadPoolExecutor(max_workers=8) as pool:
            with counters_lock:
                progress.total += len(backlog)
            for rid, media_type in backlog:
                pool.submit(_fetch_preview, rid, media_type)

            try:
                for asset in client.list_assets(since=None):
                    with counters_lock:
                        progress.scanned += 1
                    if asset.remote_id in known_ids or asset.remote_id in state.downloaded:
                        continue
                    try:
                        # T29: the row upsert always happens regardless of
                        # the working range -- only the preview *fetch*
                        # below is scoped (metadata enumeration stays
                        # whole-library, per SPEC).
                        _process_asset(folder, client, state, asset)
                        known_ids.add(asset.remote_id)
                        if max_captured_at is None or asset.captured_at > max_captured_at:
                            max_captured_at = asset.captured_at
                    except Exception as exc:  # per-asset errors never abort the pull
                        progress.errors.append(f"{asset.remote_id}: {exc}")
                        continue
                    if _in_range(asset.captured_at):
                        with counters_lock:
                            progress.total += 1
                        pool.submit(_fetch_preview, asset.remote_id, asset.media_type)
                    else:
                        # Out-of-range discovery: backfilled after the
                        # enumeration, behind the in-range work.
                        rest.append((asset.remote_id, asset.media_type))
            except Exception as exc:
                progress.errors.append(f"list_assets: {exc}")
                iteration_failed = True

            # Enumeration is over (successfully or not): every asset it saw
            # is in the client's cache, so the deferred backlog can now fetch
            # without album lookups. Items STILL uncached (deleted remotely,
            # or the enumeration died early) fall through to the slow-lookup
            # path -- allow_defer=False so they can't loop back here.
            with deferred_lock:
                to_fetch = list(deferred)
                deferred.clear()
            for rid, media_type in to_fetch:
                pool.submit(_fetch_preview, rid, media_type, False)

            # Then the whole-library backfill: everything outside the
            # working range, newest-first, strictly behind the in-range
            # work in the pool's FIFO order. All post-enumeration, so no
            # album-pagination contention either.
            with counters_lock:
                progress.total += len(rest)
            for rid, media_type in rest:
                pool.submit(_fetch_preview, rid, media_type, False)
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

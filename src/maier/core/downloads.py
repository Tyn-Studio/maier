"""Background original-download worker (SPEC §18 rules 2-3, PLAN T17):
selecting a remote (iCloud) photo enqueues its original for download into
`selected/...`; once it lands the Photo row converts to an ordinary local
row (`source="local"`) and every further status change flows through the
normal move engine (`core/moves.py` via `core/culling.py`).

PLAN T24 (CTO decision, 2026-08-24): `selected/` is flat, so the download
destination is `selected/{filename}` directly -- no `{account-slug}/`
subdir. `provenance` stays the account slug (a DB field, filterable in the
UI) even though it no longer shapes the path. `Photo.original_path` is
deliberately left empty for these rows (PLAN T24 rule 6): unflagging a
downloaded iCloud photo falls to `moves._resolve_source_rel`'s last-resort
rule, landing it at `{account-slug}/{filename}` in the root -- a sensible
place to recreate on unflag, since there was never a real "original path"
to restore for a photo that only ever existed remotely.

The queue is DB-derived, not an in-memory list (crash-safe, matches
scan.py/pull.py's own background-thread pattern): a "pending" item is any
`Photo(source="icloud", status="selected")` row whose `remote_id` isn't
already recorded in that account's `state.downloaded` map. `enqueue_original`
never blocks on the network -- it only ensures a single daemon worker thread
is running; the actual download happens on that thread.

T20 (CTO requirement, PLAN 2026-08-24 "Round 3"): iCloud HEIC/HEIF originals
have no full-res JPEG rendition (probed live: medium tops out at ~1536px) --
selecting one must still land a directly-usable full-res JPEG/PNG in
`selected/`, EXIF preserved. This is a DELIBERATE exception to "what lands
is the original" for iCloud downloads only -- local files (manual exports)
are never touched, per CLAUDE.md hard rule 2 (moves only). Live Photos also
fetch their video companion (`ICloudClient.download_live_video`) once the
still lands.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from django.db import connection
from PIL import Image, UnidentifiedImageError

from . import moves, remote_state
from .models import Photo

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pillow-heif optional at runtime; HEIC just won't decode
    pass

# HEIC/HEIF originals get converted (T20); every other extension (JPG, PNG,
# DNG/RAW, videos) downloads unchanged, as before.
_HEIC_EXTENSIONS = {".heic", ".heif"}
_JPEG_QUALITY = 92

# Named tuple (not an inline `except (A, B, C):` literal) -- ruff 0.16.4
# formatter bug, see previews.py/icloud.py for the same workaround.
_HEIC_CONVERSION_ERRORS = (OSError, UnidentifiedImageError, ValueError)


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


def _remote_filename(photo: Photo) -> str:
    filename = PurePosixPath(photo.remote_filename).name if photo.remote_filename else ""
    if not filename:
        filename = f"{photo.remote_id}.jpg"
    return filename


def _dest_for(folder: Path, photo: Photo, slug: str) -> Path:
    # PLAN T24 (CTO decision): `selected/` is flat -- no `{slug}` subdir.
    # `slug` is still threaded through (used for the DB `provenance` field
    # in `_convert_to_local` and for the account-scoped staging dir) even
    # though it no longer shapes the destination path.
    dest_dir = folder / "selected"
    # `moves._unique_path` is a private helper (flagged, per brief): reused
    # here rather than duplicated so the collision-suffix rule (SPEC §3/§4:
    # " (n)" before the extension, never overwrite) stays a single
    # implementation shared by ordinary moves and remote downloads.
    return moves._unique_path(dest_dir / _remote_filename(photo))


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


def _staging_dir(folder: Path) -> Path:
    # Same volume as `selected/{slug}/` (both under the working folder), so
    # the conversion step's `os.replace` from here into `selected/` is a
    # same-volume rename -- not a system temp dir, which could be a
    # different filesystem (T20 brief: "download to a TEMP file, not into
    # selected/" -- staged as a hidden file under `.maier/`, never visible
    # in Finder alongside real selections).
    d = folder / ".maier" / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _convert_heic_to_dest(src: Path, dest_stem: str, dest_dir: Path) -> Path:
    """Convert a downloaded HEIC/HEIF original to a full-res JPEG (or PNG
    when the image has an alpha channel), EXIF preserved, at full original
    resolution (no `.thumbnail()` resize -- unlike `previews.py`, this is
    the deliverable, not a preview). Deliberately does NOT call
    `ImageOps.exif_transpose`: this is a faithful pixel conversion that
    keeps the original EXIF orientation tag intact, not a display-baked
    preview -- viewers already honor EXIF orientation. Returns the final
    (collision-suffixed) destination path; raises on failure (caller
    decides the HEIC-fallback policy).
    """
    with Image.open(src) as img:
        exif_bytes = img.info.get("exif")
        if img.mode in ("RGBA", "LA", "PA"):
            candidate = moves._unique_path(dest_dir / f"{dest_stem}.png")
            fmt = "PNG"
            # Pillow >= 9 supports exif= for PNG too (verified against the
            # pinned Pillow version, 2026-08-24); no fallback needed here.
            save_kwargs: dict = {"exif": exif_bytes} if exif_bytes else {}
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            candidate = moves._unique_path(dest_dir / f"{dest_stem}.jpg")
            fmt = "JPEG"
            save_kwargs = {"quality": _JPEG_QUALITY}
            if exif_bytes:
                save_kwargs["exif"] = exif_bytes

        tmp = candidate.parent / f"{candidate.name}.part"
        try:
            img.save(tmp, fmt, **save_kwargs)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    os.replace(tmp, candidate)
    return candidate


def _download_and_convert_heic(
    folder: Path,
    client,
    photo: Photo,
    filename: str,
    dest_dir: Path,
    progress: DownloadProgress,
) -> Path | None:
    """Download a HEIC/HEIF original to a staging file, convert it, and
    return the final destination path. Returns None (error already
    recorded) if the download itself failed. Conversion failure falls back
    to saving the HEIC original as-is under its own name -- an error is
    still recorded so the fallback is visible, but a HEIC in `selected/`
    beats nothing landing at all (T20 brief).
    """
    staging = _staging_dir(folder) / f"{photo.remote_id}{Path(filename).suffix.lower()}"
    try:
        client.download(photo.remote_id, "original", staging)
    except Exception as exc:  # per-item errors never abort the worker run
        progress.errors.append(f"{photo.remote_id}: {exc}")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        return _convert_heic_to_dest(staging, Path(filename).stem, dest_dir)
    except _HEIC_CONVERSION_ERRORS as exc:
        progress.errors.append(
            f"{photo.remote_id}: HEIC conversion failed ({exc}) -- saved original HEIC instead"
        )
        fallback_dest = moves._unique_path(dest_dir / filename)
        os.replace(staging, fallback_dest)
        return fallback_dest
    finally:
        staging.unlink(missing_ok=True)


def _handle_live_photo(
    folder: Path, client, photo: Photo, slug: str, still_dest: Path, progress: DownloadProgress
) -> None:
    """After a still image lands (converted or not), fetch its Live Photo
    video companion (T20, SPEC §18/CLAUDE.md rule 3). `client` may be a test
    double without `download_live_video` (older fakes) -- treated as "no
    live video available", not an error. Best-effort: on any failure the
    still image is left intact and an error is recorded; retry is NOT
    automatic (flagged gap, per brief) -- the row is already `source=
    "local"` by this point, so nothing will re-enqueue this photo.
    """
    if not hasattr(client, "download_live_video"):
        return

    video_dest = moves._unique_path(still_dest.parent / f"{still_dest.stem}.mov")
    try:
        wrote = client.download_live_video(photo.remote_id, video_dest)
    except Exception as exc:
        progress.errors.append(f"{photo.remote_id}: live video fetch failed: {exc}")
        return
    if not wrote:
        return

    try:
        st = video_dest.stat()
        rel_video = video_dest.relative_to(folder).as_posix()
        Photo.objects.filter(pk=photo.pk).update(live_photo_video_path=rel_video)
        # A brand-new local file with no Photo row of its own yet -- create
        # one so scan/queries stay consistent (it's hidden as a companion
        # by the existing `live_photo_companion_paths` exclusion, same as
        # any local Live Photo's .mov).
        Photo.objects.create(
            source=Photo.SOURCE_LOCAL,
            relative_path=rel_video,
            status=Photo.STATUS_SELECTED,
            provenance=slug,
            file_size=st.st_size,
            file_mtime=st.st_mtime,
            captured_at=photo.captured_at,
            captured_at_source="exif",
            media_type=Photo.MEDIA_VIDEO,
        )
    except Exception as exc:
        progress.errors.append(f"{photo.remote_id}: live video row bookkeeping failed: {exc}")


def _download_one(folder: Path, client, photo: Photo, progress: DownloadProgress) -> None:
    slug = remote_state.account_slug(photo.account)
    filename = _remote_filename(photo)

    if Path(filename).suffix.lower() in _HEIC_EXTENSIONS:
        # PLAN T24: flat selected/, no `{slug}` subdir (see `_dest_for`).
        dest_dir = folder / "selected"
        dest = _download_and_convert_heic(folder, client, photo, filename, dest_dir, progress)
        if dest is None:
            return  # download itself failed; error already recorded
    else:
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
        return

    _handle_live_photo(folder, client, photo, slug, dest, progress)


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

"""Phase B items 2 and 3 (SPEC §6, §8, §17.3): background SHA-256 hashing
queue, exact-duplicate grouping, pHash computation, and time-windowed
near-dupe pairing. Mirrors scan.py's background-thread pattern: work is
derived from DB state (`sha256__isnull=True` / `phash__isnull=True`), so a
rerun after an interruption simply picks up where it left off -- no
separate "resume" bookkeeping needed.

Exact-dupe groups are never materialized as a model/migration: they're
derived on read via `values("sha256").annotate(...)` queries, kept cheap
with a two-query approach (never per-photo queries in a loop).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import imagehash
from django.db import connection
from django.db.models import Count, Q, QuerySet
from PIL import Image, UnidentifiedImageError

from . import moves
from . import previews as previews_module
from .models import DuplicatePair, Photo

_HASH_CHUNK = 1024 * 1024  # 1 MiB, per brief

# SPEC §6.3: near-dupe scan is time-windowed (±8s) and hamming-thresholded.
_PHASH_TIME_WINDOW_SECONDS = 8
_PHASH_MAX_HAMMING_DISTANCE = 8

# SPEC §6.4 fallback rule: same basename (stem, case-insensitive) + capture
# time within 1s. exiftool's ContentIdentifier match is the SPEC's primary
# rule but is out of scope here -- exiftool is absent in M1/M2 (SPEC §12,
# PLAN decisions log), so only the fallback is implemented.
_LIVE_PHOTO_TIME_WINDOW_SECONDS = 1
_LIVE_PHOTO_VIDEO_EXTENSION = ".mov"

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see previews.py).
_PHASH_ERRORS = (OSError, UnidentifiedImageError, ValueError)


@dataclass
class PhaseBProgress:
    total: int = 0
    done: int = 0
    errors: list[str] = field(default_factory=list)
    finished: bool = False


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _hash_pending(folder: Path, progress: PhaseBProgress) -> None:
    try:
        pending = list(
            Photo.objects.filter(sha256__isnull=True, missing=False).values_list(
                "pk", "relative_path"
            )
        )
    except Exception as exc:
        # Transient DB contention (e.g. a scan's own transaction still open)
        # -- never crash the background thread; the next Phase B run (kicked
        # off by the next scan) retries from scratch.
        progress.errors.append(f"phase B: could not list pending photos: {exc}")
        return

    progress.total = len(pending)

    for pk, rel_path in pending:
        try:
            abspath = folder / rel_path
            if not abspath.exists():
                # Vanished between being queued and hashed -- record and move
                # on; the next Phase A scan will mark it missing.
                progress.errors.append(f"{rel_path}: file no longer exists")
                continue
            digest = _sha256_file(abspath)
            # Short, idempotent write -- re-running is always safe.
            Photo.objects.filter(pk=pk).update(sha256=digest)
        except Exception as exc:  # per-file errors never abort the run
            progress.errors.append(f"{rel_path}: {exc}")
        finally:
            progress.done += 1


def _phash_pending(folder: Path, progress: PhaseBProgress) -> None:
    """pHash the preview of every non-missing, hashed, not-yet-pHashed image
    Photo (SPEC §6 item 3: "pHash of the preview"). Runs as a second pass
    with its own additions to `progress.total` -- keeps the single progress
    object meaningful across both steps without a separate counter.

    Photos whose preview resolves to the shared RAW/video/corrupt placeholder
    are skipped (still counted in total/done): hashing the identical
    placeholder would pair every such photo with every other one.
    """
    try:
        pending = list(
            Photo.objects.filter(
                phash__isnull=True,
                missing=False,
                media_type=Photo.MEDIA_IMAGE,
                sha256__isnull=False,
            ).values_list("pk", "relative_path")
        )
    except Exception as exc:
        progress.errors.append(f"phase B: could not list photos pending pHash: {exc}")
        return

    progress.total += len(pending)

    for pk, rel_path in pending:
        try:
            photo = Photo.objects.get(pk=pk)
            preview = previews_module.preview_path(folder, photo)
            if preview.name == previews_module._PLACEHOLDER_NAME:
                continue
            with Image.open(preview) as img:
                digest = imagehash.phash(img)
            Photo.objects.filter(pk=pk).update(phash=str(digest))
        except Photo.DoesNotExist:
            continue
        except _PHASH_ERRORS as exc:
            progress.errors.append(f"{rel_path}: {exc}")
        finally:
            progress.done += 1


def _pair_near_duplicates(progress: PhaseBProgress) -> None:
    """Time-windowed near-dupe scan (SPEC §6.3): O(burst), not O(folder).
    Photos are sorted by `captured_at`; a sliding window holds only the
    already-seen photos within the trailing 8s, so each new photo is
    compared against a small burst rather than the whole collection.
    Idempotent: `get_or_create` on the canonical (lower pk, higher pk) pair.
    """
    try:
        rows = list(
            Photo.objects.filter(phash__isnull=False, missing=False)
            .order_by("captured_at", "pk")
            .values_list("pk", "captured_at", "phash", "sha256")
        )
    except Exception as exc:
        progress.errors.append(f"phase B: could not list photos for near-dupe scan: {exc}")
        return

    window: list[tuple[int, datetime, imagehash.ImageHash, str | None]] = []

    for pk, captured_at, phash_hex, sha256 in rows:
        try:
            current_hash = imagehash.hex_to_hash(phash_hex)
        except ValueError as exc:
            progress.errors.append(f"pk={pk}: invalid phash {phash_hex!r}: {exc}")
            continue

        while window and (captured_at - window[0][1]).total_seconds() > _PHASH_TIME_WINDOW_SECONDS:
            window.pop(0)

        for other_pk, _other_captured_at, other_hash, other_sha256 in window:
            if sha256 and other_sha256 and sha256 == other_sha256:
                continue  # exact dupes are T7's domain, not a near-dupe pair
            distance = current_hash - other_hash
            if distance <= _PHASH_MAX_HAMMING_DISTANCE:
                lo, hi = sorted((pk, other_pk))
                try:
                    DuplicatePair.objects.get_or_create(
                        photo_a_id=lo,
                        photo_b_id=hi,
                        defaults={"hamming_distance": distance},
                    )
                except Exception as exc:
                    progress.errors.append(f"pk={lo}/{hi}: could not record duplicate pair: {exc}")

        window.append((pk, captured_at, current_hash, sha256))


# --- Live Photo pairing (SPEC §6 item 4, §6.4 fallback) ---------------------


def _parent_and_stem(relative_path: str) -> tuple[str, str]:
    p = PurePosixPath(relative_path)
    return p.parent.as_posix(), p.stem.lower()


def live_photo_companion_paths() -> set[str]:
    """relative_path values currently paired as some non-missing image's
    Live Photo companion. SPEC §6 item 4: the companion is "hidden as a
    standalone item" -- callers (queries.filtered_photos) exclude these.
    """
    return set(
        Photo.objects.filter(missing=False)
        .exclude(live_photo_video_path__isnull=True)
        .exclude(live_photo_video_path="")
        .values_list("live_photo_video_path", flat=True)
    )


def _pair_live_photos(progress: PhaseBProgress) -> None:
    """Fallback-only Live Photo pairing (SPEC §6.4): a non-missing `.mov`
    whose stem (case-insensitive) matches a non-missing image's stem, in
    the *same directory*, with `captured_at` within 1s, becomes that
    image's companion.

    Deliberate tightening of the SPEC fallback: pairing is restricted to
    photos and videos sharing a parent directory. A Live Photo export always
    lands its `.mov` right next to its image; matching across directories
    would risk false pairs from unrelated same-named/same-second files
    elsewhere in the working folder.

    Self-heals first: if an image's recorded companion path no longer
    resolves to a non-missing Photo row (moved/renamed outside Culler),
    clear it so it becomes eligible for re-pairing below.
    """
    try:
        paired_images = list(
            Photo.objects.filter(media_type=Photo.MEDIA_IMAGE, missing=False)
            .exclude(live_photo_video_path__isnull=True)
            .exclude(live_photo_video_path="")
            .values_list("pk", "live_photo_video_path")
        )
    except Exception as exc:
        progress.errors.append(f"phase B: could not list paired images: {exc}")
        return

    if paired_images:
        candidate_paths = {path for _pk, path in paired_images}
        existing_video_paths = set(
            Photo.objects.filter(relative_path__in=candidate_paths, missing=False).values_list(
                "relative_path", flat=True
            )
        )
        dangling_pks = [pk for pk, path in paired_images if path not in existing_video_paths]
        if dangling_pks:
            Photo.objects.filter(pk__in=dangling_pks).update(live_photo_video_path=None)

    try:
        videos = list(
            Photo.objects.filter(media_type=Photo.MEDIA_VIDEO, missing=False).values_list(
                "relative_path", "captured_at"
            )
        )
        images = list(
            Photo.objects.filter(media_type=Photo.MEDIA_IMAGE, missing=False)
            .filter(Q(live_photo_video_path__isnull=True) | Q(live_photo_video_path=""))
            .values_list("pk", "relative_path", "captured_at")
        )
    except Exception as exc:
        progress.errors.append(f"phase B: could not list photos for Live Photo pairing: {exc}")
        return

    video_by_key: dict[tuple[str, str], tuple[str, datetime]] = {}
    for rel_path, captured_at in videos:
        if PurePosixPath(rel_path).suffix.lower() != _LIVE_PHOTO_VIDEO_EXTENSION:
            continue
        video_by_key[_parent_and_stem(rel_path)] = (rel_path, captured_at)

    for pk, rel_path, captured_at in images:
        match = video_by_key.get(_parent_and_stem(rel_path))
        if match is None:
            continue
        video_path, video_captured_at = match
        delta = abs((captured_at - video_captured_at).total_seconds())
        if delta <= _LIVE_PHOTO_TIME_WINDOW_SECONDS:
            Photo.objects.filter(pk=pk).update(live_photo_video_path=video_path)


def run_phase_b(folder: Path, progress: PhaseBProgress) -> None:
    """Hash every non-missing, not-yet-hashed Photo, pHash their previews,
    pair up near-duplicates within a time window, and pair Live Photos.
    Exact-dupe groups are derived by query (see `duplicate_group` /
    `duplicate_counts`) rather than written here -- once hashes land,
    grouping is immediately visible to readers, no further step required.
    Never aborts on a single-file error; errors accumulate in
    `progress.errors`.
    """
    folder = Path(folder)
    try:
        _hash_pending(folder, progress)
        _phash_pending(folder, progress)
        _pair_near_duplicates(progress)
        _pair_live_photos(progress)
    finally:
        progress.finished = True


_pb_lock = threading.Lock()
_current_phase_b: PhaseBProgress | None = None


def start_phase_b(folder: Path) -> PhaseBProgress:
    global _current_phase_b
    with _pb_lock:
        if _current_phase_b is not None and not _current_phase_b.finished:
            return _current_phase_b
        progress = PhaseBProgress()
        _current_phase_b = progress

    def _run() -> None:
        try:
            run_phase_b(folder, progress)
        finally:
            connection.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return progress


# --- exact-dupe grouping (SPEC §8) -----------------------------------------


def duplicate_group(photo: Photo) -> QuerySet[Photo]:
    """Photos sharing `photo`'s sha256 (including itself), excluding missing
    ones. Empty queryset when the photo has no sha256 yet.
    """
    if not photo.sha256:
        return Photo.objects.none()
    return Photo.objects.filter(sha256=photo.sha256, missing=False)


def duplicate_counts() -> dict[str, int]:
    """sha256 -> member count, for every group with more than one non-missing
    photo sharing it.
    """
    rows = (
        Photo.objects.filter(missing=False, sha256__isnull=False)
        .values("sha256")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
    )
    return {row["sha256"]: row["n"] for row in rows}


def _representative_pks() -> dict[str, int]:
    """sha256 -> representative pk, for sha256s with a duplicate group.
    Representative = lowest pk with status != rejected, else lowest pk.
    Single pass over one query (no per-photo queries).
    """
    dupe_shas = list(duplicate_counts())
    if not dupe_shas:
        return {}

    best: dict[str, tuple[int, int]] = {}  # sha -> (rejected_flag, pk)
    rows = Photo.objects.filter(sha256__in=dupe_shas, missing=False).values_list(
        "pk", "sha256", "status"
    )
    for pk, sha, status in rows:
        candidate = (1 if status == Photo.STATUS_REJECTED else 0, pk)
        if sha not in best or candidate < best[sha]:
            best[sha] = candidate
    return {sha: pk for sha, (_flag, pk) in best.items()}


def non_representative_pks() -> set[int]:
    """pks that are redundant exact-dupe copies -- hidden from the grid."""
    reps = _representative_pks()
    if not reps:
        return set()
    dupe_shas = list(reps)
    excluded: set[int] = set()
    for pk, sha in Photo.objects.filter(sha256__in=dupe_shas, missing=False).values_list(
        "pk", "sha256"
    ):
        if pk != reps[sha]:
            excluded.add(pk)
    return excluded


# --- group cull / auto-reject policy (SPEC §8, §17.3) -----------------------


def apply_status_to_group(folder: Path, photo: Photo, new_status: str) -> Photo:
    """Apply `new_status` to `photo` (the targeted photo acts as the
    representative for this action) and, if it belongs to an exact-dupe
    group, auto-move every other non-missing member to `rejected/` unless
    already there. Never auto-restores: unflagging the representative later
    leaves redundant copies in `rejected/` (SPEC §17.3). Falls back to a
    plain `moves.apply_status` when the photo has no sha256 / no group.
    """
    updated = moves.apply_status(folder, photo, new_status)
    if not updated.sha256:
        return updated

    others = Photo.objects.filter(sha256=updated.sha256, missing=False).exclude(pk=updated.pk)
    for other in others:
        if other.status == Photo.STATUS_REJECTED:
            continue
        try:
            moves.apply_status(folder, other, Photo.STATUS_REJECTED)
        except FileNotFoundError:
            # Vanished on disk between the query and the move -- leave it;
            # the next Phase A scan reconciles/marks it missing.
            pass

    return updated


# --- near-dupe review (SPEC §8) ---------------------------------------------


def _unresolved_pairs_qs() -> QuerySet[DuplicatePair]:
    return DuplicatePair.objects.filter(
        resolved=False, photo_a__missing=False, photo_b__missing=False
    ).order_by("pk")


def unresolved_pair_count() -> int:
    return _unresolved_pairs_qs().count()


def next_unresolved_pair(after_pk: int | None = None) -> DuplicatePair | None:
    """First unresolved pair (both photos non-missing), ordered by pk --
    the review queue. With `after_pk`, returns the first unresolved pair
    with a higher pk, wrapping to the very first unresolved pair if none
    remain -- used both for "next pair after resolving this one" and for
    `defer` (which pushes the current pair to the back of the queue without
    resolving it by simply not being returned again until we wrap around).
    """
    qs = _unresolved_pairs_qs().select_related("photo_a", "photo_b")
    if after_pk is not None:
        nxt = qs.filter(pk__gt=after_pk).first()
        if nxt is not None:
            return nxt
    return qs.first()

"""Phase B item 2 (SPEC §6, §8, §17.3): background SHA-256 hashing queue and
exact-duplicate grouping. Mirrors scan.py's background-thread pattern:
work is derived from DB state (`sha256__isnull=True`), so a rerun after an
interruption simply picks up where it left off -- no separate "resume"
bookkeeping needed.

Exact-dupe groups are never materialized as a model/migration: they're
derived on read via `values("sha256").annotate(...)` queries, kept cheap
with a two-query approach (never per-photo queries in a loop).
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path

from django.db import connection
from django.db.models import Count, QuerySet

from . import moves
from .models import Photo

_HASH_CHUNK = 1024 * 1024  # 1 MiB, per brief


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


def run_phase_b(folder: Path, progress: PhaseBProgress) -> None:
    """Hash every non-missing, not-yet-hashed Photo. Exact-dupe groups are
    derived by query (see `duplicate_group` / `duplicate_counts`) rather
    than written here -- once hashes land, grouping is immediately visible
    to readers, no further step required. Never aborts on a single-file
    error; errors accumulate in `progress.errors`.
    """
    folder = Path(folder)
    try:
        _hash_pending(folder, progress)
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

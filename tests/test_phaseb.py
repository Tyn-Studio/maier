"""Phase B items 2/3 (SPEC §6/§8/§17.3): SHA-256 background queue,
exact-dupe grouping, pHash computation, and time-windowed near-dupe
pairing. Unit tests call `run_phase_b` synchronously against `tmp_path`
trees built with `build_fixture_folder` / `moves.apply_status`; the two
`start_phase_b` single-flight/threading tests mirror `test_scan.py`'s
pattern for `start_background_scan`.
"""

import hashlib
import io
import random
import shutil
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import imagehash
import pytest
from PIL import Image, ImageDraw

from culler.core import phaseb as phaseb_module
from culler.core.models import DuplicatePair, Photo
from culler.core.phaseb import (
    PhaseBProgress,
    apply_status_to_group,
    duplicate_counts,
    duplicate_group,
    non_representative_pks,
    run_phase_b,
    start_phase_b,
)
from culler.core.scan import ScanProgress, scan
from fixtures import build_fixture_folder

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _pattern_image(seed: int, size: tuple[int, int] = (64, 64)) -> Image.Image:
    """A structurally distinct (not solid-colour) image -- pHash is
    structural, so solid colours all hash near-identically regardless of
    colour. Random-ish rectangles give a stable, seed-distinguishable
    structure for phash-distance testing.
    """
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    rnd = random.Random(seed)
    w, h = size
    for _ in range(12):
        x0 = rnd.randint(0, w - 10)
        y0 = rnd.randint(0, h - 10)
        x1 = x0 + rnd.randint(5, 15)
        y1 = y0 + rnd.randint(5, 15)
        color = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        draw.rectangle([x0, y0, x1, y1], fill=color)
    return img


def _save_jpeg(img: Image.Image, path: Path, quality: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "JPEG", quality=quality)


def _resaved(img: Image.Image, quality: int) -> Image.Image:
    """Simulate a near-identical crop/resave of the same shot -- genuine
    JPEG recompression artifacts, not a trivially-identical `.copy()`.
    """
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    buf.seek(0)
    return Image.open(buf).convert("RGB")


def _db_photo(relative_path: str, **overrides) -> Photo:
    kwargs = dict(
        status=Photo.STATUS_OPTIONAL,
        provenance="",
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    kwargs.update(overrides)
    return Photo.objects.create(relative_path=relative_path, **kwargs)


def _real_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- run_phase_b: hashing ---------------------------------------------------


@pytest.mark.django_db
def test_run_phase_b_fills_sha256_for_all_photos(tmp_path):
    build_fixture_folder(
        tmp_path,
        {"a.jpg": {"size": (6, 4)}, "sub/b.jpg": {"size": (10, 10)}},
    )
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)  # scan() itself may have started phase B already

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert progress.finished is True
    assert progress.errors == []
    # 2 sha256 hashes + 2 pHashes of the same two photos (second pass).
    assert progress.done == progress.total == 4

    a = Photo.objects.get(relative_path="a.jpg")
    b = Photo.objects.get(relative_path="sub/b.jpg")
    assert a.sha256 == _real_sha256(tmp_path / "a.jpg")
    assert b.sha256 == _real_sha256(tmp_path / "sub/b.jpg")
    assert a.sha256 != b.sha256
    assert a.phash
    assert b.phash


@pytest.mark.django_db
def test_rerun_is_noop_already_hashed_rows_untouched(tmp_path, monkeypatch):
    build_fixture_folder(tmp_path, {"a.jpg": None})
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)
    run_phase_b(tmp_path, PhaseBProgress())

    photo = Photo.objects.get(relative_path="a.jpg")
    assert photo.sha256

    calls: list[Path] = []
    original = phaseb_module._sha256_file

    def _counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(phaseb_module, "_sha256_file", _counting)

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert calls == []
    assert progress.total == 0
    assert progress.done == 0
    assert progress.finished is True
    photo.refresh_from_db()
    assert photo.sha256 == _real_sha256(tmp_path / "a.jpg")


@pytest.mark.django_db
def test_error_on_unreadable_file_recorded_rest_proceed(tmp_path):
    build_fixture_folder(tmp_path, {"present.jpg": None, "gone.jpg": None})
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)

    (tmp_path / "gone.jpg").unlink()

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert progress.finished is True
    # 2 sha256 attempts (present + gone) + 1 pHash attempt (only present has
    # a sha256, so only it is eligible for the pHash pass).
    assert progress.done == progress.total == 3
    assert len(progress.errors) == 1
    assert "gone.jpg" in progress.errors[0]

    present = Photo.objects.get(relative_path="present.jpg")
    gone = Photo.objects.get(relative_path="gone.jpg")
    assert present.sha256 == _real_sha256(tmp_path / "present.jpg")
    assert present.phash
    assert gone.sha256 is None
    assert gone.phash is None


# --- pHash fill (SPEC §6 item 3) ----------------------------------------


@pytest.mark.django_db
def test_phash_fill_computes_distinct_hashes_for_structurally_different_photos(tmp_path):
    img_a = _pattern_image(seed=1)
    img_b = _pattern_image(seed=2)
    # Verify the fixture assumption directly with imagehash before asserting
    # app behaviour -- solid colours would all hash near-identically.
    assert (imagehash.phash(img_a) - imagehash.phash(img_b)) > 8

    _save_jpeg(img_a, tmp_path / "a.jpg")
    _save_jpeg(img_b, tmp_path / "b.jpg")
    photo_a = _db_photo("a.jpg", sha256="a" * 64)
    photo_b = _db_photo("b.jpg", sha256="b" * 64)

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert progress.errors == []
    photo_a.refresh_from_db()
    photo_b.refresh_from_db()
    assert photo_a.phash
    assert photo_b.phash
    assert photo_a.phash != photo_b.phash


@pytest.mark.django_db
def test_phash_skips_raw_placeholder_preview(tmp_path):
    # RAW extension: preview_path returns the shared placeholder without
    # even needing the source file to exist -- hashing it would pair every
    # RAW/video/corrupt photo with every other one.
    photo = _db_photo("raw.dng", sha256="c" * 64)

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert progress.errors == []
    photo.refresh_from_db()
    assert photo.phash is None


@pytest.mark.django_db
def test_phash_rerun_is_noop_already_phashed_rows_untouched(tmp_path, monkeypatch):
    img = _pattern_image(seed=3)
    _save_jpeg(img, tmp_path / "a.jpg")
    photo = _db_photo("a.jpg", sha256="d" * 64)

    run_phase_b(tmp_path, PhaseBProgress())
    photo.refresh_from_db()
    original_phash = photo.phash
    assert original_phash

    called = []
    original = imagehash.phash

    def _counting(img):
        called.append(img)
        return original(img)

    monkeypatch.setattr(phaseb_module.imagehash, "phash", _counting)

    progress = PhaseBProgress()
    run_phase_b(tmp_path, progress)

    assert called == []
    photo.refresh_from_db()
    assert photo.phash == original_phash


# --- near-dupe pairing (SPEC §6.3/§8) -------------------------------------


@pytest.mark.django_db
def test_near_dupe_pair_created_within_time_window(tmp_path):
    base = _pattern_image(seed=10)
    near = _resaved(base, quality=70)
    assert (imagehash.phash(base) - imagehash.phash(near)) <= 8

    _save_jpeg(base, tmp_path / "a.jpg")
    _save_jpeg(near, tmp_path / "b.jpg", quality=70)
    _db_photo(
        "a.jpg",
        sha256="1" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
    )
    _db_photo(
        "b.jpg",
        sha256="2" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 3, tzinfo=UTC),  # +3s
    )

    run_phase_b(tmp_path, PhaseBProgress())

    a = Photo.objects.get(relative_path="a.jpg")
    b = Photo.objects.get(relative_path="b.jpg")
    pair = DuplicatePair.objects.get(photo_a=min(a, b, key=lambda p: p.pk))
    assert {pair.photo_a_id, pair.photo_b_id} == {a.pk, b.pk}
    assert pair.hamming_distance <= 8
    assert pair.resolved is False


@pytest.mark.django_db
def test_no_pair_when_outside_time_window(tmp_path):
    base = _pattern_image(seed=11)
    near = _resaved(base, quality=70)
    assert (imagehash.phash(base) - imagehash.phash(near)) <= 8

    _save_jpeg(base, tmp_path / "a.jpg")
    _save_jpeg(near, tmp_path / "b.jpg", quality=70)
    _db_photo(
        "a.jpg",
        sha256="3" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
    )
    _db_photo(
        "b.jpg",
        sha256="4" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=60),
    )

    run_phase_b(tmp_path, PhaseBProgress())

    assert DuplicatePair.objects.count() == 0


@pytest.mark.django_db
def test_no_pair_for_identical_sha256_even_if_close(tmp_path):
    img = _pattern_image(seed=12)
    _save_jpeg(img, tmp_path / "a.jpg")
    _save_jpeg(img, tmp_path / "b.jpg")
    same_sha = "5" * 64
    _db_photo(
        "a.jpg",
        sha256=same_sha,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
    )
    _db_photo(
        "b.jpg",
        sha256=same_sha,
        captured_at=datetime(2025, 6, 14, 12, 0, 1, tzinfo=UTC),
    )

    run_phase_b(tmp_path, PhaseBProgress())

    assert DuplicatePair.objects.count() == 0


@pytest.mark.django_db
def test_near_dupe_pairing_idempotent_on_rerun(tmp_path):
    base = _pattern_image(seed=13)
    near = _resaved(base, quality=70)
    assert (imagehash.phash(base) - imagehash.phash(near)) <= 8

    _save_jpeg(base, tmp_path / "a.jpg")
    _save_jpeg(near, tmp_path / "b.jpg", quality=70)
    _db_photo(
        "a.jpg",
        sha256="6" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
    )
    _db_photo(
        "b.jpg",
        sha256="7" * 64,
        captured_at=datetime(2025, 6, 14, 12, 0, 2, tzinfo=UTC),
    )

    run_phase_b(tmp_path, PhaseBProgress())
    assert DuplicatePair.objects.count() == 1

    run_phase_b(tmp_path, PhaseBProgress())
    assert DuplicatePair.objects.count() == 1


# --- exact-dupe grouping -----------------------------------------------


@pytest.mark.django_db
def test_duplicate_detection_identical_content_groups_of_two(tmp_path):
    build_fixture_folder(tmp_path, {"a/one.jpg": None})
    shutil.copy(tmp_path / "a/one.jpg", tmp_path / "a/one-copy.jpg")
    assert _real_sha256(tmp_path / "a/one.jpg") == _real_sha256(tmp_path / "a/one-copy.jpg")

    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)
    run_phase_b(tmp_path, PhaseBProgress())

    photo = Photo.objects.get(relative_path="a/one.jpg")
    group = duplicate_group(photo)
    assert group.count() == 2
    assert set(group.values_list("relative_path", flat=True)) == {"a/one.jpg", "a/one-copy.jpg"}

    counts = duplicate_counts()
    assert counts[photo.sha256] == 2


@pytest.mark.django_db
def test_duplicate_group_empty_when_no_sha256():
    photo = _db_photo("no-hash.jpg", sha256=None)
    assert duplicate_group(photo).count() == 0


@pytest.mark.django_db
def test_duplicate_counts_excludes_singletons_and_missing():
    _db_photo("unique.jpg", sha256="a" * 64)
    _db_photo("dupe1.jpg", sha256="b" * 64)
    _db_photo("dupe2.jpg", sha256="b" * 64)
    _db_photo("missing-dupe.jpg", sha256="c" * 64, missing=True)
    _db_photo("missing-dupe2.jpg", sha256="c" * 64, missing=True)

    counts = duplicate_counts()

    assert counts == {"b" * 64: 2}


@pytest.mark.django_db
def test_non_representative_pks_prefers_non_rejected_lowest_pk():
    sha = "d" * 64
    rejected_first = _db_photo("rej.jpg", sha256=sha, status=Photo.STATUS_REJECTED)
    optional_second = _db_photo("opt.jpg", sha256=sha, status=Photo.STATUS_OPTIONAL)

    excluded = non_representative_pks()

    # lowest-pk non-rejected member wins representative status even though
    # it has a higher pk than the (already) rejected member.
    assert excluded == {rejected_first.pk}
    assert optional_second.pk not in excluded


@pytest.mark.django_db
def test_non_representative_pks_falls_back_to_lowest_pk_when_all_rejected():
    sha = "e" * 64
    first = _db_photo("r1.jpg", sha256=sha, status=Photo.STATUS_REJECTED)
    second = _db_photo("r2.jpg", sha256=sha, status=Photo.STATUS_REJECTED)

    excluded = non_representative_pks()

    assert excluded == {second.pk}
    assert first.pk not in excluded


@pytest.mark.django_db
def test_non_representative_pks_empty_when_no_duplicates():
    _db_photo("solo.jpg", sha256="f" * 64)
    assert non_representative_pks() == set()


# --- group cull / auto-reject policy ------------------------------------


@pytest.mark.django_db
def test_apply_status_to_group_rejects_other_members(tmp_path):
    build_fixture_folder(tmp_path, {"a/one.jpg": None})
    shutil.copy(tmp_path / "a/one.jpg", tmp_path / "a/one-copy.jpg")
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)
    run_phase_b(tmp_path, PhaseBProgress())

    rep = Photo.objects.get(relative_path="a/one.jpg")
    other = Photo.objects.get(relative_path="a/one-copy.jpg")

    updated = apply_status_to_group(tmp_path, rep, Photo.STATUS_SELECTED)

    assert updated.status == Photo.STATUS_SELECTED
    assert (tmp_path / "selected/a/one.jpg").exists()

    other.refresh_from_db()
    assert other.status == Photo.STATUS_REJECTED
    assert (tmp_path / "rejected/a/one-copy.jpg").exists()
    assert not (tmp_path / "a/one-copy.jpg").exists()


@pytest.mark.django_db
def test_apply_status_to_group_unflag_does_not_restore_others(tmp_path):
    build_fixture_folder(tmp_path, {"a/one.jpg": None})
    shutil.copy(tmp_path / "a/one.jpg", tmp_path / "a/one-copy.jpg")
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)
    run_phase_b(tmp_path, PhaseBProgress())

    rep = Photo.objects.get(relative_path="a/one.jpg")
    other = Photo.objects.get(relative_path="a/one-copy.jpg")

    apply_status_to_group(tmp_path, rep, Photo.STATUS_SELECTED)
    rep.refresh_from_db()

    unflagged = apply_status_to_group(tmp_path, rep, Photo.STATUS_OPTIONAL)

    assert unflagged.status == Photo.STATUS_OPTIONAL
    assert (tmp_path / "a/one.jpg").exists()

    other.refresh_from_db()
    assert other.status == Photo.STATUS_REJECTED
    assert (tmp_path / "rejected/a/one-copy.jpg").exists()


@pytest.mark.django_db
def test_apply_status_to_group_no_sha256_behaves_like_plain_apply_status(tmp_path):
    build_fixture_folder(tmp_path, {"solo.jpg": None})
    scan(tmp_path, ScanProgress())
    photo = Photo.objects.get(relative_path="solo.jpg")
    assert photo.sha256 is None  # no run_phase_b call yet in this test

    updated = apply_status_to_group(tmp_path, photo, Photo.STATUS_SELECTED)

    assert updated.status == Photo.STATUS_SELECTED
    assert (tmp_path / "selected/solo.jpg").exists()


@pytest.mark.django_db
def test_apply_status_to_group_no_duplicates_leaves_group_query_empty(tmp_path):
    build_fixture_folder(tmp_path, {"solo.jpg": None})
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)
    run_phase_b(tmp_path, PhaseBProgress())

    photo = Photo.objects.get(relative_path="solo.jpg")
    assert photo.sha256  # hashed, but unique -- no group

    updated = apply_status_to_group(tmp_path, photo, Photo.STATUS_SELECTED)

    assert updated.status == Photo.STATUS_SELECTED
    assert (tmp_path / "selected/solo.jpg").exists()


# --- start_phase_b threading ---------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_phase_b_finishes_and_hashes(tmp_path):
    build_fixture_folder(tmp_path, {"a.jpg": None})
    Photo.objects.all().delete()
    scan(tmp_path, ScanProgress())
    Photo.objects.update(sha256=None)

    progress = start_phase_b(tmp_path)

    deadline = time.time() + 10
    while not progress.finished and time.time() < deadline:
        time.sleep(0.05)

    assert progress.finished is True
    photo = Photo.objects.get(relative_path="a.jpg")
    assert photo.sha256 == _real_sha256(tmp_path / "a.jpg")


@pytest.mark.django_db(transaction=True)
def test_start_phase_b_single_flight(monkeypatch, tmp_path):
    release = threading.Event()
    started = threading.Event()

    def _slow_run(folder, progress):
        started.set()
        release.wait(timeout=5)
        progress.finished = True

    monkeypatch.setattr(phaseb_module, "run_phase_b", _slow_run)

    progress1 = start_phase_b(tmp_path)
    assert started.wait(timeout=5)
    progress2 = start_phase_b(tmp_path)

    assert progress1 is progress2

    release.set()
    deadline = time.time() + 5
    while not progress1.finished and time.time() < deadline:
        time.sleep(0.02)
    assert progress1.finished is True

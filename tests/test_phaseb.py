"""Phase B item 2 (SPEC §6/§8/§17.3): SHA-256 background queue + exact-dupe
grouping. Unit tests call `run_phase_b` synchronously against `tmp_path`
trees built with `build_fixture_folder` / `moves.apply_status`; the two
`start_phase_b` single-flight/threading tests mirror `test_scan.py`'s
pattern for `start_background_scan`.
"""

import hashlib
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from culler.core import phaseb as phaseb_module
from culler.core.models import Photo
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
    assert progress.done == progress.total == 2

    a = Photo.objects.get(relative_path="a.jpg")
    b = Photo.objects.get(relative_path="sub/b.jpg")
    assert a.sha256 == _real_sha256(tmp_path / "a.jpg")
    assert b.sha256 == _real_sha256(tmp_path / "sub/b.jpg")
    assert a.sha256 != b.sha256


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
    assert progress.done == progress.total == 2
    assert len(progress.errors) == 1
    assert "gone.jpg" in progress.errors[0]

    present = Photo.objects.get(relative_path="present.jpg")
    gone = Photo.objects.get(relative_path="gone.jpg")
    assert present.sha256 == _real_sha256(tmp_path / "present.jpg")
    assert gone.sha256 is None


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

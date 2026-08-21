import os
import shutil
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from culler.core import scan as scan_module
from culler.core.models import Photo
from culler.core.scan import ScanProgress, scan, start_background_scan


def _make_jpeg(path: Path, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (4, 4), color=(50, 60, 70)).save(path, "jpeg")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _build_tree(root: Path) -> None:
    _make_jpeg(root / "apple-luis" / "IMG_0001.jpg")
    _make_jpeg(root / "apple-luis" / "IMG_0002.jpg")
    _make_jpeg(root / "lightroom" / "IMG_0003.jpg")
    _make_jpeg(root / "IMG_0004.jpg")
    _make_jpeg(root / "selected" / "apple-luis" / "IMG_0005.jpg")
    _make_jpeg(root / "selected" / "IMG_0006.jpg")
    _make_jpeg(root / "rejected" / "lightroom" / "IMG_0007.jpg")

    (root / "notes.txt").parent.mkdir(parents=True, exist_ok=True)
    (root / "notes.txt").write_text("not media")

    (root / ".culler").mkdir(parents=True, exist_ok=True)
    (root / ".culler" / "culler.sqlite3").write_bytes(b"fake db, must be ignored")

    (root / ".hidden").mkdir(parents=True, exist_ok=True)
    _make_jpeg(root / ".hidden" / "IMG_9999.jpg")


EXPECTED_PATHS = {
    "apple-luis/IMG_0001.jpg",
    "apple-luis/IMG_0002.jpg",
    "lightroom/IMG_0003.jpg",
    "IMG_0004.jpg",
    "selected/apple-luis/IMG_0005.jpg",
    "selected/IMG_0006.jpg",
    "rejected/lightroom/IMG_0007.jpg",
}


@pytest.mark.django_db
def test_scan_indexes_fixture_tree(tmp_path):
    _build_tree(tmp_path)
    progress = ScanProgress()

    scan(tmp_path, progress)

    assert progress.finished is True
    assert progress.errors == []
    assert progress.total == progress.done == len(EXPECTED_PATHS)

    photos = {p.relative_path: p for p in Photo.objects.all()}
    assert set(photos) == EXPECTED_PATHS

    assert photos["apple-luis/IMG_0001.jpg"].status == Photo.STATUS_OPTIONAL
    assert photos["apple-luis/IMG_0001.jpg"].provenance == "apple-luis"

    assert photos["IMG_0004.jpg"].status == Photo.STATUS_OPTIONAL
    assert photos["IMG_0004.jpg"].provenance == ""

    assert photos["selected/apple-luis/IMG_0005.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/apple-luis/IMG_0005.jpg"].provenance == "apple-luis"

    assert photos["selected/IMG_0006.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/IMG_0006.jpg"].provenance == ""

    assert photos["rejected/lightroom/IMG_0007.jpg"].status == Photo.STATUS_REJECTED
    assert photos["rejected/lightroom/IMG_0007.jpg"].provenance == "lightroom"

    for photo in photos.values():
        assert photo.media_type == Photo.MEDIA_IMAGE
        assert photo.missing is False


@pytest.mark.django_db
def test_rescan_skips_unchanged_files(tmp_path, monkeypatch):
    _build_tree(tmp_path)
    scan(tmp_path, ScanProgress())

    calls: list[Path] = []
    original = scan_module.capture_datetime

    def _counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(scan_module, "capture_datetime", _counting)

    progress = ScanProgress()
    scan(tmp_path, progress)

    assert calls == []
    assert progress.done == progress.total == len(EXPECTED_PATHS)
    assert progress.errors == []


@pytest.mark.django_db
def test_changed_mtime_triggers_reread(tmp_path, monkeypatch):
    _build_tree(tmp_path)
    scan(tmp_path, ScanProgress())

    target = tmp_path / "IMG_0004.jpg"
    new_mtime = target.stat().st_mtime + 100
    os.utime(target, (new_mtime, new_mtime))

    calls: list[Path] = []
    original = scan_module.capture_datetime

    def _counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(scan_module, "capture_datetime", _counting)

    scan(tmp_path, ScanProgress())

    assert calls == [target]

    photo = Photo.objects.get(relative_path="IMG_0004.jpg")
    assert photo.file_mtime == pytest.approx(new_mtime)


@pytest.mark.django_db
def test_move_reconciliation_relinks_same_row(tmp_path):
    _build_tree(tmp_path)
    scan(tmp_path, ScanProgress())

    old_photo = Photo.objects.get(relative_path="apple-luis/IMG_0001.jpg")
    old_id = old_photo.id
    old_captured_at = old_photo.captured_at
    old_photo.sha256 = "deadbeef"
    old_photo.save()

    new_path = tmp_path / "apple-luis" / "moved" / "IMG_0001.jpg"
    new_path.parent.mkdir(parents=True)
    shutil.move(str(tmp_path / "apple-luis" / "IMG_0001.jpg"), str(new_path))

    scan(tmp_path, ScanProgress())

    assert not Photo.objects.filter(relative_path="apple-luis/IMG_0001.jpg").exists()

    relinked = Photo.objects.get(relative_path="apple-luis/moved/IMG_0001.jpg")
    assert relinked.id == old_id
    assert relinked.sha256 == "deadbeef"
    assert relinked.captured_at == old_captured_at
    assert relinked.missing is False
    assert relinked.status == Photo.STATUS_OPTIONAL
    assert relinked.provenance == "apple-luis"

    assert Photo.objects.filter(relative_path="apple-luis/IMG_0001.jpg").count() == 0
    assert Photo.objects.count() == len(EXPECTED_PATHS)


@pytest.mark.django_db
def test_reconciliation_consumes_candidate_once(tmp_path):
    # Two identical files (same size + mtime) both vanish; only one new path
    # appears. Exactly one row may re-link; the other must go missing -- and
    # the re-linked row must survive with its id intact.
    _make_jpeg(tmp_path / "a" / "twin.jpg", mtime=1_700_000_000)
    shutil.copy2(str(tmp_path / "a" / "twin.jpg"), str(tmp_path / "a" / "twin-copy.jpg"))
    scan(tmp_path, ScanProgress())
    ids_before = set(Photo.objects.values_list("id", flat=True))

    (tmp_path / "b").mkdir()
    shutil.move(str(tmp_path / "a" / "twin.jpg"), str(tmp_path / "b" / "twin.jpg"))
    (tmp_path / "a" / "twin-copy.jpg").unlink()

    scan(tmp_path, ScanProgress())

    assert set(Photo.objects.values_list("id", flat=True)) == ids_before
    relinked = Photo.objects.get(relative_path="b/twin.jpg")
    assert relinked.missing is False
    missing = Photo.objects.exclude(pk=relinked.pk).get()
    assert missing.missing is True


@pytest.mark.django_db
def test_deleted_file_marked_missing(tmp_path):
    _build_tree(tmp_path)
    scan(tmp_path, ScanProgress())

    (tmp_path / "IMG_0004.jpg").unlink()

    scan(tmp_path, ScanProgress())

    photo = Photo.objects.get(relative_path="IMG_0004.jpg")
    assert photo.missing is True


@pytest.mark.django_db
def test_reappeared_file_clears_missing(tmp_path):
    _build_tree(tmp_path)
    scan(tmp_path, ScanProgress())

    (tmp_path / "IMG_0004.jpg").unlink()
    scan(tmp_path, ScanProgress())
    assert Photo.objects.get(relative_path="IMG_0004.jpg").missing is True

    _make_jpeg(tmp_path / "IMG_0004.jpg")
    scan(tmp_path, ScanProgress())
    assert Photo.objects.get(relative_path="IMG_0004.jpg").missing is False


@pytest.mark.django_db
def test_scan_records_per_file_errors_without_aborting(tmp_path, monkeypatch):
    _build_tree(tmp_path)

    original = scan_module.capture_datetime

    def _boom(path):
        if path.name == "IMG_0004.jpg":
            raise ValueError("boom")
        return original(path)

    monkeypatch.setattr(scan_module, "capture_datetime", _boom)

    progress = ScanProgress()
    scan(tmp_path, progress)

    assert progress.finished is True
    assert progress.done == progress.total == len(EXPECTED_PATHS)
    assert len(progress.errors) == 1
    assert "IMG_0004.jpg" in progress.errors[0]

    assert not Photo.objects.filter(relative_path="IMG_0004.jpg").exists()
    assert Photo.objects.count() == len(EXPECTED_PATHS) - 1


@pytest.mark.django_db(transaction=True)
def test_start_background_scan_finishes(tmp_path):
    _build_tree(tmp_path)

    progress = start_background_scan(tmp_path)

    deadline = time.time() + 10
    while not progress.finished and time.time() < deadline:
        time.sleep(0.05)

    assert progress.finished is True
    assert progress.errors == []
    assert progress.total == len(EXPECTED_PATHS)


@pytest.mark.django_db(transaction=True)
def test_start_background_scan_single_flight(monkeypatch, tmp_path):
    release = threading.Event()
    started = threading.Event()

    def _slow_scan(folder, progress):
        started.set()
        release.wait(timeout=5)
        progress.finished = True

    monkeypatch.setattr(scan_module, "scan", _slow_scan)

    progress1 = start_background_scan(tmp_path)
    assert started.wait(timeout=5)
    progress2 = start_background_scan(tmp_path)

    assert progress1 is progress2

    release.set()
    deadline = time.time() + 5
    while not progress1.finished and time.time() < deadline:
        time.sleep(0.02)
    assert progress1.finished is True

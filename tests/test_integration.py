"""End-to-end integration tests (SPEC §15): index a fixture folder ->
assert DB matches filesystem; cull via views -> files physically moved;
external moves -> rescan converges; `.maier/` cache loss -> state rebuilt
from locations alone; reopen -> no-op diff; full grid/cull/filter loop.

Flows that touch views (set-status, grid) must create their fixture files
inside `settings.WORKING_FOLDER`, since the views resolve moves against
that setting rather than an explicit folder arg -- see `tests/test_views.py`
and `tests/_bootstrap.py` for why the DB is bound to a session-wide tmp
folder. Each such test uses a unique subfolder name (`t_integration_*`) to
avoid path collisions with fixtures left behind by other tests in the same
session (files persist across tests; only DB rows roll back per-test).
"""

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from fixtures import build_fixture_folder
from maier.core import moves
from maier.core import scan as scan_module
from maier.core.models import Photo
from maier.core.scan import ScanProgress, scan


def _local(naive: datetime) -> datetime:
    # Mirrors metadata.py's own naive-EXIF-timestamp -> UTC conversion, so
    # expectations are correct regardless of the test machine's local tz.
    return naive.astimezone(UTC)


# --- 1. index -> DB matches filesystem --------------------------------


@pytest.mark.django_db
def test_index_matches_filesystem(tmp_path):
    spec = {
        "apple-luis/IMG_0001.jpg": {"datetime_original": "2025:06:01 10:00:00"},
        "apple-luis/IMG_0002.jpg": {"datetime_original": "2025:06:02 11:00:00"},
        "lightroom/IMG_0003.jpg": {"datetime_original": "2025:06:03 12:00:00"},
        "IMG_0004.jpg": {"datetime_original": "2025:06:04 13:00:00"},
        "selected/apple-luis/IMG_0005.jpg": {"datetime_original": "2025:06:05 14:00:00"},
        "selected/IMG_0006.jpg": {"datetime_original": "2025:06:06 15:00:00"},
        "rejected/lightroom/IMG_0007.jpg": {"datetime_original": "2025:06:07 16:00:00"},
        # no EXIF, but a parseable filename timestamp
        "apple-luis/IMG_20250608_170000.jpg": None,
        # no EXIF, no filename pattern -- falls through to file mtime
        "apple-luis/plain.jpg": {"mtime": 1_700_000_000.0},
        # non-media junk, must be ignored
        "notes.txt": {"junk": True},
    }
    build_fixture_folder(tmp_path, spec)

    # Reserved .maier/ dir (as if a prior open already created a cache)
    # must never be walked into.
    (tmp_path / ".maier").mkdir()
    (tmp_path / ".maier" / "maier.sqlite3").write_bytes(b"fake db, must be ignored")

    progress = ScanProgress()
    scan(tmp_path, progress)

    expected_media_paths = {p for p in spec if Path(p).suffix.lower() != ".txt"}

    assert progress.finished is True
    assert progress.errors == []
    assert progress.total == progress.done == len(expected_media_paths)

    photos = {p.relative_path: p for p in Photo.objects.all()}
    assert set(photos) == expected_media_paths
    assert Photo.objects.count() == len(expected_media_paths)

    assert photos["apple-luis/IMG_0001.jpg"].status == Photo.STATUS_OPTIONAL
    assert photos["apple-luis/IMG_0001.jpg"].provenance == "apple-luis"
    assert photos["apple-luis/IMG_0001.jpg"].captured_at_source == "exif"
    assert photos["apple-luis/IMG_0001.jpg"].captured_at == _local(datetime(2025, 6, 1, 10, 0, 0))

    assert photos["IMG_0004.jpg"].status == Photo.STATUS_OPTIONAL
    assert photos["IMG_0004.jpg"].provenance == ""

    assert photos["selected/apple-luis/IMG_0005.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/apple-luis/IMG_0005.jpg"].provenance == "apple-luis"

    assert photos["selected/IMG_0006.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/IMG_0006.jpg"].provenance == ""

    assert photos["rejected/lightroom/IMG_0007.jpg"].status == Photo.STATUS_REJECTED
    assert photos["rejected/lightroom/IMG_0007.jpg"].provenance == "lightroom"

    filename_photo = photos["apple-luis/IMG_20250608_170000.jpg"]
    assert filename_photo.captured_at_source == "filename"
    assert filename_photo.captured_at == _local(datetime(2025, 6, 8, 17, 0, 0))

    mtime_photo = photos["apple-luis/plain.jpg"]
    assert mtime_photo.captured_at_source == "file_mtime"
    assert mtime_photo.captured_at == datetime.fromtimestamp(1_700_000_000.0, tz=UTC)

    for photo in photos.values():
        assert photo.media_type == Photo.MEDIA_IMAGE
        assert photo.missing is False


# --- 2. cull via views -> files physically moved ------------------------


@pytest.mark.django_db
def test_cull_via_views_moves_file_then_unflag_restores(client):
    unique = "t_integration_cull"
    rel = f"{unique}/apple-luis/IMG_0001.jpg"
    build_fixture_folder(
        settings.WORKING_FOLDER, {rel: {"datetime_original": "2025:06:01 10:00:00"}}
    )

    progress = ScanProgress()
    scan(settings.WORKING_FOLDER, progress)
    assert progress.errors == []

    photo = Photo.objects.get(relative_path=rel)
    old_path = settings.WORKING_FOLDER / rel
    new_path = settings.WORKING_FOLDER / f"selected/{unique}/apple-luis/IMG_0001.jpg"

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "selected", "context": "grid"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert f"cell-{photo.pk}" in body
    assert "status-selected" in body

    assert new_path.exists()
    assert not old_path.exists()

    photo.refresh_from_db()
    assert photo.status == "selected"
    assert photo.relative_path == f"selected/{unique}/apple-luis/IMG_0001.jpg"

    # unflag: status=optional restores the original path on disk
    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "optional", "context": "grid"},
    )

    assert response.status_code == 200
    photo.refresh_from_db()
    assert photo.status == "optional"
    assert photo.relative_path == rel
    assert old_path.exists()
    assert not new_path.exists()


# --- 3. rescan converges after external (Finder-style) moves ------------


@pytest.mark.django_db
def test_rescan_converges_after_external_moves(tmp_path):
    spec = {
        "apple-luis/IMG_0001.jpg": {
            "datetime_original": "2025:06:01 10:00:00",
            "mtime": 1_700_000_000.0,
        },
        "rejected/lightroom/IMG_0002.jpg": {
            "datetime_original": "2025:06:02 11:00:00",
            "mtime": 1_700_000_100.0,
        },
        "lightroom/IMG_0003.jpg": {"datetime_original": "2025:06:03 12:00:00"},
    }
    build_fixture_folder(tmp_path, spec)
    scan(tmp_path, ScanProgress())

    before_ids = {p.relative_path: p.id for p in Photo.objects.all()}

    # Simulate Finder: user drags apple-luis/IMG_0001.jpg into selected/,
    # mirroring the source substructure, and drags rejected/lightroom's
    # photo back out to its original root location -- both outside the app.
    selected_dest = tmp_path / "selected" / "apple-luis" / "IMG_0001.jpg"
    selected_dest.parent.mkdir(parents=True)
    shutil.move(str(tmp_path / "apple-luis" / "IMG_0001.jpg"), str(selected_dest))

    restored_dest = tmp_path / "lightroom" / "IMG_0002.jpg"
    shutil.move(str(tmp_path / "rejected" / "lightroom" / "IMG_0002.jpg"), str(restored_dest))

    scan(tmp_path, ScanProgress())

    photos = {p.relative_path: p for p in Photo.objects.all()}
    assert set(photos) == {
        "selected/apple-luis/IMG_0001.jpg",
        "lightroom/IMG_0002.jpg",
        "lightroom/IMG_0003.jpg",
    }
    assert Photo.objects.count() == 3

    assert photos["selected/apple-luis/IMG_0001.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/apple-luis/IMG_0001.jpg"].provenance == "apple-luis"
    assert photos["lightroom/IMG_0002.jpg"].status == Photo.STATUS_OPTIONAL
    assert photos["lightroom/IMG_0002.jpg"].provenance == "lightroom"

    # (size, mtime) reconciliation preserved row identity across the move --
    # no duplicate rows, no re-read of capture metadata.
    assert photos["selected/apple-luis/IMG_0001.jpg"].id == before_ids["apple-luis/IMG_0001.jpg"]
    assert photos["lightroom/IMG_0002.jpg"].id == before_ids["rejected/lightroom/IMG_0002.jpg"]


# --- 4. .maier/ cache loss -> state rebuilt from locations alone -------


@pytest.mark.django_db
def test_maier_cache_loss_state_rebuilt_from_locations(tmp_path):
    spec = {
        "apple-luis/IMG_0001.jpg": {"datetime_original": "2025:06:01 10:00:00"},
        "lightroom/IMG_0002.jpg": {"datetime_original": "2025:06:02 11:00:00"},
    }
    build_fixture_folder(tmp_path, spec)
    scan(tmp_path, ScanProgress())

    photo = Photo.objects.get(relative_path="apple-luis/IMG_0001.jpg")
    moves.apply_status(tmp_path, photo, "selected")
    assert (tmp_path / "selected/apple-luis/IMG_0001.jpg").exists()

    # Stand-in for "the user deletes .maier/": we can't actually delete the
    # on-disk sqlite3 file here, since it's the live connection this whole
    # test session runs on (bound to MAIER_FOLDER, not this tmp_path -- see
    # tests/_bootstrap.py). Deleting every Photo row is the equivalent
    # cache-loss event for what this test is actually checking: that status
    # is fully re-derivable from file location alone, with no DB memory.
    Photo.objects.all().delete()

    progress = ScanProgress()
    scan(tmp_path, progress)

    assert progress.errors == []
    photos = {p.relative_path: p for p in Photo.objects.all()}
    assert set(photos) == {
        "selected/apple-luis/IMG_0001.jpg",
        "lightroom/IMG_0002.jpg",
    }
    assert Photo.objects.count() == 2
    assert photos["selected/apple-luis/IMG_0001.jpg"].status == Photo.STATUS_SELECTED
    assert photos["selected/apple-luis/IMG_0001.jpg"].provenance == "apple-luis"
    assert photos["lightroom/IMG_0002.jpg"].status == Photo.STATUS_OPTIONAL


# --- 5. re-open persistence: unchanged fixture -> second scan is a no-op --


@pytest.mark.django_db
def test_reopen_rescan_unchanged_is_noop_diff(tmp_path, monkeypatch):
    spec = {
        "apple-luis/IMG_0001.jpg": {"datetime_original": "2025:06:01 10:00:00"},
        "lightroom/IMG_0002.jpg": {"datetime_original": "2025:06:02 11:00:00"},
        "IMG_0003.jpg": None,
    }
    build_fixture_folder(tmp_path, spec)
    scan(tmp_path, ScanProgress())

    before = {
        p.relative_path: (
            p.id,
            p.status,
            p.captured_at,
            p.captured_at_source,
            p.file_size,
            p.file_mtime,
        )
        for p in Photo.objects.all()
    }

    calls: list[Path] = []
    original = scan_module.capture_datetime

    def _counting(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(scan_module, "capture_datetime", _counting)

    progress = ScanProgress()
    scan(tmp_path, progress)

    assert calls == []  # no re-read: (path, size, mtime) all unchanged
    assert progress.errors == []
    assert progress.done == progress.total == len(spec)

    after = {
        p.relative_path: (
            p.id,
            p.status,
            p.captured_at,
            p.captured_at_source,
            p.file_size,
            p.file_mtime,
        )
        for p in Photo.objects.all()
    }
    assert after == before


# --- 6. full-loop sanity: index -> grid -> cull -> filtered grid --------


@pytest.mark.django_db
def test_full_loop_index_grid_cull_filter(client):
    unique = "t_integration_full_loop"
    rel_a = f"{unique}/apple-luis/IMG_0001.jpg"
    rel_b = f"{unique}/lightroom/IMG_0002.jpg"
    build_fixture_folder(
        settings.WORKING_FOLDER,
        {
            rel_a: {"datetime_original": "2025:06:01 10:00:00"},
            rel_b: {"datetime_original": "2025:06:02 11:00:00"},
        },
    )

    scan(settings.WORKING_FOLDER, ScanProgress())

    photo_a = Photo.objects.get(relative_path=rel_a)
    photo_b = Photo.objects.get(relative_path=rel_b)
    # provenance is the first *top-level* path segment under the working
    # folder root -- our shared unique subfolder here, not "apple-luis" /
    # "lightroom" (those are nested one level deeper).
    assert photo_a.provenance == unique
    assert photo_b.provenance == unique

    response = client.get(reverse("grid"), {"provenance": unique})
    assert response.status_code == 200
    body = response.content.decode()
    assert reverse("preview", args=[photo_a.pk]) in body
    assert reverse("preview", args=[photo_b.pk]) in body

    response = client.post(
        reverse("set-status", args=[photo_a.pk]),
        {"status": "selected", "context": "grid"},
    )
    assert response.status_code == 200

    # Scope the filtered assertion to our own provenance, since the shared
    # session-wide working folder can carry "selected" leftovers from other
    # tests (only DB rows roll back per-test; moved files on disk persist).
    response = client.get(reverse("grid"), {"status": "selected", "provenance": unique})
    assert response.status_code == 200
    body = response.content.decode()
    assert reverse("preview", args=[photo_a.pk]) in body
    assert reverse("preview", args=[photo_b.pk]) not in body

from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from culler.core.models import Photo
from culler.core.moves import apply_status, dest_for

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _make_photo(relative_path: str, **overrides) -> Photo:
    kwargs = dict(
        relative_path=relative_path,
        status=overrides.pop("status", Photo.STATUS_OPTIONAL),
        provenance=overrides.pop("provenance", ""),
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    kwargs.update(overrides)
    return Photo.objects.create(**kwargs)


def _touch(folder, relative_path: str, content: bytes = b"data") -> None:
    path = folder / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


# --- dest_for ---------------------------------------------------------


def test_dest_for_select_mirrors_substructure():
    photo = Photo(relative_path="apple-luis/IMG_001.jpg")
    assert dest_for(photo, "selected") == PurePosixPath("selected/apple-luis/IMG_001.jpg")


def test_dest_for_reject_root_file():
    photo = Photo(relative_path="IMG.jpg")
    assert dest_for(photo, "rejected") == PurePosixPath("rejected/IMG.jpg")


def test_dest_for_optional_strips_status_prefix():
    photo = Photo(relative_path="selected/apple-luis/IMG_001.jpg")
    assert dest_for(photo, "optional") == PurePosixPath("apple-luis/IMG_001.jpg")


def test_dest_for_invalid_status_raises_value_error():
    photo = Photo(relative_path="a.jpg")
    with pytest.raises(ValueError):
        dest_for(photo, "bogus")


# --- apply_status: basic moves -----------------------------------------


@pytest.mark.django_db
def test_apply_status_mirrors_substructure(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    result = apply_status(tmp_path, photo, "selected")

    assert result.relative_path == "selected/apple-luis/IMG_001.jpg"
    assert result.status == "selected"
    assert result.status_changed_at is not None
    assert (tmp_path / "selected/apple-luis/IMG_001.jpg").exists()
    assert not (tmp_path / "apple-luis/IMG_001.jpg").exists()

    photo.refresh_from_db()
    assert photo.relative_path == "selected/apple-luis/IMG_001.jpg"
    assert photo.status == "selected"


@pytest.mark.django_db
def test_root_level_file_select(tmp_path):
    _touch(tmp_path, "IMG.jpg")
    photo = _make_photo("IMG.jpg")

    apply_status(tmp_path, photo, "selected")

    assert (tmp_path / "selected/IMG.jpg").exists()
    assert photo.relative_path == "selected/IMG.jpg"


@pytest.mark.django_db
def test_unflag_restore(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG_001.jpg")
    photo = _make_photo(
        "selected/apple-luis/IMG_001.jpg", provenance="apple-luis", status="selected"
    )

    apply_status(tmp_path, photo, "optional")

    assert photo.relative_path == "apple-luis/IMG_001.jpg"
    assert photo.status == "optional"
    assert (tmp_path / "apple-luis/IMG_001.jpg").exists()
    assert not (tmp_path / "selected/apple-luis/IMG_001.jpg").exists()


@pytest.mark.django_db
def test_reject_from_selected(tmp_path):
    _touch(tmp_path, "selected/a/x.jpg")
    photo = _make_photo("selected/a/x.jpg", provenance="a", status="selected")

    apply_status(tmp_path, photo, "rejected")

    assert photo.relative_path == "rejected/a/x.jpg"
    assert photo.status == "rejected"
    assert (tmp_path / "rejected/a/x.jpg").exists()
    assert not (tmp_path / "selected/a/x.jpg").exists()


# --- apply_status: no-op, invalid, missing ------------------------------


@pytest.mark.django_db
def test_noop_when_new_status_equals_current(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")
    original_changed_at = photo.status_changed_at

    result = apply_status(tmp_path, photo, "optional")

    assert result.relative_path == "apple-luis/IMG_001.jpg"
    assert result.status_changed_at == original_changed_at
    assert (tmp_path / "apple-luis/IMG_001.jpg").exists()


@pytest.mark.django_db
def test_invalid_status_raises_value_error(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    with pytest.raises(ValueError):
        apply_status(tmp_path, photo, "bogus")


@pytest.mark.django_db
def test_missing_source_raises_file_not_found_error(tmp_path):
    photo = _make_photo("apple-luis/IMG_missing.jpg", provenance="apple-luis")

    with pytest.raises(FileNotFoundError):
        apply_status(tmp_path, photo, "selected")

    photo.refresh_from_db()
    assert photo.status == "optional"
    assert photo.relative_path == "apple-luis/IMG_missing.jpg"


# --- apply_status: collisions --------------------------------------------


@pytest.mark.django_db
def test_collision_appends_numeric_suffix(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG_001.jpg", b"existing")
    _touch(tmp_path, "apple-luis/IMG_001.jpg", b"new")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/apple-luis/IMG_001 (1).jpg"
    assert (tmp_path / "selected/apple-luis/IMG_001 (1).jpg").read_bytes() == b"new"
    # the pre-existing file was never overwritten
    assert (tmp_path / "selected/apple-luis/IMG_001.jpg").read_bytes() == b"existing"


@pytest.mark.django_db
def test_second_collision_increments_suffix(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG_001.jpg", b"existing-0")
    _touch(tmp_path, "selected/apple-luis/IMG_001 (1).jpg", b"existing-1")
    _touch(tmp_path, "apple-luis/IMG_001.jpg", b"new")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/apple-luis/IMG_001 (2).jpg"
    assert (tmp_path / "selected/apple-luis/IMG_001 (2).jpg").read_bytes() == b"new"


# --- apply_status: Live Photo companion -----------------------------------


@pytest.mark.django_db
def test_live_photo_companion_moves_with_image(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    _touch(tmp_path, "apple-luis/IMG_001.mov")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/apple-luis/IMG_001.jpg"
    assert photo.live_photo_video_path == "selected/apple-luis/IMG_001.mov"
    assert (tmp_path / "selected/apple-luis/IMG_001.jpg").exists()
    assert (tmp_path / "selected/apple-luis/IMG_001.mov").exists()
    assert not (tmp_path / "apple-luis/IMG_001.mov").exists()


@pytest.mark.django_db
def test_live_photo_companion_collision_gets_own_suffix(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    _touch(tmp_path, "apple-luis/IMG_001.mov", b"new-mov")
    _touch(tmp_path, "selected/apple-luis/IMG_001.mov", b"existing-mov")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )

    apply_status(tmp_path, photo, "selected")

    # image itself has no collision, moves cleanly
    assert photo.relative_path == "selected/apple-luis/IMG_001.jpg"
    # companion collides and gets its own suffix
    assert photo.live_photo_video_path == "selected/apple-luis/IMG_001 (1).mov"
    assert (tmp_path / "selected/apple-luis/IMG_001 (1).mov").read_bytes() == b"new-mov"
    assert (tmp_path / "selected/apple-luis/IMG_001.mov").read_bytes() == b"existing-mov"


@pytest.mark.django_db
def test_live_photo_companion_missing_on_disk_does_not_crash(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/apple-luis/IMG_001.jpg"
    # recorded path is left unchanged for the scanner to reconcile
    assert photo.live_photo_video_path == "apple-luis/IMG_001.mov"

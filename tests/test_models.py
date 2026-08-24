from datetime import UTC, datetime

import pytest

from maier.core.models import Photo


@pytest.mark.django_db
def test_photo_create_read_roundtrip():
    Photo.objects.create(
        relative_path="apple-luis/IMG_0001.jpg",
        status=Photo.STATUS_OPTIONAL,
        provenance="apple-luis",
        file_size=123456,
        file_mtime=1_700_000_000.0,
        captured_at=datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC),
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )

    photo = Photo.objects.get(relative_path="apple-luis/IMG_0001.jpg")

    assert photo.status == "optional"
    assert photo.provenance == "apple-luis"
    assert photo.file_size == 123456
    assert photo.sha256 is None
    assert photo.phash is None
    assert photo.live_photo_video_path is None
    assert photo.status_changed_at is None
    assert photo.media_type == "image"
    assert photo.missing is False
    assert photo.indexed_at is not None


@pytest.mark.django_db
def test_relative_path_is_unique():
    from django.db import IntegrityError

    kwargs = dict(
        relative_path="dup.jpg",
        file_size=1,
        file_mtime=0.0,
        captured_at=datetime(2025, 1, 1, tzinfo=UTC),
        captured_at_source="file_mtime",
        media_type=Photo.MEDIA_IMAGE,
    )
    Photo.objects.create(**kwargs)
    with pytest.raises(IntegrityError):
        Photo.objects.create(**kwargs)

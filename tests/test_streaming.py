"""Range-request streaming (SPEC §10): parse_range unit tests plus a view
test exercising the `stream` URL against a fake video file with known
bytes, per PLAN T9 brief.
"""

from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from culler.core.models import Photo
from culler.core.streaming import (
    RangeNotSatisfiable,
    content_type_for,
    parse_range,
)

# --- parse_range --------------------------------------------------------


def test_parse_range_no_header_returns_none():
    assert parse_range(None, 1000) is None


def test_parse_range_simple_start_end():
    assert parse_range("bytes=0-99", 1000) == (0, 99)


def test_parse_range_open_ended_tail():
    assert parse_range("bytes=100-", 1000) == (100, 999)


def test_parse_range_suffix_last_n_bytes():
    assert parse_range("bytes=-500", 1000) == (500, 999)


def test_parse_range_suffix_larger_than_file_clamped_to_start():
    assert parse_range("bytes=-5000", 1000) == (0, 999)


def test_parse_range_end_clamped_to_file_size():
    assert parse_range("bytes=900-9999", 1000) == (900, 999)


def test_parse_range_missing_bytes_prefix_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("0-99", 1000)


def test_parse_range_multi_range_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-99,200-299", 1000)


def test_parse_range_start_beyond_file_size_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=1000-1001", 1000)


def test_parse_range_start_after_end_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=50-10", 1000)


def test_parse_range_non_numeric_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=abc-def", 1000)


def test_parse_range_empty_spec_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=-", 1000)


def test_parse_range_zero_size_file_unsatisfiable():
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-0", 0)


# --- content_type_for -----------------------------------------------------


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".mov", "video/quicktime"),
        (".MOV", "video/quicktime"),
        (".mp4", "video/mp4"),
        (".m4v", "video/mp4"),
        (".avi", "video/x-msvideo"),
        (".xyz", "application/octet-stream"),
    ],
)
def test_content_type_for(suffix, expected):
    assert content_type_for(Path(f"video{suffix}")) == expected


# --- stream view: full file / range slicing --------------------------------


def _touch(rel_path: str, content: bytes) -> Path:
    path = settings.WORKING_FOLDER / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _db_photo(relative_path: str, **overrides) -> Photo:
    from datetime import UTC, datetime

    kwargs = dict(
        status=Photo.STATUS_OPTIONAL,
        provenance="",
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
        captured_at_source="exif",
        media_type=Photo.MEDIA_VIDEO,
    )
    kwargs.update(overrides)
    return Photo.objects.create(relative_path=relative_path, **kwargs)


_VIDEO_BYTES = bytes(range(256)) * 4  # 1024 known bytes


@pytest.mark.django_db
def test_stream_no_range_header_returns_200_full_file(client):
    _touch("t_t9_stream_full/clip.mov", _VIDEO_BYTES)
    photo = _db_photo("t_t9_stream_full/clip.mov", provenance="t_t9_stream_full")

    response = client.get(reverse("stream", args=[photo.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "video/quicktime"
    assert response["Accept-Ranges"] == "bytes"
    assert response["Content-Length"] == str(len(_VIDEO_BYTES))
    assert b"".join(response.streaming_content) == _VIDEO_BYTES


@pytest.mark.django_db
def test_stream_range_bytes_0_99_returns_206_exact_slice(client):
    _touch("t_t9_stream_range/clip.mov", _VIDEO_BYTES)
    photo = _db_photo("t_t9_stream_range/clip.mov", provenance="t_t9_stream_range")

    response = client.get(reverse("stream", args=[photo.pk]), HTTP_RANGE="bytes=0-99")

    assert response.status_code == 206
    assert response["Content-Range"] == f"bytes 0-99/{len(_VIDEO_BYTES)}"
    assert response["Content-Length"] == "100"
    body = b"".join(response.streaming_content)
    assert body == _VIDEO_BYTES[0:100]


@pytest.mark.django_db
def test_stream_range_tail_open_ended(client):
    _touch("t_t9_stream_tail/clip.mov", _VIDEO_BYTES)
    photo = _db_photo("t_t9_stream_tail/clip.mov", provenance="t_t9_stream_tail")

    response = client.get(reverse("stream", args=[photo.pk]), HTTP_RANGE="bytes=1000-")

    assert response.status_code == 206
    total = len(_VIDEO_BYTES)
    assert response["Content-Range"] == f"bytes 1000-{total - 1}/{total}"
    body = b"".join(response.streaming_content)
    assert body == _VIDEO_BYTES[1000:]


@pytest.mark.django_db
def test_stream_range_unsatisfiable_returns_416(client):
    _touch("t_t9_stream_416/clip.mov", _VIDEO_BYTES)
    photo = _db_photo("t_t9_stream_416/clip.mov", provenance="t_t9_stream_416")

    response = client.get(
        reverse("stream", args=[photo.pk]), HTTP_RANGE=f"bytes={len(_VIDEO_BYTES) + 10}-"
    )

    assert response.status_code == 416
    assert response["Content-Range"] == f"bytes */{len(_VIDEO_BYTES)}"


@pytest.mark.django_db
def test_stream_unknown_pk_returns_404(client):
    response = client.get(reverse("stream", args=[999999]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_stream_missing_file_returns_404(client):
    photo = _db_photo("t_t9_stream_missing/clip.mov", provenance="t_t9_stream_missing")
    response = client.get(reverse("stream", args=[photo.pk]))
    assert response.status_code == 404


# --- stream view: Live Photo companion (?companion=1) ----------------------


@pytest.mark.django_db
def test_stream_companion_serves_paired_video_bytes(client):
    from datetime import UTC, datetime

    unique = "t_t9_stream_companion"
    image_bytes = b"jpeg-bytes-not-really"
    video_bytes = _VIDEO_BYTES
    _touch(f"{unique}/IMG.jpg", image_bytes)
    _touch(f"{unique}/IMG.mov", video_bytes)

    image = _db_photo(
        f"{unique}/IMG.jpg",
        provenance=unique,
        media_type=Photo.MEDIA_IMAGE,
        live_photo_video_path=f"{unique}/IMG.mov",
        captured_at=datetime(2025, 6, 14, 12, 0, 0, tzinfo=UTC),
    )

    response = client.get(reverse("stream", args=[image.pk]), {"companion": "1"})

    assert response.status_code == 200
    assert response["Content-Type"] == "video/quicktime"
    assert b"".join(response.streaming_content) == video_bytes


@pytest.mark.django_db
def test_stream_companion_without_pairing_returns_404(client):
    unique = "t_t9_stream_no_companion"
    _touch(f"{unique}/IMG.jpg", b"jpeg-bytes")
    image = _db_photo(f"{unique}/IMG.jpg", provenance=unique, media_type=Photo.MEDIA_IMAGE)

    response = client.get(reverse("stream", args=[image.pk]), {"companion": "1"})

    assert response.status_code == 404


@pytest.mark.django_db
def test_stream_without_companion_param_serves_own_file(client):
    unique = "t_t9_stream_own_file"
    _touch(f"{unique}/IMG.jpg", b"jpeg-bytes")
    _touch(f"{unique}/IMG.mov", b"companion-bytes")
    image = _db_photo(
        f"{unique}/IMG.jpg",
        provenance=unique,
        media_type=Photo.MEDIA_IMAGE,
        live_photo_video_path=f"{unique}/IMG.mov",
    )

    response = client.get(reverse("stream", args=[image.pk]))

    assert response.status_code == 200
    assert b"".join(response.streaming_content) == b"jpeg-bytes"

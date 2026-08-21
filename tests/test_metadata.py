import os
from datetime import UTC, datetime

import pytest
from PIL import ExifTags, Image

from culler.core.metadata import capture_datetime


def _make_jpeg(
    path,
    size: tuple[int, int] = (4, 4),
    datetime_original: str | None = None,
    datetime_tag: str | None = None,
) -> None:
    img = Image.new("RGB", size, color=(120, 40, 200))
    if datetime_original or datetime_tag:
        exif = img.getexif()
        if datetime_tag:
            exif[306] = datetime_tag
        if datetime_original:
            exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
            exif_ifd[36867] = datetime_original
        img.save(path, "jpeg", exif=exif)
    else:
        img.save(path, "jpeg")


def test_capture_datetime_exif_date_time_original(tmp_path):
    path = tmp_path / "photo.jpg"
    _make_jpeg(path, datetime_original="2025:06:14 18:30:12")

    dt, source = capture_datetime(path)

    assert source == "exif"
    expected = datetime(2025, 6, 14, 18, 30, 12).astimezone(UTC)
    assert dt == expected


def test_capture_datetime_exif_datetime_tag_fallback(tmp_path):
    # No DateTimeOriginal (36867) -- only base DateTime (306).
    path = tmp_path / "photo.jpg"
    _make_jpeg(path, datetime_tag="2024:01:02 03:04:05")

    dt, source = capture_datetime(path)

    assert source == "exif"
    expected = datetime(2024, 1, 2, 3, 4, 5).astimezone(UTC)
    assert dt == expected


def test_capture_datetime_exif_prefers_date_time_original(tmp_path):
    path = tmp_path / "photo.jpg"
    _make_jpeg(
        path,
        datetime_tag="2020:01:01 00:00:00",
        datetime_original="2025:06:14 18:30:12",
    )

    dt, source = capture_datetime(path)

    assert source == "exif"
    expected = datetime(2025, 6, 14, 18, 30, 12).astimezone(UTC)
    assert dt == expected


@pytest.mark.parametrize(
    "filename",
    [
        "IMG_20250614_183012.jpg",
        "20250614_183012.jpg",
        "2025-06-14 18.30.12.jpg",
        "2025-06-14_18-30-12.jpg",
        "PXL_20250614_183012.MP.jpg",
    ],
)
def test_capture_datetime_filename_patterns(tmp_path, filename):
    path = tmp_path / filename
    _make_jpeg(path)

    dt, source = capture_datetime(path)

    assert source == "filename"
    expected = datetime(2025, 6, 14, 18, 30, 12).astimezone(UTC)
    assert dt == expected


def test_capture_datetime_mtime_fallback(tmp_path):
    path = tmp_path / "random_name.jpg"
    _make_jpeg(path)
    os.utime(path, (1_700_000_000, 1_700_000_000))

    dt, source = capture_datetime(path)

    assert source == "file_mtime"
    assert dt == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_capture_datetime_corrupt_file_falls_back_to_mtime(tmp_path):
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not actually a jpeg")
    os.utime(path, (1_700_000_000, 1_700_000_000))

    dt, source = capture_datetime(path)

    assert source == "file_mtime"
    assert dt == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_capture_datetime_implausible_filename_year_falls_through(tmp_path):
    path = tmp_path / "30000101_010101.jpg"
    _make_jpeg(path)
    os.utime(path, (1_700_000_000, 1_700_000_000))

    dt, source = capture_datetime(path)

    assert source == "file_mtime"
    assert dt == datetime.fromtimestamp(1_700_000_000, tz=UTC)


def test_capture_datetime_missing_file_falls_back_to_mtime(tmp_path):
    # File doesn't exist -- Pillow open fails, filename doesn't match, and
    # mtime lookup would raise too. capture_datetime should not crash for
    # a nonexistent path; caller only ever passes existing files, but the
    # EXIF/filename stages must not blow up regardless.
    path = tmp_path / "IMG_20250614_183012.jpg"
    path.write_bytes(b"")

    dt, source = capture_datetime(path)

    assert source == "filename"
    expected = datetime(2025, 6, 14, 18, 30, 12).astimezone(UTC)
    assert dt == expected

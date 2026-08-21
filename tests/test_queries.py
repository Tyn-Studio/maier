from datetime import UTC, datetime

import pytest
from django.http import QueryDict

from culler.core import queries
from culler.core.models import Photo

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


def _qd(**params) -> QueryDict:
    qd = QueryDict(mutable=True)
    for key, value in params.items():
        qd[key] = value
    return qd


# --- filtered_photos: low-confidence dates filter (T13 item 2) -------------


@pytest.mark.django_db
def test_filtered_photos_dates_low_excludes_exif_sourced():
    exif_photo = _db_photo("q_dates/a.jpg", captured_at_source="exif")
    filename_photo = _db_photo(
        "q_dates/b.jpg",
        captured_at_source="filename",
        captured_at=datetime(2025, 6, 14, 19, 0, tzinfo=UTC),
    )
    mtime_photo = _db_photo(
        "q_dates/c.jpg",
        captured_at_source="file_mtime",
        captured_at=datetime(2025, 6, 14, 20, 0, tzinfo=UTC),
    )

    result = list(queries.filtered_photos(_qd(dates="low")))

    assert exif_photo not in result
    assert filename_photo in result
    assert mtime_photo in result


@pytest.mark.django_db
def test_filtered_photos_without_dates_param_includes_all_sources():
    exif_photo = _db_photo("q_dates_all/a.jpg", captured_at_source="exif")
    mtime_photo = _db_photo(
        "q_dates_all/b.jpg",
        captured_at_source="file_mtime",
        captured_at=datetime(2025, 6, 14, 19, 0, tzinfo=UTC),
    )

    result = list(queries.filtered_photos(_qd()))

    assert exif_photo in result
    assert mtime_photo in result


# --- total_photo_count ------------------------------------------------------


@pytest.mark.django_db
def test_total_photo_count_counts_all_regardless_of_filters():
    _db_photo("q_total/a.jpg")
    _db_photo("q_total/b.jpg", missing=True)

    assert queries.total_photo_count() == 2


@pytest.mark.django_db
def test_total_photo_count_zero_on_empty_db():
    assert queries.total_photo_count() == 0


# --- counts_by_status ---------------------------------------------------


@pytest.mark.django_db
def test_counts_by_status_fills_zero_for_absent_statuses():
    _db_photo("q_status/a.jpg", status=Photo.STATUS_OPTIONAL)
    _db_photo("q_status/selected/b.jpg", status=Photo.STATUS_SELECTED)
    _db_photo("q_status/selected/c.jpg", status=Photo.STATUS_SELECTED)

    counts = queries.counts_by_status()

    assert counts == {"optional": 1, "selected": 2, "rejected": 0}


@pytest.mark.django_db
def test_counts_by_status_empty_db():
    assert queries.counts_by_status() == {"optional": 0, "selected": 0, "rejected": 0}


# --- counts_by_provenance_status --------------------------------------------


@pytest.mark.django_db
def test_counts_by_provenance_status_matrix():
    _db_photo("q_prov/a.jpg", provenance="apple-luis", status=Photo.STATUS_OPTIONAL)
    _db_photo("q_prov/selected/b.jpg", provenance="apple-luis", status=Photo.STATUS_SELECTED)
    _db_photo("q_prov/root.jpg", provenance="", status=Photo.STATUS_OPTIONAL)

    rows = queries.counts_by_provenance_status()
    by_provenance = {row["provenance"]: row for row in rows}

    assert by_provenance["apple-luis"]["optional"] == 1
    assert by_provenance["apple-luis"]["selected"] == 1
    assert by_provenance["apple-luis"]["rejected"] == 0
    assert by_provenance["apple-luis"]["total"] == 2
    assert by_provenance["(root)"]["optional"] == 1


@pytest.mark.django_db
def test_counts_by_provenance_status_empty_db():
    assert queries.counts_by_provenance_status() == []


# --- selected_size_bytes / human_size ---------------------------------------


@pytest.mark.django_db
def test_selected_size_bytes_sums_only_selected():
    _db_photo("q_size/a.jpg", status=Photo.STATUS_SELECTED, file_size=1000)
    _db_photo("q_size/selected/b.jpg", status=Photo.STATUS_SELECTED, file_size=2000)
    _db_photo("q_size/optional/c.jpg", status=Photo.STATUS_OPTIONAL, file_size=5000)

    assert queries.selected_size_bytes() == 3000


@pytest.mark.django_db
def test_selected_size_bytes_zero_when_none_selected():
    _db_photo("q_size_zero/a.jpg", status=Photo.STATUS_OPTIONAL)

    assert queries.selected_size_bytes() == 0


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0 bytes"),
        (500, "500 bytes"),
        (2048, "2.00 KB"),
        (5 * 1024 * 1024, "5.00 MB"),
        (3 * 1024 * 1024 * 1024, "3.00 GB"),
    ],
)
def test_human_size_formats_binary_units(num_bytes, expected):
    assert queries.human_size(num_bytes) == expected


# --- recent_activity ---------------------------------------------------


@pytest.mark.django_db
def test_recent_activity_orders_newest_first_and_excludes_unchanged():
    older = _db_photo("q_activity/a.jpg", status_changed_at=datetime(2025, 1, 1, tzinfo=UTC))
    newer = _db_photo("q_activity/b.jpg", status_changed_at=datetime(2025, 6, 1, tzinfo=UTC))
    _db_photo("q_activity/c.jpg", status_changed_at=None)

    result = queries.recent_activity()

    assert result == [newer, older]


@pytest.mark.django_db
def test_recent_activity_respects_limit():
    for i in range(15):
        _db_photo(
            f"q_activity_limit/{i}.jpg",
            status_changed_at=datetime(2025, 1, 1 + i, tzinfo=UTC),
        )

    result = queries.recent_activity(limit=10)

    assert len(result) == 10


@pytest.mark.django_db
def test_recent_activity_empty_db():
    assert queries.recent_activity() == []

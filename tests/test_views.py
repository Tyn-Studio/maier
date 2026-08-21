from datetime import UTC, datetime

import pytest
from django.conf import settings
from django.urls import reverse

from culler.core import scan as scan_module
from culler.core import views as views_module
from culler.core.models import Photo

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _touch(rel_path: str, content: bytes = b"data"):
    path = settings.WORKING_FOLDER / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


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


# --- home -------------------------------------------------------------


@pytest.mark.django_db
def test_home_redirects_to_grid(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == reverse("grid")


# --- grid ---------------------------------------------------------------


@pytest.mark.django_db
def test_grid_renders_photos_and_day_headers(client):
    p1 = _db_photo(
        "t_grid_days/a.jpg",
        provenance="t_grid_days",
        captured_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )
    p2 = _db_photo(
        "t_grid_days/b.jpg",
        provenance="t_grid_days",
        captured_at=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
    )

    response = client.get(reverse("grid"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "Sat 14 Jun 2025" in body
    assert "Sun 15 Jun 2025" in body
    assert reverse("preview", args=[p1.pk]) in body
    assert reverse("preview", args=[p2.pk]) in body


@pytest.mark.django_db
def test_grid_status_filter(client):
    optional = _db_photo("t_grid_status/opt.jpg", provenance="t_grid_status")
    selected = _db_photo(
        "t_grid_status/selected/sel.jpg",
        provenance="t_grid_status",
        status=Photo.STATUS_SELECTED,
    )

    response = client.get(reverse("grid"), {"status": "selected"})

    body = response.content.decode()
    assert reverse("preview", args=[selected.pk]) in body
    assert reverse("preview", args=[optional.pk]) not in body


@pytest.mark.django_db
def test_grid_provenance_filter(client):
    a = _db_photo("t_grid_prov/a/x.jpg", provenance="a")
    b = _db_photo("t_grid_prov/b/y.jpg", provenance="b")

    response = client.get(reverse("grid"), {"provenance": "a"})

    body = response.content.decode()
    assert reverse("preview", args=[a.pk]) in body
    assert reverse("preview", args=[b.pk]) not in body


@pytest.mark.django_db
def test_grid_date_range_filter(client):
    early = _db_photo("t_grid_dates/early.jpg", captured_at=datetime(2025, 1, 1, tzinfo=UTC))
    mid = _db_photo("t_grid_dates/mid.jpg", captured_at=datetime(2025, 6, 14, tzinfo=UTC))
    late = _db_photo("t_grid_dates/late.jpg", captured_at=datetime(2025, 12, 31, tzinfo=UTC))

    response = client.get(reverse("grid"), {"from": "2025-06-01", "to": "2025-06-30"})

    body = response.content.decode()
    assert reverse("preview", args=[mid.pk]) in body
    assert reverse("preview", args=[early.pk]) not in body
    assert reverse("preview", args=[late.pk]) not in body


@pytest.mark.django_db
def test_grid_page2_with_hx_request_returns_partial(client):
    photos = [
        Photo(
            relative_path=f"t_grid_page/{i:04d}.jpg",
            status=Photo.STATUS_OPTIONAL,
            provenance="t_grid_page",
            file_size=1,
            file_mtime=1_700_000_000.0,
            captured_at=datetime(2025, 1, 1, tzinfo=UTC).replace(day=min(i % 28 + 1, 28)),
            captured_at_source="exif",
            media_type=Photo.MEDIA_IMAGE,
        )
        for i in range(201)
    ]
    Photo.objects.bulk_create(photos)

    response = client.get(reverse("grid"), {"page": 2}, headers={"HX-Request": "true"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "<html" not in body.lower()
    assert "<!doctype" not in body.lower()


# --- set-status -----------------------------------------------------------


@pytest.mark.django_db
def test_set_status_grid_context_moves_file_and_returns_cell_partial(client):
    _touch("t_set_status_grid/img.jpg")
    photo = _db_photo("t_set_status_grid/img.jpg", provenance="t_set_status_grid")

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "selected", "context": "grid"},
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    body = response.content.decode()
    assert f"cell-{photo.pk}" in body
    assert "status-selected" in body

    assert (settings.WORKING_FOLDER / "selected/t_set_status_grid/img.jpg").exists()
    assert not (settings.WORKING_FOLDER / "t_set_status_grid/img.jpg").exists()

    photo.refresh_from_db()
    assert photo.status == "selected"
    assert photo.relative_path == "selected/t_set_status_grid/img.jpg"


@pytest.mark.django_db
def test_set_status_review_context_returns_hx_redirect(client):
    _touch("t_set_status_review/img.jpg")
    photo = _db_photo("t_set_status_review/img.jpg", provenance="t_set_status_review")
    next_photo = _db_photo(
        "t_set_status_review/next.jpg",
        provenance="t_set_status_review",
        captured_at=datetime(2025, 6, 15, tzinfo=UTC),
    )
    _touch("t_set_status_review/next.jpg")

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {
            "status": "rejected",
            "context": "review",
            "next": str(next_photo.pk),
            "qs": "provenance=t_set_status_review",
        },
    )

    assert response.status_code == 200
    expected = reverse("review", args=[next_photo.pk]) + "?provenance=t_set_status_review"
    assert response["HX-Redirect"] == expected

    photo.refresh_from_db()
    assert photo.status == "rejected"


@pytest.mark.django_db
def test_set_status_invalid_status_returns_400(client):
    _touch("t_set_status_invalid/img.jpg")
    photo = _db_photo("t_set_status_invalid/img.jpg")

    response = client.post(reverse("set-status", args=[photo.pk]), {"status": "bogus"})

    assert response.status_code == 400


@pytest.mark.django_db
def test_set_status_unknown_pk_returns_404(client):
    response = client.post(reverse("set-status", args=[999999]), {"status": "selected"})
    assert response.status_code == 404


@pytest.mark.django_db
def test_set_status_vanished_file_returns_409(client):
    photo = _db_photo("t_set_status_missing/img.jpg")

    response = client.post(reverse("set-status", args=[photo.pk]), {"status": "selected"})

    assert response.status_code == 409


# --- scan-status / rescan --------------------------------------------------


@pytest.mark.django_db
def test_scan_status_returns_200(client):
    # A row already exists, so scan_status won't auto-start a real
    # background scan of the (shared, cross-test) working folder here.
    _db_photo("t_scan_status/a.jpg")

    response = client.get(reverse("scan-status"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_scan_status_auto_starts_when_no_photos_indexed(monkeypatch, client):
    calls = []

    def _fake_start(folder):
        calls.append(folder)
        return scan_module.ScanProgress(total=0, done=0, finished=True)

    monkeypatch.setattr(scan_module, "start_background_scan", _fake_start)

    response = client.get(reverse("scan-status"))

    assert response.status_code == 200
    assert calls == [settings.WORKING_FOLDER]


@pytest.mark.django_db
def test_rescan_calls_start_background_scan(client, monkeypatch):
    calls = []

    def _fake_start(folder):
        calls.append(folder)
        return scan_module.ScanProgress(total=0, done=0, finished=True)

    monkeypatch.setattr(scan_module, "start_background_scan", _fake_start)

    response = client.post(reverse("rescan"))

    assert response.status_code == 200
    assert calls == [settings.WORKING_FOLDER]


# --- review -----------------------------------------------------------


@pytest.mark.django_db
def test_review_renders_metadata_and_neighbours_respecting_filters(client):
    p1 = _db_photo(
        "t_review/a.jpg",
        provenance="t_review",
        captured_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
        file_size=111,
    )
    p2 = _db_photo(
        "t_review/b.jpg",
        provenance="t_review",
        captured_at=datetime(2025, 6, 14, 11, 0, tzinfo=UTC),
        file_size=222,
    )
    p3 = _db_photo(
        "t_review/c.jpg",
        provenance="t_review",
        captured_at=datetime(2025, 6, 14, 12, 0, tzinfo=UTC),
        file_size=333,
    )
    # a photo with a different provenance that must be excluded by the filter
    _db_photo(
        "t_review_other/z.jpg",
        provenance="t_review_other",
        captured_at=datetime(2025, 6, 14, 10, 30, tzinfo=UTC),
    )

    response = client.get(reverse("review", args=[p2.pk]), {"provenance": "t_review"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "t_review/b.jpg" in body
    assert "222 bytes" in body
    assert reverse("review", args=[p1.pk]) in body
    assert reverse("review", args=[p3.pk]) in body
    assert reverse("preview", args=[p1.pk]) in body
    assert reverse("preview", args=[p3.pk]) in body


@pytest.mark.django_db
def test_review_shows_low_confidence_warning_glyph(client):
    photo = _db_photo(
        "t_review_warn/a.jpg",
        captured_at_source="file_mtime",
    )

    response = client.get(reverse("review", args=[photo.pk]))

    assert response.status_code == 200
    assert "warn" in response.content.decode()


def test_in_flight_scan_progress_returns_none_by_default():
    assert views_module._in_flight_scan_progress() is None

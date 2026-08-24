import shutil
from datetime import UTC, datetime

import pytest
from django.conf import settings
from django.urls import reverse

from fixtures import build_fixture_folder
from maier.core import downloads as downloads_module
from maier.core import pull as pull_module
from maier.core import remote_state
from maier.core import scan as scan_module
from maier.core import views as views_module
from maier.core.models import DuplicatePair, Photo
from maier.core.phaseb import PhaseBProgress, run_phase_b
from maier.core.pull import PullProgress
from maier.core.scan import ScanProgress, scan

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _remote_db_photo(remote_id: str, account: str = "luis@example.com", **overrides) -> Photo:
    kwargs = dict(
        status=Photo.STATUS_OPTIONAL,
        provenance=remote_state.account_slug(account),
        file_size=1000,
        file_mtime=0.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
        remote_filename=f"{remote_id}.jpg",
    )
    kwargs.update(overrides)
    return Photo.objects.create(
        source=Photo.SOURCE_ICLOUD,
        account=account,
        remote_id=remote_id,
        relative_path=f"@icloud/{account}/{remote_id}",
        **kwargs,
    )


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


# --- remote (iCloud) set-status paths (T17) --------------------------------


@pytest.mark.django_db
def test_set_status_remote_reject_writes_state_no_disk_io(client):
    unique = "t_t17_remote_reject"
    slug = remote_state.account_slug("luis@example.com")
    photo = _remote_db_photo(f"r_{unique}", provenance=slug)

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "rejected", "context": "grid"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert f"cell-{photo.pk}" in body
    assert "status-rejected" in body

    assert not (settings.WORKING_FOLDER / "selected" / slug / f"r_{unique}.jpg").exists()
    assert not (settings.WORKING_FOLDER / "rejected" / slug / f"r_{unique}.jpg").exists()

    photo.refresh_from_db()
    assert photo.status == "rejected"
    assert photo.source == Photo.SOURCE_ICLOUD

    state = remote_state.load_state(settings.WORKING_FOLDER, "luis@example.com")
    assert state.decisions == {f"r_{unique}": "rejected"}


@pytest.mark.django_db
def test_set_status_remote_undecide_review_context_hx_redirect(client):
    unique = "t_t17_remote_undecide"
    slug = remote_state.account_slug("luis@example.com")
    photo = _remote_db_photo(f"r_{unique}", provenance=slug)
    remote_state.save_state(
        settings.WORKING_FOLDER,
        remote_state.AccountState(
            account="luis@example.com", decisions={f"r_{unique}": "rejected"}
        ),
    )

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "optional", "context": "review", "qs": f"provenance={slug}"},
    )

    assert response.status_code == 200
    assert response["HX-Redirect"] == f"{reverse('grid')}?provenance={slug}"

    photo.refresh_from_db()
    assert photo.status == "optional"

    state = remote_state.load_state(settings.WORKING_FOLDER, "luis@example.com")
    assert state.decisions == {}


@pytest.mark.django_db
def test_set_status_remote_select_flips_status_and_enqueues_download(client, monkeypatch):
    unique = "t_t17_remote_select"
    slug = remote_state.account_slug("luis@example.com")
    photo = _remote_db_photo(f"r_{unique}", provenance=slug)

    calls = []
    monkeypatch.setattr(downloads_module, "enqueue_original", lambda folder, p: calls.append(p.pk))

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {"status": "selected", "context": "grid"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "status-selected" in body

    photo.refresh_from_db()
    assert photo.status == "selected"
    # The download itself is async -- the row hasn't converted on this request.
    assert photo.source == Photo.SOURCE_ICLOUD
    assert calls == [photo.pk]
    assert not (settings.WORKING_FOLDER / "selected" / slug / f"r_{unique}.jpg").exists()


@pytest.mark.django_db
def test_set_status_remote_invalid_status_returns_400(client):
    unique = "t_t17_remote_invalid"
    photo = _remote_db_photo(f"r_{unique}")

    response = client.post(reverse("set-status", args=[photo.pk]), {"status": "bogus"})

    assert response.status_code == 400


@pytest.mark.django_db
def test_grid_annotates_download_pending_for_selected_remote_photo(client):
    unique = "t_t17_grid_pending"
    slug = remote_state.account_slug("luis@example.com")
    pending = _remote_db_photo(f"r_{unique}_pending", provenance=slug, status=Photo.STATUS_SELECTED)
    not_pending = _remote_db_photo(
        f"r_{unique}_optional",
        provenance=slug,
        status=Photo.STATUS_OPTIONAL,
        captured_at=_CAPTURED.replace(hour=19),
    )

    response = client.get(reverse("grid"), {"provenance": slug})

    groups = response.context["day_groups"]
    by_pk = {p.pk: p for g in groups for p in g["photos"]}
    assert by_pk[pending.pk].download_pending is True
    assert by_pk[not_pending.pk].download_pending is False


@pytest.mark.django_db
def test_review_annotates_download_pending_for_selected_remote_photo(client):
    unique = "t_t17_review_pending"
    slug = remote_state.account_slug("luis@example.com")
    photo = _remote_db_photo(f"r_{unique}", provenance=slug, status=Photo.STATUS_SELECTED)

    response = client.get(reverse("review", args=[photo.pk]))

    assert response.status_code == 200
    assert response.context["photo"].download_pending is True


# --- exact-dupe grouping (T7): grid hiding, badge, group cull -------------


@pytest.mark.django_db
def test_grid_hides_non_representative_and_shows_dupe_badge(client):
    unique = "t_phaseb_grid_dupe"
    sha = "1" * 64
    rep = _db_photo(
        f"{unique}/rep.jpg",
        provenance=unique,
        sha256=sha,
        captured_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )
    other = _db_photo(
        f"{unique}/other.jpg",
        provenance=unique,
        sha256=sha,
        captured_at=datetime(2025, 6, 14, 10, 5, tzinfo=UTC),
    )

    response = client.get(reverse("grid"), {"provenance": unique})

    assert response.status_code == 200
    body = response.content.decode()
    assert reverse("preview", args=[rep.pk]) in body
    assert reverse("preview", args=[other.pk]) not in body
    assert "&times;2" in body


@pytest.mark.django_db
def test_set_status_select_representative_auto_rejects_dupe_copy(client):
    unique = "t_phaseb_set_status_dupe"
    build_fixture_folder(settings.WORKING_FOLDER, {f"{unique}/rep.jpg": None})
    shutil.copy(
        settings.WORKING_FOLDER / f"{unique}/rep.jpg",
        settings.WORKING_FOLDER / f"{unique}/other.jpg",
    )
    scan(settings.WORKING_FOLDER, ScanProgress())
    Photo.objects.filter(relative_path__startswith=f"{unique}/").update(sha256=None)
    run_phase_b(settings.WORKING_FOLDER, PhaseBProgress())

    rep = Photo.objects.get(relative_path=f"{unique}/rep.jpg")
    other = Photo.objects.get(relative_path=f"{unique}/other.jpg")
    assert rep.sha256 == other.sha256

    response = client.post(
        reverse("set-status", args=[rep.pk]),
        {"status": "selected", "context": "grid"},
    )
    assert response.status_code == 200

    assert (settings.WORKING_FOLDER / f"selected/{unique}/rep.jpg").exists()
    other.refresh_from_db()
    assert other.status == Photo.STATUS_REJECTED
    assert (settings.WORKING_FOLDER / f"rejected/{unique}/other.jpg").exists()

    # unflag the representative: it's restored, the copy stays rejected --
    # SPEC §17.3, redundant copies are never auto-restored.
    response = client.post(
        reverse("set-status", args=[rep.pk]),
        {"status": "optional", "context": "grid"},
    )
    assert response.status_code == 200
    assert (settings.WORKING_FOLDER / f"{unique}/rep.jpg").exists()

    other.refresh_from_db()
    assert other.status == Photo.STATUS_REJECTED
    assert (settings.WORKING_FOLDER / f"rejected/{unique}/other.jpg").exists()


# --- dupes review (T8) -----------------------------------------------------


@pytest.mark.django_db
def test_dupes_renders_pair_with_metadata_and_count(client):
    unique = "t_t8_dupes_render"
    left = _db_photo(
        f"{unique}/left.jpg",
        provenance=unique,
        file_size=111,
        captured_at=datetime(2025, 6, 14, 10, 0, tzinfo=UTC),
    )
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        file_size=222,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=5)

    response = client.get(reverse("dupes"))

    assert response.status_code == 200
    body = response.content.decode()
    assert f"{unique}/left.jpg" in body
    assert f"{unique}/right.jpg" in body
    assert "111 bytes" in body
    assert "222 bytes" in body
    assert "Hamming distance: 5" in body
    assert "1 unresolved" in body


@pytest.mark.django_db
def test_dupes_empty_state_when_no_unresolved_pairs(client):
    response = client.get(reverse("dupes"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "No unresolved duplicates" in body
    assert "0 unresolved" in body


@pytest.mark.django_db
def test_resolve_pair_keep_left_moves_files_and_resolves(client):
    unique = "t_t8_resolve_keep_left"
    _touch(f"{unique}/left.jpg")
    _touch(f"{unique}/right.jpg")
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    pair = DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.post(reverse("resolve-pair", args=[pair.pk]), {"action": "keep_left"})

    assert response.status_code == 200
    assert response["HX-Redirect"] == f"{reverse('dupes')}?after={pair.pk}"

    pair.refresh_from_db()
    assert pair.resolved is True

    assert (settings.WORKING_FOLDER / f"selected/{unique}/left.jpg").exists()
    assert (settings.WORKING_FOLDER / f"rejected/{unique}/right.jpg").exists()
    assert not (settings.WORKING_FOLDER / f"{unique}/left.jpg").exists()
    assert not (settings.WORKING_FOLDER / f"{unique}/right.jpg").exists()


@pytest.mark.django_db
def test_resolve_pair_keep_right_moves_files_mirrored(client):
    unique = "t_t8_resolve_keep_right"
    _touch(f"{unique}/left.jpg")
    _touch(f"{unique}/right.jpg")
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    pair = DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.post(reverse("resolve-pair", args=[pair.pk]), {"action": "keep_right"})

    assert response.status_code == 200
    pair.refresh_from_db()
    assert pair.resolved is True
    assert (settings.WORKING_FOLDER / f"selected/{unique}/right.jpg").exists()
    assert (settings.WORKING_FOLDER / f"rejected/{unique}/left.jpg").exists()


@pytest.mark.django_db
def test_resolve_pair_keep_both_resolves_without_moving_files(client):
    unique = "t_t8_resolve_keep_both"
    _touch(f"{unique}/left.jpg")
    _touch(f"{unique}/right.jpg")
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    pair = DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.post(reverse("resolve-pair", args=[pair.pk]), {"action": "keep_both"})

    assert response.status_code == 200
    pair.refresh_from_db()
    assert pair.resolved is True

    left.refresh_from_db()
    right.refresh_from_db()
    assert left.status == Photo.STATUS_OPTIONAL
    assert right.status == Photo.STATUS_OPTIONAL
    assert (settings.WORKING_FOLDER / f"{unique}/left.jpg").exists()
    assert (settings.WORKING_FOLDER / f"{unique}/right.jpg").exists()


@pytest.mark.django_db
def test_resolve_pair_defer_does_not_resolve_and_navigates_to_next(client):
    unique = "t_t8_resolve_defer"
    photo_1a = _db_photo(f"{unique}/1a.jpg", provenance=unique)
    photo_1b = _db_photo(
        f"{unique}/1b.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    photo_2a = _db_photo(
        f"{unique}/2a.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 11, 0, tzinfo=UTC),
    )
    photo_2b = _db_photo(
        f"{unique}/2b.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 11, 0, 3, tzinfo=UTC),
    )
    pair1 = DuplicatePair.objects.create(photo_a=photo_1a, photo_b=photo_1b, hamming_distance=3)
    DuplicatePair.objects.create(photo_a=photo_2a, photo_b=photo_2b, hamming_distance=3)

    response = client.post(reverse("resolve-pair", args=[pair1.pk]), {"action": "defer"})

    assert response.status_code == 200
    assert response["HX-Redirect"] == f"{reverse('dupes')}?after={pair1.pk}"

    pair1.refresh_from_db()
    assert pair1.resolved is False

    # navigating to the redirect target shows the other pair, not the
    # deferred one (which is pushed to the back of the pk-ordered queue).
    next_response = client.get(response["HX-Redirect"])
    body = next_response.content.decode()
    assert f"{unique}/2a.jpg" in body
    assert f"{unique}/1a.jpg" not in body


@pytest.mark.django_db
def test_resolve_pair_unknown_pk_returns_404(client):
    response = client.post(reverse("resolve-pair", args=[999999]), {"action": "keep_left"})
    assert response.status_code == 404


@pytest.mark.django_db
def test_resolve_pair_unknown_action_returns_400(client):
    unique = "t_t8_resolve_bad_action"
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    pair = DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.post(reverse("resolve-pair", args=[pair.pk]), {"action": "bogus"})

    assert response.status_code == 400
    pair.refresh_from_db()
    assert pair.resolved is False


@pytest.mark.django_db
def test_resolve_pair_vanished_file_returns_409(client):
    unique = "t_t8_resolve_missing_file"
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)  # never touched on disk
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    pair = DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.post(reverse("resolve-pair", args=[pair.pk]), {"action": "keep_left"})

    assert response.status_code == 409
    pair.refresh_from_db()
    assert pair.resolved is False


@pytest.mark.django_db
def test_grid_shows_dupes_badge_when_unresolved_pairs_exist(client):
    unique = "t_t8_grid_badge"
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert "Dupes (1)" in body
    assert reverse("dupes") in body


@pytest.mark.django_db
def test_grid_hides_dupes_badge_when_no_unresolved_pairs(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert "dupes-badge" not in body


# --- scan-status / rescan --------------------------------------------------


@pytest.mark.django_db
def test_scan_status_idle_renders_inert_div(client):
    # A row already exists, so scan_status won't auto-start a real
    # background scan of the (shared, cross-test) working folder here.
    # Idle -> the response div carries NO hx attributes, ending the
    # recursive load-polling chain (T13 item 7).
    _db_photo("t_scan_status/a.jpg")

    response = client.get(reverse("scan-status"))

    body = response.content.decode()
    assert response.status_code == 200
    assert 'id="scan-poller"' in body
    assert "hx-get" not in body


@pytest.mark.django_db
def test_scan_status_in_flight_renders_live_poller(client, monkeypatch):
    _db_photo("t_scan_status_in_flight/a.jpg")
    in_flight = scan_module.ScanProgress(total=10, done=3, finished=False)
    monkeypatch.setattr(views_module, "_in_flight_scan_progress", lambda: in_flight)

    response = client.get(reverse("scan-status"))

    body = response.content.decode()
    assert response.status_code == 200
    assert "Indexing 3 / 10" in body
    assert 'id="scan-poller"' in body
    assert "load delay:2s" in body  # schedules the next poll


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


@pytest.mark.django_db
def test_rescan_response_contains_fresh_polling_div(client, monkeypatch):
    """T13 item 7: the rescan response must re-render the polling div (not
    just its contents) so a freshly-created #scan-poller element gets its
    own hx-trigger registration and polling resumes after a manual rescan.
    """

    def _fake_start(folder):
        return scan_module.ScanProgress(total=5, done=1, finished=False)

    monkeypatch.setattr(scan_module, "start_background_scan", _fake_start)

    response = client.post(reverse("rescan"))

    body = response.content.decode()
    assert 'id="scan-poller"' in body
    assert "hx-trigger" in body
    assert "Indexing 1 / 5" in body


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


# --- video / Live Photo badges (T9) ----------------------------------------


@pytest.mark.django_db
def test_grid_shows_video_glyph_badge(client):
    unique = "t_t9_grid_video_badge"
    video = _db_photo(f"{unique}/clip.mov", provenance=unique, media_type=Photo.MEDIA_VIDEO)

    response = client.get(reverse("grid"), {"provenance": unique})

    body = response.content.decode()
    assert reverse("preview", args=[video.pk]) in body
    assert "badge-video" in body


@pytest.mark.django_db
def test_grid_shows_live_badge_for_paired_image(client):
    unique = "t_t9_grid_live_badge"
    image = _db_photo(
        f"{unique}/img.jpg",
        provenance=unique,
        media_type=Photo.MEDIA_IMAGE,
        live_photo_video_path=f"{unique}/img.mov",
    )
    video = _db_photo(
        f"{unique}/img.mov",
        provenance=unique,
        media_type=Photo.MEDIA_VIDEO,
        captured_at=datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC),
    )

    response = client.get(reverse("grid"), {"provenance": unique})

    body = response.content.decode()
    assert reverse("preview", args=[image.pk]) in body
    assert reverse("preview", args=[video.pk]) not in body  # companion hidden
    assert "badge-live" in body


@pytest.mark.django_db
def test_set_status_grid_partial_shows_live_badge(client):
    unique = "t_t9_set_status_live_badge"
    _touch(f"{unique}/img.jpg")
    image = _db_photo(
        f"{unique}/img.jpg",
        provenance=unique,
        media_type=Photo.MEDIA_IMAGE,
        live_photo_video_path=f"{unique}/img.mov",
    )

    response = client.post(
        reverse("set-status", args=[image.pk]),
        {"status": "selected", "context": "grid"},
    )

    assert response.status_code == 200
    assert "badge-live" in response.content.decode()


@pytest.mark.django_db
def test_review_video_uses_stream_url(client):
    unique = "t_t9_review_video"
    photo = _db_photo(f"{unique}/clip.mov", provenance=unique, media_type=Photo.MEDIA_VIDEO)

    response = client.get(reverse("review", args=[photo.pk]))

    body = response.content.decode()
    assert reverse("stream", args=[photo.pk]) in body
    assert "<video" in body


@pytest.mark.django_db
def test_review_live_photo_renders_toggle_elements(client):
    unique = "t_t9_review_live"
    image = _db_photo(
        f"{unique}/img.jpg",
        provenance=unique,
        media_type=Photo.MEDIA_IMAGE,
        live_photo_video_path=f"{unique}/img.mov",
    )

    response = client.get(reverse("review", args=[image.pk]))

    body = response.content.decode()
    assert 'id="live-badge"' in body
    assert 'id="review-live-video"' in body
    assert f"{reverse('stream', args=[image.pk])}?companion=1" in body


@pytest.mark.django_db
def test_review_non_live_photo_has_no_toggle_elements(client):
    unique = "t_t9_review_not_live"
    photo = _db_photo(f"{unique}/img.jpg", provenance=unique, media_type=Photo.MEDIA_IMAGE)

    response = client.get(reverse("review", args=[photo.pk]))

    body = response.content.decode()
    assert 'id="live-badge"' not in body
    assert 'id="review-live-video"' not in body


# --- missing-file UX (T10) ---------------------------------------------


@pytest.mark.django_db
def test_grid_excludes_missing_by_default(client):
    unique = "t_t10_grid_default"
    present = _db_photo(f"{unique}/present.jpg", provenance=unique)
    missing = _db_photo(f"{unique}/gone.jpg", provenance=unique, missing=True)

    response = client.get(reverse("grid"), {"provenance": unique})

    body = response.content.decode()
    assert reverse("preview", args=[present.pk]) in body
    assert reverse("preview", args=[missing.pk]) not in body


@pytest.mark.django_db
def test_grid_show_missing_returns_only_missing(client):
    unique = "t_t10_grid_show_missing"
    present = _db_photo(f"{unique}/present.jpg", provenance=unique)
    missing = _db_photo(f"{unique}/gone.jpg", provenance=unique, missing=True)

    response = client.get(reverse("grid"), {"provenance": unique, "show": "missing"})

    body = response.content.decode()
    assert reverse("preview", args=[missing.pk]) in body
    assert reverse("preview", args=[present.pk]) not in body


@pytest.mark.django_db
def test_grid_missing_badge_appears_and_disappears(client):
    unique = "t_t10_grid_badge"
    response = client.get(reverse("grid"), {"provenance": unique})
    assert "missing-badge" not in response.content.decode()

    _db_photo(f"{unique}/gone.jpg", provenance=unique, missing=True)

    response = client.get(reverse("grid"), {"provenance": unique})
    body = response.content.decode()
    assert "missing-badge" in body
    assert "Missing (1)" in body

    response = client.get(reverse("grid"), {"provenance": unique, "show": "missing"})
    body = response.content.decode()
    assert "All photos" in body


@pytest.mark.django_db
def test_grid_missing_cell_has_no_action_buttons(client):
    unique = "t_t10_grid_missing_cell"
    photo = _db_photo(f"{unique}/gone.jpg", provenance=unique, missing=True)

    response = client.get(reverse("grid"), {"provenance": unique, "show": "missing"})

    body = response.content.decode()
    assert f'id="cell-{photo.pk}"' in body
    assert "cell-missing" in body
    assert "badge-missing" in body
    assert "cell-actions" not in body
    assert reverse("set-status", args=[photo.pk]) not in body


@pytest.mark.django_db
def test_review_missing_photo_renders_metadata_and_hides_actions(client):
    unique = "t_t10_review_missing"
    photo = _db_photo(f"{unique}/gone.jpg", provenance=unique, missing=True, file_size=555)

    response = client.get(reverse("review", args=[photo.pk]), {"show": "missing"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "555 bytes" in body
    assert "file is missing" in body
    assert 'id="action-select"' not in body
    assert 'id="action-reject"' not in body


# --- low-confidence date filter + glyph (T13 item 2) -----------------------


@pytest.mark.django_db
def test_grid_dates_low_filter_excludes_exif_sourced_photos(client):
    unique = "t_t13_dates_low"
    exif_dated = _db_photo(f"{unique}/exif.jpg", provenance=unique, captured_at_source="exif")
    low_trust = _db_photo(
        f"{unique}/mtime.jpg",
        provenance=unique,
        captured_at_source="file_mtime",
        captured_at=datetime(2025, 6, 14, 11, 0, tzinfo=UTC),
    )

    response = client.get(reverse("grid"), {"provenance": unique, "dates": "low"})

    body = response.content.decode()
    assert reverse("preview", args=[low_trust.pk]) in body
    assert reverse("preview", args=[exif_dated.pk]) not in body


@pytest.mark.django_db
def test_grid_shows_warn_glyph_for_low_confidence_dates(client):
    unique = "t_t13_grid_warn_glyph"
    _db_photo(f"{unique}/mtime.jpg", provenance=unique, captured_at_source="file_mtime")

    response = client.get(reverse("grid"), {"provenance": unique})

    assert "warn-glyph" in response.content.decode()


@pytest.mark.django_db
def test_grid_no_warn_glyph_for_exif_dates(client):
    unique = "t_t13_grid_no_warn_glyph"
    _db_photo(f"{unique}/exif.jpg", provenance=unique, captured_at_source="exif")

    response = client.get(reverse("grid"), {"provenance": unique})

    assert "warn-glyph" not in response.content.decode()


# --- empty states (T13 item 6) ---------------------------------------------


@pytest.mark.django_db
def test_grid_empty_folder_shows_no_photos_found(client, monkeypatch):
    monkeypatch.setattr(views_module, "_in_flight_scan_progress", lambda: None)

    response = client.get(reverse("grid"), {"provenance": "t_t13_empty_folder_never_used"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "No photos found in this folder." in body


@pytest.mark.django_db
def test_grid_empty_db_while_scanning_shows_indexing_message(client, monkeypatch):
    in_flight = scan_module.ScanProgress(total=100, done=1, finished=False)
    monkeypatch.setattr(views_module, "_in_flight_scan_progress", lambda: in_flight)

    response = client.get(reverse("grid"), {"provenance": "t_t13_scanning_empty_db"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "Indexing your photos" in body


@pytest.mark.django_db
def test_grid_filters_exclude_everything_shows_no_match_message(client):
    _db_photo("t_t13_no_match/a.jpg", provenance="t_t13_no_match")

    response = client.get(reverse("grid"), {"provenance": "t_t13_no_match_missing_provenance"})

    assert response.status_code == 200
    body = response.content.decode()
    assert "No photos match these filters." in body


@pytest.mark.django_db
def test_review_empty_db_unknown_pk_returns_404_not_500(client):
    response = client.get(reverse("review", args=[999999]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_dupes_empty_db_returns_200_not_500(client):
    response = client.get(reverse("dupes"))
    assert response.status_code == 200


# --- summary screen (T13 item 1) --------------------------------------------


@pytest.mark.django_db
def test_summary_empty_db_returns_200_not_500(client):
    response = client.get(reverse("summary"))

    assert response.status_code == 200
    body = response.content.decode()
    assert "No photos indexed yet." in body
    assert "No activity yet." in body


@pytest.mark.django_db
def test_summary_shows_counts_by_status_and_provenance(client):
    unique = "t_t13_summary"
    _db_photo(f"{unique}/a.jpg", provenance=unique, status=Photo.STATUS_OPTIONAL)
    _db_photo(
        f"{unique}/selected/b.jpg",
        provenance=unique,
        status=Photo.STATUS_SELECTED,
        file_size=2_000_000,
    )
    _db_photo(f"{unique}/rejected/c.jpg", provenance=unique, status=Photo.STATUS_REJECTED)

    response = client.get(reverse("summary"))

    assert response.status_code == 200
    body = response.content.decode()
    assert unique in body
    assert "MB" in body  # selected size, human-formatted


@pytest.mark.django_db
def test_summary_shows_recent_activity(client):
    photo = _db_photo(
        "t_t13_summary_activity/a.jpg",
        status=Photo.STATUS_SELECTED,
        status_changed_at=datetime(2025, 6, 14, 12, 0, tzinfo=UTC),
    )

    response = client.get(reverse("summary"))

    body = response.content.decode()
    assert photo.relative_path in body


@pytest.mark.django_db
def test_summary_shows_unresolved_dupes_and_missing_counts(client):
    left = _db_photo("t_t13_summary_dupes/left.jpg", provenance="t_t13_summary_dupes")
    right = _db_photo(
        "t_t13_summary_dupes/right.jpg",
        provenance="t_t13_summary_dupes",
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=3)
    _db_photo("t_t13_summary_missing/gone.jpg", provenance="t_t13_summary_missing", missing=True)

    response = client.get(reverse("summary"))

    body = response.content.decode()
    assert "<dt>Unresolved duplicates</dt><dd>1</dd>" in body
    assert "<dt>Missing files</dt><dd>1</dd>" in body


@pytest.mark.django_db
def test_grid_summary_link_present(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert reverse("summary") in body


# --- dupes zoom (T13 item 4) ------------------------------------------------


@pytest.mark.django_db
def test_dupes_images_wrapped_for_zoom_toggle(client):
    unique = "t_t13_dupes_zoom"
    left = _db_photo(f"{unique}/left.jpg", provenance=unique)
    right = _db_photo(
        f"{unique}/right.jpg",
        provenance=unique,
        captured_at=datetime(2025, 6, 14, 10, 0, 3, tzinfo=UTC),
    )
    DuplicatePair.objects.create(photo_a=left, photo_b=right, hamming_distance=4)

    response = client.get(reverse("dupes"))

    body = response.content.decode()
    assert "dupes-image-wrap" in body
    assert "onclick" in body
    assert 'id="action-undecide"' not in body


# --- accounts screen (T18, SPEC §18) ----------------------------------------


class _NoNetworkICloudClient:
    """Fake for the views-module test seam (T18 brief): raises if the
    accounts list page ever tries to construct a real client, guarding the
    "never touch the network just to render this page" requirement.
    """

    @classmethod
    def login(cls, email, password):
        raise AssertionError("accounts() must not construct an ICloudClient")

    @classmethod
    def from_session(cls, email):
        raise AssertionError("accounts() must not construct an ICloudClient")


@pytest.fixture(autouse=True)
def _t18_reset_pending_2fa():
    """`views_module._pending_2fa` is a module-global dict (single-user
    localhost app, per its own docstring) -- clear any stray entry a test
    left behind so it can't leak into an unrelated later test.
    """
    views_module._pending_2fa.clear()
    yield
    views_module._pending_2fa.clear()


@pytest.mark.django_db
def test_t18_accounts_lists_accounts_from_state_files_without_network(client, monkeypatch):
    monkeypatch.setattr(views_module, "ICloudClient", _NoNetworkICloudClient)
    email = "t_t18_list@example.com"
    remote_state.save_state(
        settings.WORKING_FOLDER, remote_state.AccountState(account=email, cursor=_CAPTURED)
    )

    response = client.get(reverse("accounts"))

    assert response.status_code == 200
    rows = response.context["accounts"]
    row = next(r for r in rows if r["email"] == email)
    assert row["last_pulled"] == _CAPTURED
    assert row["slug"] == remote_state.account_slug(email)
    assert "last pulled item" in response.content.decode()


@pytest.mark.django_db
def test_t18_accounts_shows_never_for_account_with_no_pull_yet(client, monkeypatch):
    monkeypatch.setattr(views_module, "ICloudClient", _NoNetworkICloudClient)
    email = "t_t18_never@example.com"
    remote_state.save_state(settings.WORKING_FOLDER, remote_state.AccountState(account=email))

    response = client.get(reverse("accounts"))

    assert "never" in response.content.decode()
    row = next(r for r in response.context["accounts"] if r["email"] == email)
    assert row["last_pulled"] is None


@pytest.mark.django_db
def test_t18_accounts_shows_total_and_pending_counts(client, monkeypatch):
    monkeypatch.setattr(views_module, "ICloudClient", _NoNetworkICloudClient)
    email = "t_t18_counts@example.com"
    remote_state.save_state(settings.WORKING_FOLDER, remote_state.AccountState(account=email))
    _remote_db_photo("r_t18_counts_1", account=email)
    _remote_db_photo(
        "r_t18_counts_2",
        account=email,
        status=Photo.STATUS_SELECTED,
        captured_at=_CAPTURED.replace(hour=20),
    )

    response = client.get(reverse("accounts"))

    row = next(r for r in response.context["accounts"] if r["email"] == email)
    assert row["total"] == 2
    assert row["pending"] == 1


# --- add-account form / 2FA --------------------------------------------------


@pytest.mark.django_db
def test_t18_account_login_success_creates_state_file_and_redirects(client, monkeypatch):
    email = "t_t18_login_ok@example.com"

    class _Client:
        def __init__(self, account):
            self.account = account

        @classmethod
        def login(cls, e, p):
            return cls(e)

    monkeypatch.setattr(views_module, "ICloudClient", _Client)
    pulled = []
    monkeypatch.setattr(
        views_module.pull, "start_background_pull", lambda folder, c: pulled.append(c.account)
    )

    response = client.post(reverse("account-login"), {"email": email, "password": "hunter2"})

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('accounts')}?added={email}"
    assert email in remote_state.list_accounts(settings.WORKING_FOLDER)
    # Successful attach auto-starts the first pull (an authenticated but
    # empty timeline was a UX trap -- 2026-08-24).
    assert pulled == [email]


@pytest.mark.django_db
def test_t18_account_login_two_factor_required_renders_form_and_stashes_client(client, monkeypatch):
    email = "t_t18_login_2fa@example.com"

    class _PendingClient:
        def __init__(self, account):
            self.account = account

    pending = _PendingClient(email)

    class _Client:
        @classmethod
        def login(cls, e, p):
            raise views_module.TwoFactorRequired(pending)

    monkeypatch.setattr(views_module, "ICloudClient", _Client)

    response = client.post(reverse("account-login"), {"email": email, "password": "hunter2"})

    assert response.status_code == 200
    body = response.content.decode()
    assert email in body
    assert 'name="code"' in body
    assert views_module._pending_2fa[email] is pending
    assert email not in remote_state.list_accounts(settings.WORKING_FOLDER)


@pytest.mark.django_db
def test_t18_account_login_icloud_error_shows_error_no_state_file(client, monkeypatch):
    email = "t_t18_login_err@example.com"

    class _Client:
        @classmethod
        def login(cls, e, p):
            raise views_module.ICloudError("invalid credentials")

    monkeypatch.setattr(views_module, "ICloudClient", _Client)

    response = client.post(reverse("account-login"), {"email": email, "password": "bad"})

    assert response.status_code == 200
    assert "invalid credentials" in response.content.decode()
    assert email not in remote_state.list_accounts(settings.WORKING_FOLDER)


@pytest.mark.django_db
def test_t18_account_2fa_success_creates_state_file_and_redirects(client, monkeypatch):
    email = "t_t18_2fa_ok@example.com"

    class _Pending:
        def __init__(self):
            self.account = email
            self.codes = []

        def submit_2fa(self, code):
            self.codes.append(code)
            return True

    pending = _Pending()
    views_module._pending_2fa[email] = pending
    pulled = []
    monkeypatch.setattr(
        views_module.pull, "start_background_pull", lambda folder, c: pulled.append(c.account)
    )

    response = client.post(reverse("account-2fa"), {"email": email, "code": "123456"})

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('accounts')}?added={email}"
    assert email in remote_state.list_accounts(settings.WORKING_FOLDER)
    assert email not in views_module._pending_2fa
    assert pending.codes == ["123456"]
    assert pulled == [email]


@pytest.mark.django_db
def test_t18_account_2fa_wrong_code_rerenders_form_client_still_pending(client):
    email = "t_t18_2fa_wrong@example.com"

    class _Pending:
        def submit_2fa(self, code):
            return False

    pending = _Pending()
    views_module._pending_2fa[email] = pending

    response = client.post(reverse("account-2fa"), {"email": email, "code": "000000"})

    assert response.status_code == 200
    assert "Incorrect verification code" in response.content.decode()
    assert views_module._pending_2fa[email] is pending


@pytest.mark.django_db
def test_t18_account_2fa_missing_pending_client_shows_error(client):
    email = "t_t18_2fa_missing@example.com"

    response = client.post(reverse("account-2fa"), {"email": email, "code": "123456"})

    assert response.status_code == 200
    assert "log in again" in response.content.decode()


# --- pull now ----------------------------------------------------------------


@pytest.mark.django_db
def test_t18_account_pull_valid_session_starts_pull_and_worker(client, monkeypatch):
    email = "t_t18_pull_ok@example.com"

    class _Client:
        def __init__(self, account):
            self.account = account

        @classmethod
        def from_session(cls, e):
            return cls(e)

    monkeypatch.setattr(views_module, "ICloudClient", _Client)

    pull_calls = []
    worker_calls = []
    monkeypatch.setattr(
        views_module.pull,
        "start_background_pull",
        lambda folder, c: pull_calls.append((folder, c.account)),
    )
    monkeypatch.setattr(
        views_module.downloads, "start_worker", lambda folder: worker_calls.append(folder)
    )

    response = client.post(reverse("account-pull"), {"account": email})

    assert response.status_code == 302
    assert response["Location"] == reverse("accounts")
    assert pull_calls == [(settings.WORKING_FOLDER, email)]
    assert worker_calls == [settings.WORKING_FOLDER]


@pytest.mark.django_db
def test_t18_account_pull_expired_session_shows_error_no_pull_started(client, monkeypatch):
    email = "t_t18_pull_expired@example.com"

    class _Client:
        @classmethod
        def from_session(cls, e):
            return None

    monkeypatch.setattr(views_module, "ICloudClient", _Client)

    calls = []
    monkeypatch.setattr(
        views_module.pull, "start_background_pull", lambda *a: calls.append(("pull", *a))
    )
    monkeypatch.setattr(
        views_module.downloads, "start_worker", lambda *a: calls.append(("worker", *a))
    )

    response = client.post(reverse("account-pull"), {"account": email})

    assert response.status_code == 200
    body = response.content.decode()
    assert "re-authenticate" in body
    assert email in body
    assert calls == []


# --- pull-status polling partial ---------------------------------------------


@pytest.mark.django_db
def test_t18_pull_status_in_flight_renders_progress_partial(client):
    email = "t_t18_pullstatus_inflight@example.com"
    pull_module._current_pulls[email] = PullProgress(account=email, total=5, done=2)
    try:
        response = client.get(reverse("pull-status"), {"account": email})
        body = response.content.decode()
        assert "Pulling 2 / 5" in body
        assert "load delay:2s" in body
    finally:
        pull_module._current_pulls.pop(email, None)


@pytest.mark.django_db
def test_t18_pull_status_idle_renders_inert_div(client):
    email = "t_t18_pullstatus_idle@example.com"
    pull_module._current_pulls.pop(email, None)

    response = client.get(reverse("pull-status"), {"account": email})

    body = response.content.decode()
    assert "load delay:2s" not in body
    assert "banner hidden" in body


# --- grid/review badges + nav link --------------------------------------------


@pytest.mark.django_db
def test_t18_grid_shows_cloud_badge_and_pending_badge(client):
    unique = "t_t18_grid_badges"
    email = f"{unique}@example.com"
    slug = remote_state.account_slug(email)
    _remote_db_photo(f"r_{unique}_cloud", account=email, provenance=slug)
    _remote_db_photo(
        f"r_{unique}_pending",
        account=email,
        provenance=slug,
        status=Photo.STATUS_SELECTED,
        captured_at=_CAPTURED.replace(hour=21),
    )

    response = client.get(reverse("grid"), {"provenance": slug})

    body = response.content.decode()
    assert body.count("badge-cloud") == 2
    assert "badge-pending" in body


@pytest.mark.django_db
def test_t18_grid_no_cloud_badge_for_local_photo(client):
    photo = _db_photo("t_t18_grid_local/img.jpg", provenance="t_t18_grid_local")

    response = client.get(reverse("grid"), {"provenance": "t_t18_grid_local"})

    body = response.content.decode()
    assert f"cell-{photo.pk}" in body
    assert "badge-cloud" not in body


@pytest.mark.django_db
def test_t18_review_shows_cloud_and_pending_badges(client):
    unique = "t_t18_review_badges"
    email = f"{unique}@example.com"
    slug = remote_state.account_slug(email)
    photo = _remote_db_photo(
        f"r_{unique}", account=email, provenance=slug, status=Photo.STATUS_SELECTED
    )

    response = client.get(reverse("review", args=[photo.pk]))

    body = response.content.decode()
    assert "badge-cloud" in body
    assert "badge-pending" in body


@pytest.mark.django_db
def test_t18_grid_shows_accounts_nav_link(client):
    response = client.get(reverse("grid"))

    assert f'href="{reverse("accounts")}"' in response.content.decode()

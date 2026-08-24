import shutil
from datetime import UTC, datetime

import pytest
from django.conf import settings
from django.urls import reverse
from PIL import Image

from fixtures import build_fixture_folder
from maier.core import disconnect as disconnect_module
from maier.core import downloads as downloads_module
from maier.core import export as export_module
from maier.core import folder_settings, remote_state
from maier.core import previews as previews_module
from maier.core import pull as pull_module
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


@pytest.fixture(autouse=True)
def _t29_default_working_range():
    """T29 added a setup-wizard gate on `grid`: an unset working range now
    redirects there instead of rendering the grid. Almost every test in this
    file predates that gate and hits `grid` expecting a normal 200 -- give
    each test an "everything" range up front (session-wide WORKING_FOLDER,
    see `test_integration.py`'s docstring for why this persists across
    tests) so they're unaffected. The handful of tests that specifically
    exercise the gate/setup-wizard/range-scoping behavior below monkeypatch
    `folder_settings.load_settings` or call `folder_settings.save_settings`
    themselves to override this default for their own duration.
    """
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="1970-01-01", working_to=""),
    )


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

    # T24 CTO decision: selected/ is flat -- no mirrored provenance subpath.
    assert (settings.WORKING_FOLDER / "selected/img.jpg").exists()
    assert not (settings.WORKING_FOLDER / "t_set_status_grid/img.jpg").exists()

    photo.refresh_from_db()
    assert photo.status == "selected"
    assert photo.relative_path == "selected/img.jpg"


@pytest.mark.django_db
def test_set_status_review_context_returns_status_pill_no_redirect(client):
    # PLAN T23 item 3: review no longer auto-advances -- the user stays on
    # the photo and only the status pill (#review-status) updates in place.
    _touch("t_set_status_review/img.jpg")
    photo = _db_photo("t_set_status_review/img.jpg", provenance="t_set_status_review")

    response = client.post(
        reverse("set-status", args=[photo.pk]),
        {
            "status": "rejected",
            "context": "review",
            "qs": "provenance=t_set_status_review",
        },
    )

    assert response.status_code == 200
    assert "HX-Redirect" not in response
    body = response.content.decode()
    assert 'id="review-status"' in body
    assert "Rejected" in body

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
def test_set_status_remote_undecide_review_context_returns_status_pill(client):
    # PLAN T23 item 3: same no-redirect behaviour applies to remote rows --
    # the durable state write still happens, but the response is the pill
    # partial rather than an HX-Redirect.
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
    assert "HX-Redirect" not in response
    body = response.content.decode()
    assert 'id="review-status"' in body
    assert "Optional" in body

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

    # T24 CTO decision: selected/ is flat; rejected/ (the redundant copy)
    # is untouched, still mirrored.
    assert (settings.WORKING_FOLDER / "selected/rep.jpg").exists()
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

    # T24 CTO decision: selected/ is flat; rejected/ is untouched, mirrored.
    assert (settings.WORKING_FOLDER / "selected/left.jpg").exists()
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
    # T24 CTO decision: selected/ is flat; rejected/ is untouched, mirrored.
    assert (settings.WORKING_FOLDER / "selected/right.jpg").exists()
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


# --- grid cell size slider (PLAN T23 item 4) --------------------------------
# JS behaviour (localStorage persistence, --cell CSS var, htmx-swap survival)
# is not exercised here -- server rendering is the only testable part.


@pytest.mark.django_db
def test_grid_renders_cell_size_slider(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert '<input type="range" id="cell-size"' in body
    assert 'min="120"' in body
    assert 'max="340"' in body


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


# T30: the standalone /accounts screen was merged into /settings -- the
# accounts section now renders as part of the Settings page, and /accounts
# itself is just a redirect there (see the dedicated test below).


@pytest.mark.django_db
def test_t18_accounts_lists_accounts_from_state_files_without_network(client, monkeypatch):
    monkeypatch.setattr(views_module, "ICloudClient", _NoNetworkICloudClient)
    email = "t_t18_list@example.com"
    remote_state.save_state(
        settings.WORKING_FOLDER, remote_state.AccountState(account=email, cursor=_CAPTURED)
    )

    response = client.get(reverse("settings"))

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

    response = client.get(reverse("settings"))

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

    response = client.get(reverse("settings"))

    row = next(r for r in response.context["accounts"] if r["email"] == email)
    assert row["total"] == 2
    assert row["pending"] == 1


@pytest.mark.django_db
def test_accounts_redirects_to_settings_preserving_querystring(client):
    response = client.get(reverse("accounts"), {"confirm": "someone@example.com"})

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('settings')}?confirm=someone%40example.com"


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
    assert response["Location"] == f"{reverse('settings')}?added={email}"
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
    assert response["Location"] == f"{reverse('settings')}?added={email}"
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
    assert response["Location"] == reverse("settings")
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
    pull_module._current_pulls[email] = PullProgress(account=email, scanned=120, total=5, done=2)
    try:
        response = client.get(reverse("pull-status"), {"account": email})
        body = response.content.decode()
        assert "120 scanned" in body
        assert "previews 2 / 5" in body
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
    # T30: iCloud accounts management moved into the merged Settings page --
    # the shared nav header links there instead of a dedicated /accounts.
    response = client.get(reverse("grid"))

    assert f'href="{reverse("settings")}"' in response.content.decode()


# --- disconnect account (T21, SPEC §18) ---------------------------------------


@pytest.fixture
def _t21_global_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "global-data"
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", data_dir)
    return data_dir


@pytest.mark.django_db
def test_t21_accounts_confirm_param_renders_confirm_block_without_deleting(client):
    email = "t_t21_confirm@example.com"
    remote_state.save_state(settings.WORKING_FOLDER, remote_state.AccountState(account=email))
    photo = _remote_db_photo("r_t21_confirm", account=email)

    response = client.get(reverse("settings"), {"confirm": email})

    body = response.content.decode()
    assert response.status_code == 200
    assert "Yes, disconnect" in body
    assert email in body
    assert "keeps everything already in" in body
    # Nothing deleted by the GET -- confirm is a separate step.
    assert Photo.objects.filter(pk=photo.pk).exists()


@pytest.mark.django_db
def test_t21_accounts_without_confirm_param_hides_confirm_block(client):
    email = "t_t21_noconfirm@example.com"
    remote_state.save_state(settings.WORKING_FOLDER, remote_state.AccountState(account=email))

    response = client.get(reverse("settings"))

    assert "Yes, disconnect" not in response.content.decode()


@pytest.mark.django_db
def test_t21_account_disconnect_post_removes_rows_and_previews_then_redirects(client):
    email = "t_t21_post@example.com"
    remote_state.save_state(settings.WORKING_FOLDER, remote_state.AccountState(account=email))
    photo = _remote_db_photo("r_t21_post", account=email)
    previews_dir = settings.WORKING_FOLDER / ".maier" / "previews"
    previews_dir.mkdir(parents=True, exist_ok=True)
    preview_path = previews_dir / f"icloud-{remote_state.account_slug(email)}-r_t21_post.jpg"
    preview_path.write_bytes(b"jpg")

    response = client.post(reverse("account-disconnect"), {"account": email})

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('settings')}?disconnected={email}"
    assert not Photo.objects.filter(pk=photo.pk).exists()
    assert not preview_path.exists()
    # Following the redirect shows the success message.
    follow = client.get(response["Location"])
    assert f"Disconnected {email}" in follow.content.decode()


@pytest.mark.django_db
def test_t21_account_disconnect_pull_in_flight_shows_error_nothing_deleted(client, monkeypatch):
    email = "t_t21_inflight@example.com"
    photo = _remote_db_photo("r_t21_inflight", account=email)
    monkeypatch.setattr(disconnect_module, "pull_in_flight", lambda account: True)

    response = client.post(reverse("account-disconnect"), {"account": email})

    assert response.status_code == 200
    assert "currently running" in response.content.decode()
    assert Photo.objects.filter(pk=photo.pk).exists()


@pytest.mark.django_db
def test_t21_account_disconnect_get_not_allowed(client):
    response = client.get(reverse("account-disconnect"))

    assert response.status_code == 405


@pytest.mark.django_db
def test_t21_account_disconnect_removes_session_dir(client, _t21_global_data_dir):
    email = "t_t21_session@example.com"
    session_dir = _t21_global_data_dir / "icloud-sessions" / remote_state.account_slug(email)
    session_dir.mkdir(parents=True)
    (session_dir / "cookie.txt").write_text("token")

    response = client.post(reverse("account-disconnect"), {"account": email})

    assert response.status_code == 302
    assert not session_dir.exists()


@pytest.mark.django_db
def test_t21_account_disconnect_keeps_state_file_and_local_selected_row(client):
    email = "t_t21_keep@example.com"
    state = remote_state.AccountState(account=email, decisions={"r_t21_keep_rejected": "rejected"})
    remote_state.save_state(settings.WORKING_FOLDER, state)
    slug = remote_state.account_slug(email)
    local_photo = Photo.objects.create(
        source=Photo.SOURCE_LOCAL,
        account=email,
        remote_id="r_t21_keep_downloaded",
        relative_path=f"selected/{slug}/r_t21_keep_downloaded.jpg",
        status=Photo.STATUS_SELECTED,
        provenance=slug,
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )

    response = client.post(reverse("account-disconnect"), {"account": email})

    assert response.status_code == 302
    reloaded_state = remote_state.load_state(settings.WORKING_FOLDER, email)
    assert reloaded_state.decisions == {"r_t21_keep_rejected": "rejected"}
    assert email in remote_state.list_accounts(settings.WORKING_FOLDER)
    local_photo.refresh_from_db()
    assert local_photo.source == Photo.SOURCE_LOCAL


# --- T22: on-demand sharp preview upgrade (review screen) -------------------


@pytest.mark.django_db
def test_review_remote_photo_enqueues_self_and_nearest_remote_neighbours(client, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        views_module.preview_upgrade,
        "enqueue_medium",
        lambda folder, photo: calls.append(photo.pk),
    )
    day = "2030-06-14"
    base = datetime(2030, 6, 14, 8, 0, tzinfo=UTC)

    def _at(minute):
        return base.replace(minute=minute)

    p_far_left = _remote_db_photo("t22_far_left", captured_at=_at(0))
    p_b = _remote_db_photo("t22_b", captured_at=_at(2))
    # A LOCAL photo sitting inside the ±3 window -- must be excluded from
    # the enqueue set even though it's a filmstrip-window neighbour.
    local_between = _db_photo("t22_local/mid.jpg", provenance="t22_local", captured_at=_at(4))
    p_c = _remote_db_photo("t22_c", captured_at=_at(6))
    target = _remote_db_photo("t22_target", captured_at=_at(8))
    p_d = _remote_db_photo("t22_d", captured_at=_at(10))
    p_e = _remote_db_photo("t22_e", captured_at=_at(12))
    p_f = _remote_db_photo("t22_f", captured_at=_at(14))
    p_far_right = _remote_db_photo("t22_far_right", captured_at=_at(16))

    response = client.get(reverse("review", args=[target.pk]), {"from": day, "to": day})

    assert response.status_code == 200
    expected = {target.pk, p_b.pk, p_c.pk, p_d.pk, p_e.pk, p_f.pk}
    assert set(calls) == expected
    assert local_between.pk not in calls
    assert p_far_left.pk not in calls
    assert p_far_right.pk not in calls


@pytest.mark.django_db
def test_review_remote_video_does_not_enqueue_sharp_upgrade(client, monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        views_module.preview_upgrade,
        "enqueue_medium",
        lambda folder, photo: calls.append(photo.pk),
    )
    video = _remote_db_photo("t22_video", media_type=Photo.MEDIA_VIDEO)

    response = client.get(reverse("review", args=[video.pk]))

    assert response.status_code == 200
    assert calls == []


@pytest.mark.django_db
def test_review_local_photo_has_no_poller_markup(client):
    photo = _db_photo("t22_local_no_poller.jpg")

    response = client.get(reverse("review", args=[photo.pk]))

    body = response.content.decode()
    assert 'id="review-still"' in body
    assert "review-sharp-wrap" not in body
    assert reverse("sharp-status", args=[photo.pk]) not in body


@pytest.mark.django_db
def test_preview_sharp_serves_thumb_no_store_then_medium_immutable(client):
    account = "luis@example.com"
    remote_id = "t22_sharp_serve"
    photo = _remote_db_photo(remote_id, account=account)
    thumb_dest = previews_module.remote_preview_dest(settings.WORKING_FOLDER, account, remote_id)
    thumb_dest.parent.mkdir(parents=True, exist_ok=True)
    thumb_dest.write_bytes(b"thumb-bytes")

    response = client.get(reverse("preview-sharp", args=[photo.pk]))
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert b"".join(response.streaming_content) == b"thumb-bytes"

    medium_dest = previews_module.remote_medium_dest(settings.WORKING_FOLDER, account, remote_id)
    medium_dest.write_bytes(b"medium-bytes")

    response = client.get(reverse("preview-sharp", args=[photo.pk]))
    assert response.status_code == 200
    assert response["Cache-Control"] == "public, max-age=31536000, immutable"
    assert b"".join(response.streaming_content) == b"medium-bytes"


@pytest.mark.django_db
def test_preview_sharp_placeholder_is_never_cached(client):
    photo = _remote_db_photo("t22_sharp_placeholder")

    response = client.get(reverse("preview-sharp", args=[photo.pk]))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_preview_sharp_local_photo_is_immutable(client):
    src = settings.WORKING_FOLDER / "t22_local_sharp.jpg"
    Image.new("RGB", (40, 30), (10, 20, 30)).save(src, "JPEG")
    photo = _db_photo("t22_local_sharp.jpg")

    response = client.get(reverse("preview-sharp", args=[photo.pk]))

    assert response.status_code == 200
    assert response["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.django_db
def test_sharp_status_polls_when_medium_absent(client):
    photo = _remote_db_photo("t22_status_poll")

    response = client.get(reverse("sharp-status", args=[photo.pk]))

    body = response.content.decode()
    assert "hx-get" in body
    assert reverse("sharp-status", args=[photo.pk]) in body
    assert "tries=1" in body
    assert reverse("preview-sharp", args=[photo.pk]) in body
    assert "v=medium" not in body


@pytest.mark.django_db
def test_sharp_status_returns_final_img_when_medium_ready(client):
    account = "luis@example.com"
    remote_id = "t22_status_ready"
    photo = _remote_db_photo(remote_id, account=account)
    medium_dest = previews_module.remote_medium_dest(settings.WORKING_FOLDER, account, remote_id)
    medium_dest.parent.mkdir(parents=True, exist_ok=True)
    medium_dest.write_bytes(b"medium")

    response = client.get(reverse("sharp-status", args=[photo.pk]))

    body = response.content.decode()
    assert "hx-get" not in body
    assert "v=medium" in body


@pytest.mark.django_db
def test_sharp_status_stops_polling_after_max_tries(client):
    photo = _remote_db_photo("t22_status_max_tries")

    response = client.get(reverse("sharp-status", args=[photo.pk]), {"tries": 15})

    body = response.content.decode()
    assert "hx-get" not in body
    assert "v=medium" not in body


# --- T25 settings / export --------------------------------------------------


@pytest.fixture(autouse=True)
def _t25_reset_export_state():
    """`export_module._current_export` is a module-global (mirrors
    scan.py's `_current_scan`) -- clear it so an in-flight/finished run
    from an earlier test can't leak into a later "idle" assertion.
    """
    export_module._current_export = None
    yield
    export_module._current_export = None


@pytest.mark.django_db
def test_settings_page_renders_defaults(client):
    response = client.get(reverse("settings"))

    assert response.status_code == 200
    body = response.content.decode()
    assert 'name="export_destination"' in body
    assert 'name="export_mode"' in body
    assert 'name="export_date_prefix"' in body


@pytest.mark.django_db
def test_settings_page_post_saves_settings(client):
    response = client.post(
        reverse("settings"),
        {
            "export_destination": "/Volumes/Backup/export",
            "export_mode": "automatic",
            "export_date_prefix": "on",
            "export_date_prefix_submitted": "1",
        },
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("settings")

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.export_destination == "/Volumes/Backup/export"
    assert saved.export_mode == "automatic"
    assert saved.export_date_prefix is True


@pytest.mark.django_db
def test_settings_page_post_manual_mode_unchecked_date_prefix(client):
    client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
    )

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.export_mode == "manual"
    assert saved.export_date_prefix is False


@pytest.mark.django_db
def test_settings_page_post_date_prefix_checkbox_round_trip_via_hidden_marker(client):
    # Check it on -- the marker is present alongside the checkbox both times
    # (matches the real form, PLAN T31), only the checkbox's own presence
    # differs.
    client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix": "on",
            "export_date_prefix_submitted": "1",
        },
    )
    assert folder_settings.load_settings(settings.WORKING_FOLDER).export_date_prefix is True

    # Uncheck it -- browsers omit an unchecked checkbox from POST entirely,
    # so only the hidden marker distinguishes this from "a different
    # section's form was submitted" (which must leave date_prefix alone).
    client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
    )
    assert folder_settings.load_settings(settings.WORKING_FOLDER).export_date_prefix is False


@pytest.mark.django_db
def test_settings_page_shows_saved_values_on_reload(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(export_destination="/x/y", export_mode="automatic"),
    )

    response = client.get(reverse("settings"))

    body = response.content.decode()
    assert "/x/y" in body


@pytest.mark.django_db
def test_settings_page_post_export_fields_preserves_working_range(client):
    # PLAN T31: buttonless auto-save means a POST from the export section's
    # form only ever carries export fields -- the previously-saved working
    # range must survive untouched (this is the "wipe bug" T30 fixed,
    # regression-tested here for the new partial-update code path).
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2026-01-01", working_to="2026-02-01"),
    )

    client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
    )

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2026-01-01"
    assert saved.working_to == "2026-02-01"
    assert saved.export_destination == "/dest"


@pytest.mark.django_db
def test_settings_page_post_working_range_preserves_export_fields(client):
    # The reverse direction of the same regression: a POST from the
    # working-range form only carries working_from/working_to -- the
    # previously-saved export settings must survive untouched.
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(
            export_destination="/keep/me", export_mode="automatic", export_date_prefix=True
        ),
    )

    client.post(
        reverse("settings"),
        {"working_from": "2026-03-01", "working_to": "2026-04-01"},
    )

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.export_destination == "/keep/me"
    assert saved.export_mode == "automatic"
    assert saved.export_date_prefix is True
    assert saved.working_from == "2026-03-01"
    assert saved.working_to == "2026-04-01"


@pytest.mark.django_db
def test_settings_page_htmx_post_returns_saved_indicator_partial(client):
    response = client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "save-indicator" in body
    assert "Saved" in body
    # The full page (nav, other sections) is not re-rendered for an htmx
    # partial-save response.
    assert "<h1>Settings</h1>" not in body


@pytest.mark.django_db
def test_settings_page_non_htmx_post_still_redirects_to_full_page(client):
    response = client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("settings")
    follow_up = client.get(response["Location"])
    assert "<h1>Settings</h1>" in follow_up.content.decode()


@pytest.mark.django_db
def test_settings_page_post_working_range_change_kicks_pull_for_live_sessions(client, monkeypatch):
    from types import SimpleNamespace

    live_email = "t_t31_range_live@example.com"
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [live_email])
    calls = []
    monkeypatch.setattr(
        views_module.ICloudClient,
        "from_session",
        staticmethod(lambda email: SimpleNamespace(account=email)),
    )
    monkeypatch.setattr(
        views_module.pull,
        "start_background_pull",
        lambda folder, client: calls.append((folder, client.account)),
    )

    client.post(reverse("settings"), {"working_from": "2026-05-01", "working_to": "2026-06-01"})

    assert calls == [(settings.WORKING_FOLDER, live_email)]


@pytest.mark.django_db
def test_settings_page_post_working_range_unchanged_does_not_kick_pull(client, monkeypatch):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2026-05-01", working_to="2026-06-01"),
    )
    calls = []
    monkeypatch.setattr(
        views_module.pull,
        "start_background_pull",
        lambda folder, client: calls.append((folder, client)),
    )

    client.post(reverse("settings"), {"working_from": "2026-05-01", "working_to": "2026-06-01"})

    assert calls == []


@pytest.mark.django_db
def test_settings_page_post_export_only_does_not_kick_pull(client, monkeypatch):
    calls = []
    monkeypatch.setattr(
        views_module.pull,
        "start_background_pull",
        lambda folder, client: calls.append((folder, client)),
    )

    client.post(
        reverse("settings"),
        {
            "export_destination": "/dest",
            "export_mode": "manual",
            "export_date_prefix_submitted": "1",
        },
    )

    assert calls == []


@pytest.mark.django_db
def test_settings_page_has_exactly_one_add_account_email_input(client):
    response = client.get(reverse("settings"))

    body = response.content.decode()
    assert body.count('type="email"') == 1
    assert body.count('name="email"') == 1


@pytest.mark.django_db
def test_settings_page_has_no_save_settings_button(client):
    response = client.get(reverse("settings"))

    body = response.content.decode()
    assert "Save settings" not in body
    assert "Save custom range" not in body


@pytest.mark.django_db
def test_export_now_no_destination_redirects_to_settings_with_notice(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER, folder_settings.FolderSettings(export_destination="")
    )

    response = client.post(reverse("export-now"))

    assert response.status_code == 302
    assert response["Location"].startswith(reverse("settings"))
    assert "notice=" in response["Location"]


@pytest.mark.django_db
def test_export_now_get_not_allowed(client):
    response = client.get(reverse("export-now"))
    assert response.status_code == 405


@pytest.mark.django_db
def test_export_now_starts_background_export_with_configured_destination(client, monkeypatch):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(
            export_destination="/tmp/somewhere", export_mode="manual", export_date_prefix=True
        ),
    )
    calls = []

    def _fake_start(folder, dest, *, date_prefix=False):
        calls.append((folder, dest, date_prefix))
        return export_module.ExportProgress(finished=True, copied=3, skipped=1)

    monkeypatch.setattr(export_module, "start_background_export", _fake_start)

    response = client.post(reverse("export-now"))

    assert response.status_code == 302
    assert response["Location"] == reverse("settings")
    assert len(calls) == 1
    called_folder, called_dest, called_date_prefix = calls[0]
    assert called_folder == settings.WORKING_FOLDER
    assert str(called_dest) == "/tmp/somewhere"
    assert called_date_prefix is True


@pytest.mark.django_db
def test_export_status_in_flight_renders_poller(client):
    export_module._current_export = export_module.ExportProgress(copied=2, skipped=0)

    response = client.get(reverse("export-status"))

    body = response.content.decode()
    assert "load delay:2s" in body
    assert "2 copied" in body


@pytest.mark.django_db
def test_export_status_finished_renders_final_banner_and_stops_polling(client):
    export_module._current_export = export_module.ExportProgress(
        finished=True, copied=5, skipped=2, errors=["boom"]
    )

    response = client.get(reverse("export-status"))

    body = response.content.decode()
    assert "load delay:2s" not in body
    assert "5 copied" in body
    assert "2 skipped" in body
    assert "boom" in body


@pytest.mark.django_db
def test_export_status_idle_renders_inert_div(client):
    response = client.get(reverse("export-status"))

    body = response.content.decode()
    assert "load delay:2s" not in body
    assert "banner hidden" in body


@pytest.mark.django_db
def test_grid_has_export_and_settings_links(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert f'href="{reverse("settings")}"' in body
    assert f'action="{reverse("export-now")}"' in body


# --- update notification banner (PLAN T27) ----------------------------------


# T30: the update banner moved out of grid()'s own context into the shared
# `nav_context` context processor (base.html's header, present on every
# page) -- it's `context_processors.updates`, not `views_module.updates`,
# that's actually called now.


@pytest.mark.django_db
def test_grid_shows_update_banner_when_available(client, monkeypatch):
    from maier.core import context_processors as context_processors_module
    from maier.core import updates as updates_module

    monkeypatch.setattr(
        context_processors_module.updates,
        "latest_known_update",
        lambda: updates_module.UpdateInfo(
            version="9.9.9", url="https://github.com/Tyn-Studio/maier/releases/tag/v9.9.9"
        ),
    )

    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert "banner update-available" in body
    assert "Maier 9.9.9 is available" in body
    assert 'href="https://github.com/Tyn-Studio/maier/releases/tag/v9.9.9"' in body


@pytest.mark.django_db
def test_grid_hides_update_banner_when_absent(client, monkeypatch):
    from maier.core import context_processors as context_processors_module

    monkeypatch.setattr(context_processors_module.updates, "latest_known_update", lambda: None)

    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert "update-available" not in body


@pytest.mark.django_db
def test_review_has_no_update_banner_markup(client):
    # Review keeps its own immersive top bar (PLAN T23/T30) -- the shared
    # header (and its update banner) is suppressed there.
    photo = _db_photo("t_t30_review_no_header.jpg")

    response = client.get(reverse("review", args=[photo.pk]))

    body = response.content.decode()
    assert "app-header" not in body


# --- setup wizard / working date range gate (PLAN T29) ----------------------


@pytest.mark.django_db
def test_grid_redirects_to_setup_when_working_range_unset(client, monkeypatch):
    monkeypatch.setattr(
        views_module.folder_settings,
        "load_settings",
        lambda folder: folder_settings.FolderSettings(),
    )

    response = client.get(reverse("grid"))

    assert response.status_code == 302
    assert response.url == reverse("setup")


@pytest.mark.django_db
def test_grid_no_redirect_when_working_range_set(client):
    # Relies on the module's own autouse fixture above.
    response = client.get(reverse("grid"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_other_pages_never_gated_by_working_range(client, monkeypatch):
    monkeypatch.setattr(
        views_module.folder_settings,
        "load_settings",
        lambda folder: folder_settings.FolderSettings(),
    )

    # /accounts is a redirect (T30), not a page render, but it must still
    # never bounce to /setup regardless of the working-range gate.
    assert client.get(reverse("accounts")).status_code == 302
    assert client.get(reverse("settings")).status_code == 200
    assert client.get(reverse("healthz")).status_code == 200
    assert client.get(reverse("setup")).status_code == 200


@pytest.mark.django_db
def test_grid_defaults_to_working_range_when_params_absent(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2025-06-01", working_to="2025-06-30"),
    )
    in_range = _db_photo("t_t29_default/in.jpg", captured_at=datetime(2025, 6, 14, tzinfo=UTC))
    out_of_range = _db_photo("t_t29_default/out.jpg", captured_at=datetime(2025, 1, 1, tzinfo=UTC))

    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert reverse("preview", args=[in_range.pk]) in body
    assert reverse("preview", args=[out_of_range.pk]) not in body
    assert response.context["filter_from"] == "2025-06-01"
    assert response.context["filter_to"] == "2025-06-30"


@pytest.mark.django_db
def test_grid_explicit_param_overrides_working_range_default(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2025-06-01", working_to="2025-06-30"),
    )
    outside_default = _db_photo(
        "t_t29_explicit/x.jpg", captured_at=datetime(2025, 1, 1, tzinfo=UTC)
    )

    response = client.get(reverse("grid"), {"from": "2025-01-01", "to": "2025-01-31"})

    body = response.content.decode()
    assert reverse("preview", args=[outside_default.pk]) in body
    assert response.context["filter_from"] == "2025-01-01"
    assert response.context["filter_to"] == "2025-01-31"


@pytest.mark.django_db
def test_grid_explicit_empty_param_wins_over_working_range_default(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2025-06-01", working_to="2025-06-30"),
    )
    outside_range = _db_photo("t_t29_cleared/x.jpg", captured_at=datetime(2020, 1, 1, tzinfo=UTC))

    response = client.get(reverse("grid"), {"from": "", "to": ""})

    body = response.content.decode()
    assert reverse("preview", args=[outside_range.pk]) in body
    assert response.context["filter_from"] == ""
    assert response.context["filter_to"] == ""


@pytest.mark.django_db
def test_grid_shows_working_range_indicator(client):
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2026-02-01", working_to="2026-03-17"),
    )

    response = client.get(reverse("grid"))

    body = response.content.decode()
    assert "Working: 2026-02-01" in body
    assert "2026-03-17" in body
    assert f'href="{reverse("setup")}?step=2"' in body


# --- setup wizard steps (PLAN T29) -------------------------------------------


@pytest.mark.django_db
def test_setup_shows_step1_when_no_accounts(client, monkeypatch):
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [])

    response = client.get(reverse("setup"))

    assert response.context["show_step1"] is True
    assert "Step 1 of 2" in response.content.decode()


@pytest.mark.django_db
def test_setup_shows_step2_directly_when_accounts_exist(client, monkeypatch):
    monkeypatch.setattr(
        views_module.remote_state, "list_accounts", lambda folder: ["a@example.com"]
    )

    response = client.get(reverse("setup"))

    assert response.context["show_step1"] is False
    assert "Step 2 of 2" in response.content.decode()


@pytest.mark.django_db
def test_setup_step2_via_query_param_even_with_no_accounts(client, monkeypatch):
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [])

    response = client.get(reverse("setup"), {"step": "2"})

    assert response.context["show_step1"] is False


@pytest.mark.django_db
def test_setup_prefills_current_working_range(client, monkeypatch):
    monkeypatch.setattr(
        views_module.remote_state, "list_accounts", lambda folder: ["a@example.com"]
    )
    folder_settings.save_settings(
        settings.WORKING_FOLDER,
        folder_settings.FolderSettings(working_from="2026-02-01", working_to="2026-03-17"),
    )

    response = client.get(reverse("setup"))

    body = response.content.decode()
    assert 'value="2026-02-01"' in body
    assert 'value="2026-03-17"' in body


# --- setup-dates POST (PLAN T29) ---------------------------------------------


@pytest.mark.django_db
def test_setup_dates_get_not_allowed(client):
    response = client.get(reverse("setup-dates"))
    assert response.status_code == 405


@pytest.mark.django_db
def test_setup_dates_preset_everything_saves_sentinel_and_redirects_to_grid(client):
    response = client.post(reverse("setup-dates"), {"preset": "everything"})

    assert response.status_code == 302
    assert response.url == reverse("grid")
    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "1970-01-01"
    assert saved.working_to == ""


@pytest.mark.django_db
def test_setup_dates_custom_range_saves_given_values(client):
    response = client.post(reverse("setup-dates"), {"from": "2026-02-01", "to": "2026-03-17"})

    assert response.status_code == 302
    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2026-02-01"
    assert saved.working_to == "2026-03-17"


@pytest.mark.django_db
def test_setup_dates_preset_last_month_computed_server_side(client, monkeypatch):
    monkeypatch.setattr(
        views_module.timezone, "now", lambda: datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    )

    client.post(reverse("setup-dates"), {"preset": "last_month"})

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2026-02-15"
    assert saved.working_to == ""


@pytest.mark.django_db
def test_setup_dates_preset_last_3_months_computed_server_side(client, monkeypatch):
    monkeypatch.setattr(
        views_module.timezone, "now", lambda: datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    )

    client.post(reverse("setup-dates"), {"preset": "last_3_months"})

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2025-12-15"


@pytest.mark.django_db
def test_setup_dates_preset_last_year_computed_server_side(client, monkeypatch):
    monkeypatch.setattr(
        views_module.timezone, "now", lambda: datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    )

    client.post(reverse("setup-dates"), {"preset": "last_year"})

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2025-03-15"


@pytest.mark.django_db
def test_setup_dates_preset_clamps_day_of_month(client, monkeypatch):
    monkeypatch.setattr(
        views_module.timezone, "now", lambda: datetime(2026, 3, 31, 12, 0, tzinfo=UTC)
    )

    client.post(reverse("setup-dates"), {"preset": "last_month"})

    saved = folder_settings.load_settings(settings.WORKING_FOLDER)
    assert saved.working_from == "2026-02-28"  # Feb has no 31st


@pytest.mark.django_db
def test_setup_dates_kicks_pull_for_accounts_with_live_session_only(client, monkeypatch):
    from types import SimpleNamespace

    live_email = "t_setup_live@example.com"
    dead_email = "t_setup_dead@example.com"
    monkeypatch.setattr(
        views_module.remote_state, "list_accounts", lambda folder: [live_email, dead_email]
    )

    class _FakeICloudClient:
        @staticmethod
        def from_session(email):
            return SimpleNamespace(account=email) if email == live_email else None

    monkeypatch.setattr(views_module, "ICloudClient", _FakeICloudClient)

    calls = []
    monkeypatch.setattr(
        views_module.pull,
        "start_background_pull",
        lambda folder, client: calls.append((folder, client.account)),
    )

    response = client.post(reverse("setup-dates"), {"preset": "everything"})

    assert response.status_code == 302
    assert calls == [(settings.WORKING_FOLDER, live_email)]


# --- T30: merged Settings page, shared nav header, native folder picker -----


@pytest.mark.django_db
def test_setup_dates_next_settings_redirects_to_settings(client):
    response = client.post(reverse("setup-dates"), {"preset": "everything", "next": "settings"})

    assert response.status_code == 302
    assert response.url == reverse("settings")


@pytest.mark.django_db
def test_nav_header_present_on_grid_settings_summary_dupes(client):
    for name in ("grid", "settings", "summary", "dupes"):
        response = client.get(reverse(name))
        body = response.content.decode()
        assert 'class="app-header"' in body, name
        assert f'href="{reverse("grid")}"' in body, name
        assert f'href="{reverse("summary")}"' in body, name
        assert f'href="{reverse("dupes")}"' in body, name
        assert f'href="{reverse("settings")}"' in body, name


@pytest.mark.django_db
def test_nav_header_absent_on_review(client):
    photo = _db_photo("t_t30_review_header.jpg")

    response = client.get(reverse("review", args=[photo.pk]))

    assert 'class="app-header"' not in response.content.decode()


@pytest.mark.django_db
def test_grid_filter_bar_no_longer_has_moved_nav_links(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    filter_bar = body.split('id="filter-bar"', 1)[1].split("</div>", 1)[0]
    assert "summary-link" not in filter_bar
    assert "accounts-link" not in filter_bar
    assert "settings-link" not in filter_bar
    assert "working-range-link" not in filter_bar
    assert "dupes-badge" not in filter_bar


@pytest.mark.django_db
def test_grid_filter_bar_keeps_actual_filters_rescan_export_and_size(client):
    response = client.get(reverse("grid"))

    body = response.content.decode()
    filter_bar = body.split('id="filter-bar"', 1)[1].split("</div>", 1)[0]
    assert 'name="status"' in filter_bar
    assert 'name="provenance"' in filter_bar
    assert 'name="from"' in filter_bar
    assert 'name="to"' in filter_bar
    assert 'id="cell-size"' in filter_bar
    assert f'hx-post="{reverse("rescan")}"' in filter_bar
    assert f'action="{reverse("export-now")}"' in filter_bar


@pytest.mark.django_db
def test_setup_step1_embeds_accounts_section(client, monkeypatch):
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [])

    response = client.get(reverse("setup"))

    body = response.content.decode()
    assert f'action="{reverse("account-login")}"' in body
    assert 'name="email"' in body


@pytest.mark.django_db
def test_setup_step1_has_exactly_one_add_account_email_input(client, monkeypatch):
    # PLAN T31: the shared _accounts_section.html partial has exactly one
    # add-account flow (button + reveal form) -- verify no separate,
    # always-visible email/password form is duplicated on the setup page.
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [])

    response = client.get(reverse("setup"))

    body = response.content.decode()
    assert body.count('type="email"') == 1
    assert body.count('name="email"') == 1


@pytest.mark.django_db
def test_settings_page_shows_folder_picker_button_hidden_by_default(client):
    response = client.get(reverse("settings"))

    body = response.content.decode()
    assert 'id="pick-folder-btn"' in body
    picker_start = body.index('id="pick-folder-btn"')
    tag_start = body.rindex("<button", 0, picker_start)
    tag_end = body.index(">", picker_start)
    assert "hidden" in body[tag_start:tag_end]


@pytest.mark.django_db
def test_account_login_two_factor_next_setup_renders_setup_page(client, monkeypatch):
    email = "t_t30_login_2fa_setup@example.com"
    # WORKING_FOLDER is session-scoped (see test_integration.py's docstring):
    # earlier tests in this file may have attached other accounts, which
    # would flip setup()'s has_accounts/show_step1 gate -- pin it explicitly.
    monkeypatch.setattr(views_module.remote_state, "list_accounts", lambda folder: [])

    class _PendingClient:
        def __init__(self, account):
            self.account = account

    pending = _PendingClient(email)

    class _Client:
        @classmethod
        def login(cls, e, p):
            raise views_module.TwoFactorRequired(pending)

    monkeypatch.setattr(views_module, "ICloudClient", _Client)

    response = client.post(
        reverse("account-login"), {"email": email, "password": "hunter2", "next": "setup"}
    )

    assert response.status_code == 200
    body = response.content.decode()
    assert "Step 1 of 2" in body
    assert email in body
    assert 'name="code"' in body


@pytest.mark.django_db
def test_account_login_success_next_setup_redirects_to_setup(client, monkeypatch):
    email = "t_t30_login_ok_setup@example.com"

    class _Client:
        def __init__(self, account):
            self.account = account

        @classmethod
        def login(cls, e, p):
            return cls(e)

    monkeypatch.setattr(views_module, "ICloudClient", _Client)
    monkeypatch.setattr(views_module.pull, "start_background_pull", lambda folder, c: None)

    response = client.post(
        reverse("account-login"), {"email": email, "password": "hunter2", "next": "setup"}
    )

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('setup')}?added={email}"


@pytest.mark.django_db
def test_account_2fa_success_next_setup_redirects_to_setup(client, monkeypatch):
    email = "t_t30_2fa_ok_setup@example.com"

    class _Pending:
        def __init__(self, account):
            self.account = account

        def submit_2fa(self, code):
            return True

    views_module._pending_2fa[email] = _Pending(email)
    monkeypatch.setattr(views_module.pull, "start_background_pull", lambda folder, c: None)

    response = client.post(
        reverse("account-2fa"), {"email": email, "code": "123456", "next": "setup"}
    )

    assert response.status_code == 302
    assert response["Location"] == f"{reverse('setup')}?added={email}"

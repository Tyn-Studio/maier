from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest

from maier.core import remote_state
from maier.core.models import Photo
from maier.core.moves import apply_status, dest_for, flatten_selected

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
# PLAN T24 (CTO decision, 2026-08-24): `selected/` is FLAT (no mirrored
# provenance subfolder); `rejected/` stays mirrored, unchanged.


def test_dest_for_select_is_flat_no_substructure():
    photo = Photo(relative_path="apple-luis/IMG_001.jpg")
    assert dest_for(photo, "selected") == PurePosixPath("selected/IMG_001.jpg")


def test_dest_for_reject_root_file():
    photo = Photo(relative_path="IMG.jpg")
    assert dest_for(photo, "rejected") == PurePosixPath("rejected/IMG.jpg")


def test_dest_for_reject_from_optional_mirrors_substructure():
    photo = Photo(relative_path="apple-luis/IMG_001.jpg")
    assert dest_for(photo, "rejected") == PurePosixPath("rejected/apple-luis/IMG_001.jpg")


def test_dest_for_optional_strips_status_prefix_legacy_mirrored():
    # Legacy mirrored layout (pre-existing folder, or no original_path
    # recorded) -- still resolvable from location alone.
    photo = Photo(relative_path="selected/apple-luis/IMG_001.jpg")
    assert dest_for(photo, "optional") == PurePosixPath("apple-luis/IMG_001.jpg")


def test_dest_for_optional_uses_original_path_when_recorded():
    photo = Photo(
        relative_path="selected/IMG_001.jpg",
        original_path="apple-luis/IMG_001.jpg",
        provenance="apple-luis",
    )
    assert dest_for(photo, "optional") == PurePosixPath("apple-luis/IMG_001.jpg")


def test_dest_for_optional_flat_no_original_path_falls_back_to_provenance():
    # Rule (c): cache-loss degradation -- flat select, no original_path on
    # record (e.g. .maier/ cache was deleted and rebuilt from location
    # alone). The origin subfolder can't be recovered, so it lands under
    # the provenance-named folder in the root instead.
    photo = Photo(relative_path="selected/IMG_001.jpg", provenance="apple-luis")
    assert dest_for(photo, "optional") == PurePosixPath("apple-luis/IMG_001.jpg")


def test_dest_for_optional_flat_no_original_path_no_provenance_falls_back_to_root():
    photo = Photo(relative_path="selected/IMG_001.jpg", provenance="")
    assert dest_for(photo, "optional") == PurePosixPath("IMG_001.jpg")


def test_dest_for_reject_from_selected_mirrors_original_via_original_path():
    photo = Photo(
        relative_path="selected/IMG_001.jpg",
        original_path="apple-luis/IMG_001.jpg",
        provenance="apple-luis",
    )
    assert dest_for(photo, "rejected") == PurePosixPath("rejected/apple-luis/IMG_001.jpg")


def test_dest_for_invalid_status_raises_value_error():
    photo = Photo(relative_path="a.jpg")
    with pytest.raises(ValueError):
        dest_for(photo, "bogus")


# --- apply_status: basic moves -----------------------------------------


@pytest.mark.django_db
def test_apply_status_select_is_flat(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    result = apply_status(tmp_path, photo, "selected")

    assert result.relative_path == "selected/IMG_001.jpg"
    assert result.status == "selected"
    assert result.status_changed_at is not None
    assert (tmp_path / "selected/IMG_001.jpg").exists()
    assert not (tmp_path / "apple-luis/IMG_001.jpg").exists()

    photo.refresh_from_db()
    assert photo.relative_path == "selected/IMG_001.jpg"
    assert photo.status == "selected"
    # PLAN T24 rule 3: pre-select path recorded for unflag/reject-mirroring.
    assert photo.original_path == "apple-luis/IMG_001.jpg"


@pytest.mark.django_db
def test_root_level_file_select(tmp_path):
    _touch(tmp_path, "IMG.jpg")
    photo = _make_photo("IMG.jpg")

    apply_status(tmp_path, photo, "selected")

    assert (tmp_path / "selected/IMG.jpg").exists()
    assert photo.relative_path == "selected/IMG.jpg"
    assert photo.original_path == "IMG.jpg"


@pytest.mark.django_db
def test_unflag_restore_via_original_path(tmp_path):
    _touch(tmp_path, "selected/IMG_001.jpg")
    photo = _make_photo(
        "selected/IMG_001.jpg",
        provenance="apple-luis",
        status="selected",
        original_path="apple-luis/IMG_001.jpg",
    )

    apply_status(tmp_path, photo, "optional")

    assert photo.relative_path == "apple-luis/IMG_001.jpg"
    assert photo.status == "optional"
    assert (tmp_path / "apple-luis/IMG_001.jpg").exists()
    assert not (tmp_path / "selected/IMG_001.jpg").exists()
    # PLAN T24 rule 4: original_path is never cleared -- harmless, keeps
    # round trips stable if the photo is selected again later.
    assert photo.original_path == "apple-luis/IMG_001.jpg"


@pytest.mark.django_db
def test_unflag_restore_legacy_mirrored_layout_still_works(tmp_path):
    # A pre-existing mirrored `selected/a/x.jpg` (dropped there before the
    # app ever touched it, or moved externally) has no recorded
    # original_path -- location alone still resolves it (rule (b)).
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
def test_unflag_flat_select_no_original_path_falls_back_to_provenance_dir(tmp_path):
    # Simulates .maier/ cache loss: a flat select with provenance known
    # from the row but no original_path (rebuilt from location alone).
    _touch(tmp_path, "selected/IMG_001.jpg")
    photo = _make_photo("selected/IMG_001.jpg", provenance="apple-luis", status="selected")

    apply_status(tmp_path, photo, "optional")

    assert photo.relative_path == "apple-luis/IMG_001.jpg"
    assert (tmp_path / "apple-luis/IMG_001.jpg").exists()


@pytest.mark.django_db
def test_unflag_flat_select_no_original_path_no_provenance_falls_back_to_root(tmp_path):
    _touch(tmp_path, "selected/IMG_001.jpg")
    photo = _make_photo("selected/IMG_001.jpg", provenance="", status="selected")

    apply_status(tmp_path, photo, "optional")

    assert photo.relative_path == "IMG_001.jpg"
    assert (tmp_path / "IMG_001.jpg").exists()


@pytest.mark.django_db
def test_reject_from_selected_mirrors_original_structure(tmp_path):
    # Select first (records original_path), then reject: rejected/ mirrors
    # the ORIGINAL substructure, not the flat selected/ location.
    _touch(tmp_path, "a/x.jpg")
    photo = _make_photo("a/x.jpg", provenance="a")
    apply_status(tmp_path, photo, "selected")
    assert photo.relative_path == "selected/x.jpg"

    apply_status(tmp_path, photo, "rejected")

    assert photo.relative_path == "rejected/a/x.jpg"
    assert photo.status == "rejected"
    assert (tmp_path / "rejected/a/x.jpg").exists()
    assert not (tmp_path / "selected/x.jpg").exists()


@pytest.mark.django_db
def test_reject_from_selected_legacy_mirrored_layout(tmp_path):
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
    _touch(tmp_path, "selected/IMG_001.jpg", b"existing")
    _touch(tmp_path, "apple-luis/IMG_001.jpg", b"new")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/IMG_001 (1).jpg"
    assert (tmp_path / "selected/IMG_001 (1).jpg").read_bytes() == b"new"
    # the pre-existing file was never overwritten
    assert (tmp_path / "selected/IMG_001.jpg").read_bytes() == b"existing"


@pytest.mark.django_db
def test_second_collision_increments_suffix(tmp_path):
    _touch(tmp_path, "selected/IMG_001.jpg", b"existing-0")
    _touch(tmp_path, "selected/IMG_001 (1).jpg", b"existing-1")
    _touch(tmp_path, "apple-luis/IMG_001.jpg", b"new")
    photo = _make_photo("apple-luis/IMG_001.jpg", provenance="apple-luis")

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/IMG_001 (2).jpg"
    assert (tmp_path / "selected/IMG_001 (2).jpg").read_bytes() == b"new"


@pytest.mark.django_db
def test_collision_between_two_different_sources_same_filename(tmp_path):
    # Flat selected/ makes cross-source filename clashes common (two phones
    # both export "IMG_0001.jpg") -- collision suffixing must still apply
    # even though the two photos never shared a directory.
    _touch(tmp_path, "apple-luis/IMG_0001.jpg", b"from-luis")
    _touch(tmp_path, "apple-maria/IMG_0001.jpg", b"from-maria")
    photo_luis = _make_photo("apple-luis/IMG_0001.jpg", provenance="apple-luis")
    photo_maria = _make_photo("apple-maria/IMG_0001.jpg", provenance="apple-maria")

    apply_status(tmp_path, photo_luis, "selected")
    apply_status(tmp_path, photo_maria, "selected")

    assert photo_luis.relative_path == "selected/IMG_0001.jpg"
    assert photo_maria.relative_path == "selected/IMG_0001 (1).jpg"
    assert (tmp_path / "selected/IMG_0001.jpg").read_bytes() == b"from-luis"
    assert (tmp_path / "selected/IMG_0001 (1).jpg").read_bytes() == b"from-maria"


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

    # Flat selected/ (PLAN T24) -- companion follows the image beside it,
    # same directory, still just as flat.
    assert photo.relative_path == "selected/IMG_001.jpg"
    assert photo.live_photo_video_path == "selected/IMG_001.mov"
    assert (tmp_path / "selected/IMG_001.jpg").exists()
    assert (tmp_path / "selected/IMG_001.mov").exists()
    assert not (tmp_path / "apple-luis/IMG_001.mov").exists()


@pytest.mark.django_db
def test_live_photo_companion_collision_gets_own_suffix(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    _touch(tmp_path, "apple-luis/IMG_001.mov", b"new-mov")
    _touch(tmp_path, "selected/IMG_001.mov", b"existing-mov")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )

    apply_status(tmp_path, photo, "selected")

    # image itself has no collision, moves cleanly
    assert photo.relative_path == "selected/IMG_001.jpg"
    # companion collides and gets its own suffix
    assert photo.live_photo_video_path == "selected/IMG_001 (1).mov"
    assert (tmp_path / "selected/IMG_001 (1).mov").read_bytes() == b"new-mov"
    assert (tmp_path / "selected/IMG_001.mov").read_bytes() == b"existing-mov"


@pytest.mark.django_db
def test_live_photo_companion_row_moves_with_its_file(tmp_path):
    # The .mov has its own Photo row (scan indexes it standalone). Culling
    # the image must update that row too, or the companion transiently
    # reappears in the grid with a stale path until the next scan.
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    _touch(tmp_path, "apple-luis/IMG_001.mov")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )
    companion_row = _make_photo(
        "apple-luis/IMG_001.mov",
        provenance="apple-luis",
        media_type=Photo.MEDIA_VIDEO,
    )

    apply_status(tmp_path, photo, "selected")

    companion_row.refresh_from_db()
    assert companion_row.relative_path == "selected/IMG_001.mov"
    assert companion_row.status == "selected"
    # PLAN T24 rule 7 (scan._status_and_provenance, unchanged): a file
    # directly under flat selected/ has no second path segment, so
    # provenance derives to "" here -- this is the companion row's own
    # DB-derived provenance being recomputed from its *new*, flat location,
    # not the original `original_path` bookkeeping (which only lives on the
    # main image's row).
    assert companion_row.provenance == ""

    apply_status(tmp_path, photo, "optional")
    companion_row.refresh_from_db()
    assert companion_row.relative_path == "apple-luis/IMG_001.mov"
    assert companion_row.status == "optional"


@pytest.mark.django_db
def test_live_photo_companion_missing_on_disk_does_not_crash(tmp_path):
    _touch(tmp_path, "apple-luis/IMG_001.jpg")
    photo = _make_photo(
        "apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        live_photo_video_path="apple-luis/IMG_001.mov",
    )

    apply_status(tmp_path, photo, "selected")

    assert photo.relative_path == "selected/IMG_001.jpg"
    # recorded path is left unchanged for the scanner to reconcile
    assert photo.live_photo_video_path == "apple-luis/IMG_001.mov"


# --- flatten_selected: converge legacy mirrored selected/ trees ------------
# CTO follow-up (2026-08-24): pre-existing mirrored `selected/<source>/...`
# layouts (from before T24, or dropped in pre-sorted) must self-heal to the
# flat layout. `scan()` calls this at the very start of every run.


@pytest.mark.django_db
def test_flatten_selected_moves_files_flat_and_updates_row(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG_001.jpg")
    photo = _make_photo(
        "selected/apple-luis/IMG_001.jpg", provenance="apple-luis", status="selected"
    )

    moved = flatten_selected(tmp_path)

    assert moved == 1
    assert (tmp_path / "selected/IMG_001.jpg").exists()
    assert not (tmp_path / "selected/apple-luis").exists()

    photo.refresh_from_db()
    assert photo.relative_path == "selected/IMG_001.jpg"
    assert photo.original_path == "apple-luis/IMG_001.jpg"

    # unflag now restores to the original subfolder via original_path
    apply_status(tmp_path, photo, "optional")
    assert photo.relative_path == "apple-luis/IMG_001.jpg"
    assert (tmp_path / "apple-luis/IMG_001.jpg").exists()


@pytest.mark.django_db
def test_flatten_selected_never_overwrites_existing_original_path(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG_001.jpg")
    photo = _make_photo(
        "selected/apple-luis/IMG_001.jpg",
        provenance="apple-luis",
        status="selected",
        original_path="already-recorded/IMG_001.jpg",
    )

    flatten_selected(tmp_path)

    photo.refresh_from_db()
    assert photo.original_path == "already-recorded/IMG_001.jpg"


@pytest.mark.django_db
def test_flatten_selected_updates_live_photo_companion_pair(tmp_path):
    _touch(tmp_path, "selected/a/IMG_010.jpg")
    _touch(tmp_path, "selected/a/IMG_010.mov")
    image = _make_photo(
        "selected/a/IMG_010.jpg",
        provenance="a",
        status="selected",
        live_photo_video_path="selected/a/IMG_010.mov",
    )
    video = _make_photo(
        "selected/a/IMG_010.mov",
        provenance="a",
        status="selected",
        media_type=Photo.MEDIA_VIDEO,
    )

    moved = flatten_selected(tmp_path)

    assert moved == 2
    image.refresh_from_db()
    video.refresh_from_db()
    assert image.relative_path == "selected/IMG_010.jpg"
    assert image.live_photo_video_path == "selected/IMG_010.mov"
    assert video.relative_path == "selected/IMG_010.mov"
    assert (tmp_path / "selected/IMG_010.jpg").exists()
    assert (tmp_path / "selected/IMG_010.mov").exists()


@pytest.mark.django_db
def test_flatten_selected_rewrites_remote_state_downloaded_map(tmp_path):
    account = "luis@example.com"
    slug = remote_state.account_slug(account)
    _touch(tmp_path, f"selected/{slug}/r1.jpg")
    remote_state.save_state(
        tmp_path,
        remote_state.AccountState(account=account, downloaded={"r1": f"selected/{slug}/r1.jpg"}),
    )

    flatten_selected(tmp_path)

    assert (tmp_path / "selected/r1.jpg").exists()
    state = remote_state.load_state(tmp_path, account)
    assert state.downloaded == {"r1": "selected/r1.jpg"}


@pytest.mark.django_db
def test_flatten_selected_moves_files_with_no_db_row(tmp_path):
    _touch(tmp_path, "selected/apple-luis/orphan.jpg")

    moved = flatten_selected(tmp_path)

    assert moved == 1
    assert (tmp_path / "selected/orphan.jpg").exists()
    assert not (tmp_path / "selected/apple-luis").exists()


@pytest.mark.django_db
def test_flatten_selected_collision_between_two_subfolders(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG.jpg", b"from-luis")
    _touch(tmp_path, "selected/apple-maria/IMG.jpg", b"from-maria")

    moved = flatten_selected(tmp_path)

    assert moved == 2
    names = sorted(p.name for p in (tmp_path / "selected").iterdir())
    assert names == ["IMG (1).jpg", "IMG.jpg"]


@pytest.mark.django_db
def test_flatten_selected_removes_empty_dirs_but_never_files(tmp_path):
    _touch(tmp_path, "selected/apple-luis/sub/IMG.jpg")

    flatten_selected(tmp_path)

    assert not (tmp_path / "selected/apple-luis").exists()
    assert (tmp_path / "selected/IMG.jpg").exists()


@pytest.mark.django_db
def test_flatten_selected_second_call_is_a_no_op(tmp_path):
    _touch(tmp_path, "selected/apple-luis/IMG.jpg")
    flatten_selected(tmp_path)

    assert flatten_selected(tmp_path) == 0


@pytest.mark.django_db
def test_flatten_selected_no_selected_dir_returns_zero(tmp_path):
    assert flatten_selected(tmp_path) == 0


@pytest.mark.django_db
def test_flatten_selected_already_flat_is_a_no_op(tmp_path):
    _touch(tmp_path, "selected/IMG.jpg")
    _make_photo("selected/IMG.jpg", status="selected")

    assert flatten_selected(tmp_path) == 0
    assert (tmp_path / "selected/IMG.jpg").exists()

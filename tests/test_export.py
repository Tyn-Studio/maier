"""COPY-to-destination export (SPEC §3, PLAN T25). Additive-only: never
deletes/overwrites at the destination. No Django DB needed for most of
these (export.py takes `folder`/`dest` directly) except where a Photo row
lookup drives date-prefix naming.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from maier.core import export
from maier.core.export import ExportResult, export_one, export_selected, maybe_auto_export
from maier.core.folder_settings import FolderSettings, save_settings
from maier.core.models import Photo

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _write(path: Path, content: bytes = b"data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _db_photo(relative_path: str, **overrides) -> Photo:
    kwargs = dict(
        status=Photo.STATUS_SELECTED,
        provenance="",
        file_size=4,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    kwargs.update(overrides)
    return Photo.objects.create(relative_path=relative_path, **kwargs)


def test_export_selected_copies_flat_files(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    _write(folder / "selected" / "IMG_0002.jpg", b"two")

    result = export_selected(folder, dest)

    assert isinstance(result, ExportResult)
    assert result.copied == 2
    assert result.skipped == 0
    assert result.errors == []
    assert (dest / "IMG_0001.jpg").read_bytes() == b"one"
    assert (dest / "IMG_0002.jpg").read_bytes() == b"two"


def test_export_selected_no_selected_dir_is_a_noop(tmp_path):
    folder = tmp_path / "work"
    folder.mkdir()
    dest = tmp_path / "dest"

    result = export_selected(folder, dest)

    assert result == ExportResult()
    assert not dest.exists()


def test_export_selected_ignores_dotfiles(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    _write(folder / "selected" / ".DS_Store", b"junk")

    result = export_selected(folder, dest)

    assert result.copied == 1
    assert not (dest / ".DS_Store").exists()


@pytest.mark.django_db
def test_export_selected_date_prefix_uses_photo_row(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    _db_photo("selected/IMG_0001.jpg", captured_at=_CAPTURED)

    result = export_selected(folder, dest, date_prefix=True)

    assert result.copied == 1
    assert (dest / "2025-06-14_IMG_0001.jpg").exists()


@pytest.mark.django_db
def test_export_selected_date_prefix_falls_back_to_plain_name_without_row(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    # No matching Photo row created.

    result = export_selected(folder, dest, date_prefix=True)

    assert result.copied == 1
    assert (dest / "IMG_0001.jpg").exists()


def test_export_selected_skips_identical_existing_file(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    src = _write(folder / "selected" / "IMG_0001.jpg", b"one")

    first = export_selected(folder, dest)
    assert first.copied == 1

    # Re-running with the destination file untouched (same size+mtime,
    # copy2 preserved it) skips instead of re-copying.
    second = export_selected(folder, dest)
    assert second.copied == 0
    assert second.skipped == 1
    assert (dest / "IMG_0001.jpg").read_bytes() == b"one"

    # sanity: source untouched throughout
    assert src.read_bytes() == b"one"


def test_export_selected_differing_file_gets_suffixed_copy_original_untouched(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    # Pre-existing, different file already at the destination under the
    # same name -- must never be overwritten.
    existing = _write(dest / "IMG_0001.jpg", b"pre-existing-different-content")

    result = export_selected(folder, dest)

    assert result.copied == 1
    assert result.errors == []
    assert existing.read_bytes() == b"pre-existing-different-content"
    assert (dest / "IMG_0001 (1).jpg").read_bytes() == b"one"


def test_export_selected_dest_inside_working_folder_raises(tmp_path):
    folder = tmp_path / "work"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")

    with pytest.raises(ValueError):
        export_selected(folder, folder / "selected" / "backup")

    with pytest.raises(ValueError):
        export_selected(folder, folder)


def test_export_selected_per_file_error_recorded_not_aborted(tmp_path, monkeypatch):
    import shutil

    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    _write(folder / "selected" / "IMG_0002.jpg", b"two")

    real_copy2 = shutil.copy2

    def _flaky_copy2(src, dst, *args, **kwargs):
        if Path(src).name == "IMG_0001.jpg":
            raise OSError("simulated copy failure")
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(export.shutil, "copy2", _flaky_copy2)

    result = export_selected(folder, dest)

    assert result.copied == 1  # IMG_0002.jpg still got copied
    assert (dest / "IMG_0002.jpg").exists()
    assert not (dest / "IMG_0001.jpg").exists()
    assert len(result.errors) == 1
    assert "IMG_0001.jpg" in result.errors[0]


def test_export_selected_includes_legacy_nested_files(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "apple-luis" / "IMG_0001.jpg", b"legacy")
    _write(folder / "selected" / "IMG_0002.jpg", b"flat")

    result = export_selected(folder, dest)

    assert result.copied == 2
    assert (dest / "IMG_0001.jpg").read_bytes() == b"legacy"
    assert (dest / "IMG_0002.jpg").read_bytes() == b"flat"


def test_export_one_returns_none_on_success(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")

    error = export_one(folder, dest, "selected/IMG_0001.jpg")

    assert error is None
    assert (dest / "IMG_0001.jpg").read_bytes() == b"one"


def test_export_one_missing_source_returns_error(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    folder.mkdir()

    error = export_one(folder, dest, "selected/nope.jpg")

    assert error is not None
    assert "nope.jpg" in error


def test_export_one_dest_inside_working_folder_raises(tmp_path):
    folder = tmp_path / "work"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")

    with pytest.raises(ValueError):
        export_one(folder, folder / "elsewhere", "selected/IMG_0001.jpg")


# --- maybe_auto_export -------------------------------------------------


@pytest.mark.django_db
def test_maybe_auto_export_noop_when_manual_mode(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="manual"))
    photo = _db_photo("selected/IMG_0001.jpg")

    maybe_auto_export(folder, photo)

    assert not dest.exists()


@pytest.mark.django_db
def test_maybe_auto_export_noop_when_no_destination(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    save_settings(folder, FolderSettings(export_destination="", export_mode="automatic"))
    photo = _db_photo("selected/IMG_0001.jpg")

    maybe_auto_export(folder, photo)

    assert not dest.exists()


@pytest.mark.django_db
def test_maybe_auto_export_noop_when_not_selected(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "IMG_0001.jpg", b"one")
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="automatic"))
    photo = _db_photo("IMG_0001.jpg", status=Photo.STATUS_OPTIONAL)

    maybe_auto_export(folder, photo)

    assert not dest.exists()


@pytest.mark.django_db
def test_maybe_auto_export_copies_in_automatic_mode(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="automatic"))
    photo = _db_photo("selected/IMG_0001.jpg")

    maybe_auto_export(folder, photo)

    assert (dest / "IMG_0001.jpg").read_bytes() == b"one"


@pytest.mark.django_db
def test_maybe_auto_export_never_raises_on_bad_destination(tmp_path):
    folder = tmp_path / "work"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    # Destination inside the working folder -- export_one would raise
    # ValueError; maybe_auto_export must swallow it, not propagate.
    save_settings(
        folder,
        FolderSettings(export_destination=str(folder / "selected"), export_mode="automatic"),
    )
    photo = _db_photo("selected/IMG_0001.jpg")

    maybe_auto_export(folder, photo)  # must not raise


@pytest.mark.django_db
def test_maybe_auto_export_date_prefix_setting_applied(tmp_path):
    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    save_settings(
        folder,
        FolderSettings(
            export_destination=str(dest), export_mode="automatic", export_date_prefix=True
        ),
    )
    photo = _db_photo("selected/IMG_0001.jpg", captured_at=_CAPTURED)

    maybe_auto_export(folder, photo)

    assert (dest / "2025-06-14_IMG_0001.jpg").exists()


# --- start_background_export --------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_background_export_runs_and_finishes(tmp_path, monkeypatch):
    import time

    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "selected" / "IMG_0001.jpg", b"one")
    monkeypatch.setattr(export, "_current_export", None)

    progress = export.start_background_export(folder, dest)

    deadline = time.time() + 5
    while not progress.finished and time.time() < deadline:
        time.sleep(0.05)

    assert progress.finished
    assert progress.copied == 1
    assert progress.errors == []
    assert (dest / "IMG_0001.jpg").exists()


def test_export_result_default_fields():
    result = ExportResult()
    assert result.copied == 0
    assert result.skipped == 0
    assert result.errors == []


# --- hooks: culling (select) and downloads (iCloud original landing) ------


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """Mirrors test_downloads.py's own fixture: the download worker thread
    handle is a module global, so a stray thread from a previous test file
    can't be mistaken for "still running" here.
    """
    from maier.core import downloads as downloads_module

    if downloads_module._worker_thread is not None:
        downloads_module._worker_thread.join(timeout=5)
    yield
    if downloads_module._worker_thread is not None:
        downloads_module._worker_thread.join(timeout=5)


@pytest.mark.django_db
def test_culling_select_triggers_auto_export_when_configured(tmp_path):
    from maier.core import culling

    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "IMG_0001.jpg", b"data")
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="automatic"))
    photo = _db_photo("IMG_0001.jpg", status=Photo.STATUS_OPTIONAL)

    culling.apply_status_any(folder, photo, Photo.STATUS_SELECTED)

    assert (dest / "IMG_0001.jpg").read_bytes() == b"data"


@pytest.mark.django_db
def test_culling_select_no_export_in_manual_mode(tmp_path):
    from maier.core import culling

    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    _write(folder / "IMG_0001.jpg", b"data")
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="manual"))
    photo = _db_photo("IMG_0001.jpg", status=Photo.STATUS_OPTIONAL)

    culling.apply_status_any(folder, photo, Photo.STATUS_SELECTED)

    assert not dest.exists()


@pytest.mark.django_db(transaction=True)
def test_icloud_original_landing_triggers_auto_export(tmp_path, monkeypatch):
    import time

    from maier.core import downloads as downloads_module
    from maier.core import remote_state

    folder = tmp_path / "work"
    dest = tmp_path / "dest"
    folder.mkdir()
    save_settings(folder, FolderSettings(export_destination=str(dest), export_mode="automatic"))

    class FakeClient:
        def download(self, remote_id, version, dest_path):
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(b"original-bytes")

    monkeypatch.setattr(downloads_module, "_client_for_account", lambda account: FakeClient())

    account = "luis@example.com"
    photo = Photo.objects.create(
        source=Photo.SOURCE_ICLOUD,
        account=account,
        remote_id="r1",
        relative_path=f"@icloud/{account}/r1",
        status=Photo.STATUS_SELECTED,
        provenance=remote_state.account_slug(account),
        file_size=1000,
        file_mtime=0.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
        remote_filename="a.jpg",
    )

    downloads_module.enqueue_original(folder, photo)

    deadline = time.time() + 5
    while not (dest / "a.jpg").exists() and time.time() < deadline:
        time.sleep(0.05)
    downloads_module._worker_thread.join(timeout=5)

    assert (dest / "a.jpg").read_bytes() == b"original-bytes"
    assert (folder / "selected" / "a.jpg").exists()

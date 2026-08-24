"""Status-change dispatcher (SPEC §18, PLAN T17): `culling.apply_status_any`
routes local photos through the unchanged `phaseb.apply_status_to_group`
group-cull path and remote (iCloud) photos through state-file-only
reject/undecide + an async-enqueued select. The remote branch here uses a
synchronous stand-in for `downloads.enqueue_original` (monkeypatched) so the
select -> download -> "row becomes local" round trip is exercised
deterministically without a background thread -- worker mechanics
(threading, retries, collisions, re-authentication) belong to
`test_downloads.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from culler.core import culling, phaseb, remote_state
from culler.core import downloads as downloads_module
from culler.core.models import Photo

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


def _local_photo(relative_path: str, **overrides) -> Photo:
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


def _touch(folder: Path, relative_path: str, content: bytes = b"data") -> None:
    path = folder / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _remote_photo(remote_id: str, account: str = "luis@example.com", **overrides) -> Photo:
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


class FakeClient:
    def __init__(self, account: str, payload: bytes = b"original-bytes"):
        self.account = account
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    def download(self, remote_id, version, dest):
        self.calls.append((remote_id, version))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)


def _install_sync_download(client: FakeClient, monkeypatch) -> None:
    """Replace `downloads.enqueue_original` with a synchronous call to
    `downloads._download_one` against `client` -- no thread, no polling.
    """

    def _fake_enqueue(folder, photo):
        progress = downloads_module.DownloadProgress()
        downloads_module._download_one(Path(folder), client, photo, progress)

    monkeypatch.setattr(downloads_module, "enqueue_original", _fake_enqueue)


# --- invalid status ----------------------------------------------------


@pytest.mark.django_db
def test_invalid_status_raises_value_error_for_local_photo(tmp_path):
    _touch(tmp_path, "a.jpg")
    photo = _local_photo("a.jpg")

    with pytest.raises(ValueError):
        culling.apply_status_any(tmp_path, photo, "bogus")


@pytest.mark.django_db
def test_invalid_status_raises_value_error_for_remote_photo(tmp_path):
    photo = _remote_photo("r1")

    with pytest.raises(ValueError):
        culling.apply_status_any(tmp_path, photo, "bogus")


# --- local photos: unchanged delegation to phaseb -----------------------


@pytest.mark.django_db
def test_local_photo_delegates_to_group_cull_unchanged(tmp_path):
    _touch(tmp_path, "a.jpg")
    photo = _local_photo("a.jpg")

    updated = culling.apply_status_any(tmp_path, photo, "selected")

    assert updated.status == "selected"
    assert (tmp_path / "selected/a.jpg").exists()
    assert not (tmp_path / "a.jpg").exists()


@pytest.mark.django_db
def test_local_photo_group_cull_still_auto_rejects_dupe_copy(tmp_path):
    _touch(tmp_path, "rep.jpg")
    _touch(tmp_path, "other.jpg")
    sha = "a" * 64
    rep = _local_photo("rep.jpg", sha256=sha)
    other = _local_photo("other.jpg", sha256=sha)

    culling.apply_status_any(tmp_path, rep, "selected")

    other.refresh_from_db()
    assert other.status == Photo.STATUS_REJECTED
    assert (tmp_path / "rejected/other.jpg").exists()


# --- remote photos: reject/undecide are state-file only, no disk I/O ----


@pytest.mark.django_db
def test_reject_remote_photo_writes_only_state_no_disk_io(tmp_path):
    photo = _remote_photo("r1")

    updated = culling.apply_status_any(tmp_path, photo, "rejected")

    assert updated.status == "rejected"
    assert not (tmp_path / "selected").exists()
    assert not (tmp_path / "rejected").exists()

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.decisions == {"r1": "rejected"}
    assert state.downloaded == {}

    photo.refresh_from_db()
    assert photo.status == "rejected"
    assert photo.source == Photo.SOURCE_ICLOUD
    assert photo.relative_path == "@icloud/luis@example.com/r1"


@pytest.mark.django_db
def test_undecide_remote_photo_deletes_decision_key(tmp_path):
    remote_state.save_state(
        tmp_path,
        remote_state.AccountState(account="luis@example.com", decisions={"r1": "rejected"}),
    )
    photo = _remote_photo("r1")

    updated = culling.apply_status_any(tmp_path, photo, "optional")

    assert updated.status == "optional"
    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.decisions == {}
    assert not (tmp_path / "selected").exists()
    assert not (tmp_path / "rejected").exists()


@pytest.mark.django_db
def test_reject_then_undecide_round_trip_no_disk_io(tmp_path):
    photo = _remote_photo("r1")

    culling.apply_status_any(tmp_path, photo, "rejected")
    culling.apply_status_any(tmp_path, photo, "optional")

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.decisions == {}
    assert not (tmp_path / "selected").exists()
    assert not (tmp_path / "rejected").exists()


# --- remote photos: select flips status immediately, enqueues async -----


@pytest.mark.django_db
def test_select_remote_photo_flips_status_without_blocking_on_download(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        downloads_module,
        "enqueue_original",
        lambda folder, photo: calls.append((Path(folder), photo.pk)),
    )
    photo = _remote_photo("r1")

    updated = culling.apply_status_any(tmp_path, photo, "selected")

    assert updated.status == "selected"
    assert updated.status_changed_at is not None
    # Not converted yet -- the download is async, this call never blocks on it.
    assert updated.source == Photo.SOURCE_ICLOUD
    assert calls == [(tmp_path, photo.pk)]
    assert not (tmp_path / "selected").exists()


# --- remote photos: full select -> download -> local round trip ---------


@pytest.mark.django_db
def test_select_downloads_original_and_converts_row_to_local(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com", payload=b"the-original-bytes")
    _install_sync_download(client, monkeypatch)
    photo = _remote_photo("r1", remote_filename="IMG_0001.jpg")

    culling.apply_status_any(tmp_path, photo, "selected")

    slug = remote_state.account_slug("luis@example.com")
    dest = tmp_path / "selected" / slug / "IMG_0001.jpg"
    assert dest.exists()
    assert dest.read_bytes() == b"the-original-bytes"

    photo.refresh_from_db()
    assert photo.source == Photo.SOURCE_LOCAL
    assert photo.relative_path == f"selected/{slug}/IMG_0001.jpg"
    assert photo.provenance == slug
    assert photo.sha256 is None
    assert photo.file_size == len(b"the-original-bytes")

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.downloaded == {"r1": f"selected/{slug}/IMG_0001.jpg"}
    assert "r1" not in state.decisions


@pytest.mark.django_db
def test_downloaded_photo_unflag_then_reselect_moves_file_no_redownload(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com")
    _install_sync_download(client, monkeypatch)
    photo = _remote_photo("r1", remote_filename="IMG_0001.jpg")
    culling.apply_status_any(tmp_path, photo, "selected")
    photo.refresh_from_db()
    slug = remote_state.account_slug("luis@example.com")

    # Unflag: the row is local now, so this routes through phaseb -- an
    # ordinary move, never a delete, never a re-download (SPEC §18 rule 3).
    updated = culling.apply_status_any(tmp_path, photo, "optional")

    assert updated.status == "optional"
    assert (tmp_path / slug / "IMG_0001.jpg").exists()
    assert not (tmp_path / "selected" / slug / "IMG_0001.jpg").exists()
    assert client.calls == [("r1", "original")]

    # Re-select: moves back, still no re-download.
    updated2 = culling.apply_status_any(tmp_path, updated, "selected")

    assert updated2.status == "selected"
    assert (tmp_path / "selected" / slug / "IMG_0001.jpg").exists()
    assert not (tmp_path / slug / "IMG_0001.jpg").exists()
    assert client.calls == [("r1", "original")]


# --- phaseb.apply_status_to_group is remote-safe when called directly ---


@pytest.mark.django_db
def test_phaseb_apply_status_to_group_handles_remote_photo_directly(tmp_path):
    """A plain `moves.apply_status` on a remote sentinel path would raise
    `FileNotFoundError` -- `apply_status_to_group` must delegate to
    `culling`'s remote handling instead when given a remote photo directly.
    """
    photo = _remote_photo("r1")

    updated = phaseb.apply_status_to_group(tmp_path, photo, "rejected")

    assert updated.status == "rejected"
    assert updated.source == Photo.SOURCE_ICLOUD
    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.decisions == {"r1": "rejected"}
    assert not (tmp_path / "rejected").exists()


# --- AccountSessionExpired -----------------------------------------------


def test_account_session_expired_carries_account():
    exc = culling.AccountSessionExpired("luis@example.com")

    assert exc.account == "luis@example.com"
    assert "luis@example.com" in str(exc)

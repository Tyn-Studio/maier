"""Background original-download worker (SPEC §18 rules 2-3, PLAN T17). No
network, no real `pyicloud`/`ICloudClient` -- everything is faked at the
`downloads._client_for_account` seam. Thread-based tests mirror
`test_pull.py`'s own pattern: `@pytest.mark.django_db(transaction=True)` +
deadline-polling (background threads need their own DB connections, so
cross-thread visibility needs the real transactional test DB, not the
default wrapped-in-one-transaction mode).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from maier.core import downloads, remote_state
from maier.core.models import Photo

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


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
    def __init__(self, account: str, payload: bytes = b"orig", fail_ids: set[str] | None = None):
        self.account = account
        self.payload = payload
        self.fail_ids = fail_ids or set()
        self.calls: list[tuple[str, str]] = []

    def download(self, remote_id, version, dest):
        self.calls.append((remote_id, version))
        if remote_id in self.fail_ids:
            raise RuntimeError(f"simulated download failure for {remote_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@pytest.fixture(autouse=True)
def _reset_worker_state():
    """The worker thread handle/progress are module globals (mirrors
    scan.py's `_current_scan` / phaseb.py's `_current_phase_b`) -- make sure
    a stray thread from a previous test can't be mistaken for "still
    running" by the next test's `start_worker` call.
    """
    if downloads._worker_thread is not None:
        downloads._worker_thread.join(timeout=5)
    yield
    if downloads._worker_thread is not None:
        downloads._worker_thread.join(timeout=5)


# --- DownloadProgress -----------------------------------------------------


def test_download_progress_defaults():
    progress = downloads.DownloadProgress()

    assert progress.pending == 0
    assert progress.done == 0
    assert progress.errors == []


# --- pending_remote_ids -----------------------------------------------------


@pytest.mark.django_db
def test_pending_remote_ids_selected_and_undownloaded_only():
    _remote_photo("r1", status=Photo.STATUS_SELECTED)
    _remote_photo("r2", status=Photo.STATUS_OPTIONAL)
    _remote_photo("r3", status=Photo.STATUS_REJECTED)
    _remote_photo("r4", account="maria@example.com", status=Photo.STATUS_SELECTED)

    assert downloads.pending_remote_ids("luis@example.com") == {"r1"}
    assert downloads.pending_remote_ids("maria@example.com") == {"r4"}
    assert downloads.pending_remote_ids("nobody@example.com") == set()


# --- _pending_rows: DB-derived, crash-safety net via state.downloaded -----


@pytest.mark.django_db
def test_pending_rows_excludes_ids_already_recorded_in_downloaded(tmp_path):
    remote_state.save_state(
        tmp_path,
        remote_state.AccountState(
            account="luis@example.com", downloaded={"r1": "selected/luis-example-com/a.jpg"}
        ),
    )
    _remote_photo("r1", status=Photo.STATUS_SELECTED)
    p2 = _remote_photo("r2", status=Photo.STATUS_SELECTED)

    rows = downloads._pending_rows(tmp_path)

    assert [p.pk for p in rows] == [p2.pk]


# --- enqueue_original / worker: happy path ---------------------------------


@pytest.mark.django_db(transaction=True)
def test_enqueue_original_downloads_and_converts_row_to_local(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com")
    monkeypatch.setattr(downloads, "_client_for_account", lambda account: client)
    photo = _remote_photo("r1", status=Photo.STATUS_SELECTED, remote_filename="a.jpg")

    downloads.enqueue_original(tmp_path, photo)
    _wait_for(lambda: Photo.objects.get(pk=photo.pk).source == Photo.SOURCE_LOCAL)
    downloads._worker_thread.join(timeout=5)

    photo.refresh_from_db()
    slug = remote_state.account_slug("luis@example.com")
    assert photo.relative_path == f"selected/{slug}/a.jpg"
    assert photo.provenance == slug
    assert photo.sha256 is None
    assert (tmp_path / "selected" / slug / "a.jpg").exists()

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.downloaded == {"r1": f"selected/{slug}/a.jpg"}


@pytest.mark.django_db(transaction=True)
def test_worker_falls_back_to_remote_id_filename_when_none_recorded(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com")
    monkeypatch.setattr(downloads, "_client_for_account", lambda account: client)
    photo = _remote_photo("r1", status=Photo.STATUS_SELECTED, remote_filename="")

    downloads.enqueue_original(tmp_path, photo)
    _wait_for(lambda: Photo.objects.get(pk=photo.pk).source == Photo.SOURCE_LOCAL)
    downloads._worker_thread.join(timeout=5)

    photo.refresh_from_db()
    slug = remote_state.account_slug("luis@example.com")
    assert photo.relative_path == f"selected/{slug}/r1.jpg"


# --- download failure + retry -----------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_download_failure_leaves_row_pending_and_retry_succeeds(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com", fail_ids={"r1"})
    monkeypatch.setattr(downloads, "_client_for_account", lambda account: client)
    photo = _remote_photo("r1", status=Photo.STATUS_SELECTED, remote_filename="a.jpg")

    downloads.enqueue_original(tmp_path, photo)
    _wait_for(
        lambda: downloads._worker_thread is not None and not downloads._worker_thread.is_alive()
    )

    photo.refresh_from_db()
    assert photo.source == Photo.SOURCE_ICLOUD
    assert photo.status == Photo.STATUS_SELECTED
    assert downloads._last_progress is not None
    assert any("r1" in e for e in downloads._last_progress.errors)

    client.fail_ids = set()
    downloads.enqueue_original(tmp_path, photo)
    _wait_for(lambda: Photo.objects.get(pk=photo.pk).source == Photo.SOURCE_LOCAL)
    downloads._worker_thread.join(timeout=5)

    photo.refresh_from_db()
    assert photo.source == Photo.SOURCE_LOCAL


# --- account needs re-authentication ----------------------------------------


@pytest.mark.django_db(transaction=True)
def test_account_needing_reauth_is_skipped_other_accounts_still_processed(tmp_path, monkeypatch):
    client_b = FakeClient("maria@example.com")

    def _client_for(account):
        return None if account == "luis@example.com" else client_b

    monkeypatch.setattr(downloads, "_client_for_account", _client_for)

    photo_a = _remote_photo(
        "ra", account="luis@example.com", status=Photo.STATUS_SELECTED, remote_filename="a.jpg"
    )
    photo_b = _remote_photo(
        "rb", account="maria@example.com", status=Photo.STATUS_SELECTED, remote_filename="b.jpg"
    )

    downloads.enqueue_original(tmp_path, photo_b)
    _wait_for(lambda: Photo.objects.get(pk=photo_b.pk).source == Photo.SOURCE_LOCAL)
    downloads._worker_thread.join(timeout=5)

    photo_a.refresh_from_db()
    assert photo_a.source == Photo.SOURCE_ICLOUD
    assert photo_a.status == Photo.STATUS_SELECTED
    assert downloads._last_progress is not None
    errors = downloads._last_progress.errors
    assert any("luis@example.com" in e and "re-authentication" in e for e in errors)


# --- collision: identical filenames in the same account --------------------


@pytest.mark.django_db(transaction=True)
def test_collision_same_filename_gets_numeric_suffix(tmp_path, monkeypatch):
    client = FakeClient("luis@example.com")
    monkeypatch.setattr(downloads, "_client_for_account", lambda account: client)
    p1 = _remote_photo("r1", status=Photo.STATUS_SELECTED, remote_filename="dup.jpg")
    p2 = _remote_photo("r2", status=Photo.STATUS_SELECTED, remote_filename="dup.jpg")

    downloads.enqueue_original(tmp_path, p1)
    _wait_for(
        lambda: (
            not Photo.objects.filter(
                source=Photo.SOURCE_ICLOUD, status=Photo.STATUS_SELECTED
            ).exists()
        )
    )
    downloads._worker_thread.join(timeout=5)

    slug = remote_state.account_slug("luis@example.com")
    p1.refresh_from_db()
    p2.refresh_from_db()
    assert p1.relative_path == f"selected/{slug}/dup.jpg"
    assert p2.relative_path == f"selected/{slug}/dup (1).jpg"
    assert (tmp_path / "selected" / slug / "dup.jpg").exists()
    assert (tmp_path / "selected" / slug / "dup (1).jpg").exists()


# --- start_worker: single-flight --------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_worker_is_single_flight(monkeypatch, tmp_path):
    release = threading.Event()
    started = threading.Event()
    calls: list[Path] = []

    def _slow_loop(folder, progress):
        calls.append(folder)
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(downloads, "_worker_loop", _slow_loop)

    downloads.start_worker(tmp_path)
    assert started.wait(timeout=5)
    thread1 = downloads._worker_thread

    downloads.start_worker(tmp_path)
    thread2 = downloads._worker_thread

    assert thread1 is thread2
    release.set()
    thread1.join(timeout=5)
    assert len(calls) == 1


# --- pull.py interaction: never disturbs a selected-pending remote row -----


@dataclass
class _FakeAsset:
    remote_id: str
    filename: str
    captured_at: datetime
    size: int
    media_type: str = "image"


class _FakePullClient:
    def __init__(self, account: str, assets: list[_FakeAsset]):
        self.account = account
        self._assets = assets

    def list_assets(self, since):
        yield from self._assets

    def download(self, remote_id, version, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"preview")


@pytest.mark.django_db
def test_pull_does_not_disturb_selected_pending_remote_row(tmp_path):
    from maier.core.pull import PullProgress, pull_account

    photo = _remote_photo("r1", status=Photo.STATUS_SELECTED)
    client = _FakePullClient("luis@example.com", [_FakeAsset("r1", "a.jpg", _CAPTURED, 1000)])

    pull_account(tmp_path, client, PullProgress())

    photo.refresh_from_db()
    assert photo.status == Photo.STATUS_SELECTED
    assert photo.source == Photo.SOURCE_ICLOUD


@pytest.mark.django_db
def test_pull_does_not_resurrect_row_after_successful_download(tmp_path):
    """Regression guard for the flagged design choice in `_convert_to_local`
    (PLAN T17): the converted local row keeps its `account`/`remote_id`
    fields. `pull.py`'s `_process_asset` relies entirely on
    `state.downloaded` (not on `source`) to avoid resurrecting it -- as
    long as `_download_one` saves that entry (it does, right after the DB
    conversion), a later pull for the same `remote_id` must leave the local
    row alone rather than overwriting it back to a sentinel `source="icloud"`
    row.
    """
    from maier.core.pull import PullProgress, pull_account

    slug = remote_state.account_slug("luis@example.com")
    local_photo = Photo.objects.create(
        relative_path=f"selected/{slug}/a.jpg",
        status=Photo.STATUS_SELECTED,
        provenance=slug,
        file_size=1000,
        file_mtime=0.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
        source=Photo.SOURCE_LOCAL,
        account="luis@example.com",
        remote_id="r1",
    )
    remote_state.save_state(
        tmp_path,
        remote_state.AccountState(
            account="luis@example.com", downloaded={"r1": f"selected/{slug}/a.jpg"}
        ),
    )
    client = _FakePullClient("luis@example.com", [_FakeAsset("r1", "a.jpg", _CAPTURED, 1000)])

    pull_account(tmp_path, client, PullProgress())

    assert Photo.objects.filter(pk=local_photo.pk).count() == 1
    local_photo.refresh_from_db()
    assert local_photo.source == Photo.SOURCE_LOCAL
    assert local_photo.relative_path == f"selected/{slug}/a.jpg"

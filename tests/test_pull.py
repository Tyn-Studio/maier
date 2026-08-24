"""Incremental iCloud pull pipeline (SPEC §18, PLAN T16). Exercised against a
duck-typed fake client (`.account`, `.list_assets(since)`,
`.download(remote_id, version, dest)`) matching `core/icloud.py`'s
`ICloudClient` interface -- never imports `core.icloud` itself, per brief
(that module is landing in parallel via a concurrent agent).
"""

import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from maier.core import pull as pull_module
from maier.core import remote_state
from maier.core.models import Photo
from maier.core.previews import remote_preview_dest
from maier.core.pull import PullProgress, pull_account, start_background_pull


@dataclass
class FakeAsset:
    remote_id: str
    filename: str
    captured_at: datetime
    size: int
    media_type: str = "image"


class FakeClient:
    """Records every `since` it was called with; assets are consumed one
    listing at a time from `pending_batches` (a list of asset-lists), so
    tests can control what a second/incremental pull sees.
    """

    def __init__(self, account: str, batches: list[list[FakeAsset]] | None = None):
        self.account = account
        self._batches = list(batches or [])
        self.since_calls: list[datetime | None] = []
        self.downloaded: list[tuple[str, str]] = []
        self.download_failures: set[str] = set()
        self.list_raises_after: int | None = None  # raise mid-iteration after N yields

    def list_assets(self, since):
        self.since_calls.append(since)
        batch = self._batches.pop(0) if self._batches else []
        for i, asset in enumerate(batch):
            if self.list_raises_after is not None and i == self.list_raises_after:
                raise RuntimeError("simulated network failure mid-listing")
            yield asset

    def download(self, remote_id, version, dest):
        self.downloaded.append((remote_id, version))
        if remote_id in self.download_failures:
            raise RuntimeError(f"simulated download failure for {remote_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f"fake-{version}-{remote_id}".encode())


def _asset(remote_id: str, when: datetime, size: int = 1000, media_type: str = "image"):
    return FakeAsset(
        remote_id=remote_id,
        filename=f"{remote_id}.jpg",
        captured_at=when,
        size=size,
        media_type=media_type,
    )


T0 = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
T2 = T0 + timedelta(minutes=2)


# --- basic pull behaviour ---------------------------------------------------


@pytest.mark.django_db
def test_pull_creates_photo_rows_with_correct_fields(tmp_path):
    assets = [_asset("r1", T0), _asset("r2", T1, size=2000, media_type="video")]
    client = FakeClient("luis@example.com", [assets])
    progress = PullProgress()

    pull_account(tmp_path, client, progress)

    assert progress.finished is True
    assert progress.errors == []
    # scanned = assets enumerated; total/done = preview fetches only.
    assert progress.scanned == 2
    assert progress.total == progress.done == 2

    p1 = Photo.objects.get(account="luis@example.com", remote_id="r1")
    assert p1.source == Photo.SOURCE_ICLOUD
    assert p1.relative_path == "@icloud/luis@example.com/r1"
    assert p1.captured_at == T0
    assert p1.captured_at_source == "exif"
    assert p1.media_type == Photo.MEDIA_IMAGE
    assert p1.file_size == 1000
    assert p1.file_mtime == 0.0
    # PLAN T17 change (flagged): provenance is the filesystem-safe SLUG, not
    # the raw email -- keeps provenance stable across the select-download
    # conversion, whose destination directory must be the slug (SPEC §18).
    assert p1.provenance == remote_state.account_slug("luis@example.com")
    assert p1.remote_filename == "r1.jpg"
    assert p1.status == Photo.STATUS_OPTIONAL

    p2 = Photo.objects.get(account="luis@example.com", remote_id="r2")
    assert p2.media_type == Photo.MEDIA_VIDEO
    assert p2.file_size == 2000


@pytest.mark.django_db
def test_pull_status_derived_from_decisions(tmp_path):
    state = remote_state.AccountState(
        account="luis@example.com", decisions={"r1": "rejected", "r2": "optional"}
    )
    remote_state.save_state(tmp_path, state)

    assets = [_asset("r1", T0), _asset("r2", T1), _asset("r3", T2)]
    client = FakeClient("luis@example.com", [assets])

    pull_account(tmp_path, client, PullProgress())

    assert Photo.objects.get(remote_id="r1").status == Photo.STATUS_REJECTED
    assert Photo.objects.get(remote_id="r2").status == Photo.STATUS_OPTIONAL
    assert Photo.objects.get(remote_id="r3").status == Photo.STATUS_OPTIONAL


@pytest.mark.django_db
def test_pull_prefetches_medium_preview(tmp_path):
    client = FakeClient("luis@example.com", [[_asset("r1", T0)]])

    pull_account(tmp_path, client, PullProgress())

    dest = remote_preview_dest(tmp_path, "luis@example.com", "r1")
    assert dest.exists()
    assert dest.read_bytes() == b"fake-thumb-r1"
    assert client.downloaded == [("r1", "thumb")]


@pytest.mark.django_db
def test_pull_skips_preview_download_when_already_cached(tmp_path):
    client = FakeClient("luis@example.com", [[_asset("r1", T0)]])
    dest = remote_preview_dest(tmp_path, "luis@example.com", "r1")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"already-cached")

    pull_account(tmp_path, client, PullProgress())

    assert client.downloaded == []
    assert dest.read_bytes() == b"already-cached"


@pytest.mark.django_db
def test_pull_skips_assets_already_downloaded_locally(tmp_path):
    state = remote_state.AccountState(
        account="luis@example.com", downloaded={"r1": "selected/luis/IMG_0001.jpg"}
    )
    remote_state.save_state(tmp_path, state)

    client = FakeClient("luis@example.com", [[_asset("r1", T0), _asset("r2", T1)]])

    pull_account(tmp_path, client, PullProgress())

    assert not Photo.objects.filter(remote_id="r1").exists()
    assert Photo.objects.filter(remote_id="r2").exists()
    assert client.downloaded == [("r2", "thumb")]


# --- cursor / incremental behaviour -----------------------------------------


@pytest.mark.django_db
def test_pull_advances_cursor_to_max_captured_at(tmp_path):
    assets = [_asset("r1", T0), _asset("r2", T2), _asset("r3", T1)]
    client = FakeClient("luis@example.com", [assets])

    pull_account(tmp_path, client, PullProgress())

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.cursor == T2


@pytest.mark.django_db
def test_incremental_second_pull_only_processes_unknown_remote_ids(tmp_path):
    # The listing is always a full enumeration (since=None) -- incrementality
    # is keyed on remote_id, so already-known r1/r2 are skipped and only the
    # new r3 is processed.
    client = FakeClient(
        "luis@example.com",
        batches=[
            [_asset("r1", T0), _asset("r2", T1)],
            [_asset("r1", T0), _asset("r2", T1), _asset("r3", T2)],
        ],
    )

    pull_account(tmp_path, client, PullProgress())
    assert client.since_calls == [None]

    progress = PullProgress()
    pull_account(tmp_path, client, progress)

    assert client.since_calls == [None, None]
    assert progress.scanned == 3  # full enumeration always scans everything
    assert progress.total == progress.done == 1  # but only r3's preview is fetched
    assert Photo.objects.count() == 3
    assert Photo.objects.filter(remote_id="r3").exists()


@pytest.mark.django_db
def test_old_photo_added_to_icloud_later_is_still_picked_up(tmp_path):
    # A photo with a capture date OLDER than everything already pulled shows
    # up in iCloud later (e.g. the user imported an old album). A capture-
    # date cursor would hide it forever; remote_id-keyed incrementality must
    # pick it up.
    client = FakeClient(
        "luis@example.com",
        batches=[
            [_asset("r2", T1)],
            [_asset("r2", T1), _asset("r1-old", T0 - timedelta(days=365))],
        ],
    )

    pull_account(tmp_path, client, PullProgress())
    pull_account(tmp_path, client, PullProgress())

    assert Photo.objects.filter(remote_id="r1-old").exists()


@pytest.mark.django_db
def test_failed_preview_is_refetched_on_next_pull(tmp_path):
    # r1's medium download fails on the first pull (row still created);
    # the second pull's repair pass retries and lands the preview.
    client = FakeClient(
        "luis@example.com",
        batches=[[_asset("r1", T0)], [_asset("r1", T0)]],
    )
    client.download_failures = {"r1"}
    pull_account(tmp_path, client, PullProgress())
    assert not remote_preview_dest(tmp_path, "luis@example.com", "r1").exists()

    client.download_failures = set()
    progress = PullProgress()
    pull_account(tmp_path, client, progress)

    assert progress.errors == []
    assert remote_preview_dest(tmp_path, "luis@example.com", "r1").exists()


@pytest.mark.django_db
def test_cursor_not_advanced_when_listing_raises_mid_way(tmp_path):
    assets = [_asset("r1", T0), _asset("r2", T1), _asset("r3", T2)]
    client = FakeClient("luis@example.com", [assets])
    client.list_raises_after = 2  # yields r1, r2, then raises before r3

    progress = PullProgress()
    pull_account(tmp_path, client, progress)

    assert progress.finished is True
    assert any("list_assets" in e for e in progress.errors)

    # Assets yielded before the failure were still processed...
    assert Photo.objects.filter(remote_id="r1").exists()
    assert Photo.objects.filter(remote_id="r2").exists()
    assert not Photo.objects.filter(remote_id="r3").exists()

    # ...but the cursor must not advance past the failed listing.
    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.cursor is None


@pytest.mark.django_db
def test_cursor_unaffected_by_failed_listing_on_second_pull(tmp_path):
    client = FakeClient(
        "luis@example.com",
        batches=[[_asset("r1", T0)], [_asset("r2", T1), _asset("r3", T2)]],
    )

    pull_account(tmp_path, client, PullProgress())
    cursor_after_first = remote_state.load_state(tmp_path, "luis@example.com").cursor
    assert cursor_after_first == T0

    client.list_raises_after = 0  # second pull's listing fails immediately
    pull_account(tmp_path, client, PullProgress())

    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.cursor == T0  # unchanged, not advanced and not reset


# --- per-asset failure handling ---------------------------------------------


@pytest.mark.django_db
def test_per_asset_download_failure_recorded_but_pull_completes(tmp_path):
    client = FakeClient("luis@example.com", [[_asset("r1", T0), _asset("r2", T1)]])
    client.download_failures = {"r1"}

    progress = PullProgress()
    pull_account(tmp_path, client, progress)

    assert progress.finished is True
    assert progress.done == 2  # both preview attempts completed (one failed)
    assert any("r1" in e for e in progress.errors)

    # The Photo row still got created (upsert happens before download).
    assert Photo.objects.filter(remote_id="r1").exists()
    assert Photo.objects.filter(remote_id="r2").exists()
    assert remote_preview_dest(tmp_path, "luis@example.com", "r2").exists()
    assert not remote_preview_dest(tmp_path, "luis@example.com", "r1").exists()

    # Cursor still advances -- per-asset errors aren't iteration failures.
    state = remote_state.load_state(tmp_path, "luis@example.com")
    assert state.cursor == T1


# --- idempotency -------------------------------------------------------------


@pytest.mark.django_db
def test_repull_is_idempotent_no_duplicate_rows(tmp_path):
    client = FakeClient("luis@example.com", [[_asset("r1", T0)], [_asset("r1", T0)]])

    pull_account(tmp_path, client, PullProgress())
    pull_account(tmp_path, client, PullProgress())

    assert Photo.objects.filter(remote_id="r1").count() == 1


# --- two-phase progress accounting ------------------------------------------


@pytest.mark.django_db
def test_progress_counts_scanned_and_previews_separately(tmp_path):
    # scanned tracks the enumeration; total/done track preview fetches,
    # which run concurrently with the enumeration on a shared pool.
    assets = [_asset("r1", T0), _asset("r2", T1), _asset("r3", T2)]
    client = FakeClient("luis@example.com", [assets])
    progress = PullProgress()

    pull_account(tmp_path, client, progress)

    assert progress.scanned == 3
    assert progress.total == 3
    assert progress.done == 3


# --- background / single-flight ---------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_start_background_pull_finishes(tmp_path):
    client = FakeClient("luis@example.com", [[_asset("r1", T0)]])

    progress = start_background_pull(tmp_path, client)

    deadline = time.time() + 10
    while not progress.finished and time.time() < deadline:
        time.sleep(0.05)

    assert progress.finished is True
    assert progress.errors == []
    assert Photo.objects.filter(remote_id="r1").exists()


@pytest.mark.django_db(transaction=True)
def test_start_background_pull_single_flight_per_account(monkeypatch, tmp_path):
    release = threading.Event()
    started = threading.Event()

    def _slow_pull(folder, client, progress):
        started.set()
        release.wait(timeout=5)
        progress.finished = True

    monkeypatch.setattr(pull_module, "pull_account", _slow_pull)

    client = FakeClient("luis@example.com")
    progress1 = start_background_pull(tmp_path, client)
    assert started.wait(timeout=5)
    progress2 = start_background_pull(tmp_path, client)

    assert progress1 is progress2

    release.set()
    deadline = time.time() + 5
    while not progress1.finished and time.time() < deadline:
        time.sleep(0.02)
    assert progress1.finished is True


@pytest.mark.django_db(transaction=True)
def test_start_background_pull_different_accounts_run_independently(monkeypatch, tmp_path):
    release = threading.Event()
    started = threading.Event()
    call_accounts = []

    def _slow_pull(folder, client, progress):
        call_accounts.append(client.account)
        started.set()
        release.wait(timeout=5)
        progress.finished = True

    monkeypatch.setattr(pull_module, "pull_account", _slow_pull)

    client_a = FakeClient("luis@example.com")
    client_b = FakeClient("maria@example.com")

    progress_a = start_background_pull(tmp_path, client_a)
    assert started.wait(timeout=5)
    started.clear()

    progress_b = start_background_pull(tmp_path, client_b)
    assert started.wait(timeout=5)

    assert progress_a is not progress_b
    assert set(call_accounts) == {"luis@example.com", "maria@example.com"}

    release.set()
    deadline = time.time() + 5
    while (not progress_a.finished or not progress_b.finished) and time.time() < deadline:
        time.sleep(0.02)
    assert progress_a.finished is True
    assert progress_b.finished is True

"""M5 end-to-end integration tests (SPEC §18, PLAN T19): attach -> pull ->
mixed local/remote timeline -> full remote cull loop via views -> only
selected originals ever land on disk -> state survives `.culler/` cache
deletion -> incremental re-pull is idempotent -> expired-session UX ->
remote rows never join dupe grouping.

Exercised against a fake duck-typed `ICloudClient` (`.account`,
`.list_assets(since)`, `.download(remote_id, version, dest)`), matching
`tests/test_pull.py` / `tests/test_downloads.py`'s own convention -- never
imports `core.icloud`'s real `pyicloud`-backed client, no network. View-level
steps go through the Django test client, monkeypatching
`views_module.ICloudClient` / `downloads_module._client_for_account` at the
same seams those modules' own test suites use.

Every test uses a unique account email (`t_t19_<flow>@example.com`) even
though each test's DB rows roll back per `tests/test_integration.py`'s own
documented convention -- `icloud-state/` lives directly under
`settings.WORKING_FOLDER` (a session-wide tmp dir, see `tests/_bootstrap.py`)
and is NOT rolled back between tests, so a shared account slug would let one
test's leftover state file bleed into another's.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from culler.core import culling, remote_state
from culler.core import downloads as downloads_module
from culler.core import phaseb as phaseb_module
from culler.core import previews as previews_module
from culler.core import views as views_module
from culler.core.models import DuplicatePair, Photo
from culler.core.phaseb import PhaseBProgress, run_phase_b
from culler.core.pull import PullProgress, pull_account
from culler.core.scan import ScanProgress, scan
from fixtures import build_fixture_folder


@dataclass
class FakeAsset:
    remote_id: str
    filename: str
    captured_at: datetime
    size: int
    media_type: str = "image"


class FakeICloudClient:
    """`.account`/`.list_assets(since)`/`.download(...)` duck type. `assets`
    is read fresh on every `list_assets` call (a plain list, mutable by the
    test between pulls) since real pulls are always a full re-enumeration
    (PLAN T16 decisions log) -- incremental behaviour is entirely keyed on
    already-known `remote_id`s downstream, not on what this fake yields.
    """

    def __init__(self, account: str, assets: list[FakeAsset] | None = None):
        self.account = account
        self.assets: list[FakeAsset] = list(assets or [])
        self.downloaded: list[tuple[str, str]] = []

    def list_assets(self, since):
        yield from list(self.assets)

    def download(self, remote_id: str, version: str, dest: Path) -> None:
        self.downloaded.append((remote_id, version))
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


def _pull(folder: Path, client: FakeICloudClient) -> PullProgress:
    progress = PullProgress()
    pull_account(folder, client, progress)
    return progress


def _snapshot(folder: Path) -> set[str]:
    """Relative paths of every file under `folder`, EXCLUDING `.culler/`
    (SPEC §18/§7: the DB + preview cache are a cache role the app is always
    allowed to write to -- SPEC §18 rule 1's "no files anywhere else"
    guarantee is about the *photo tree*, not the cache; `.culler/`'s own
    sqlite WAL/SHM files churn on every DB write regardless of what this
    test does, which would make a `.culler`-inclusive snapshot flaky for
    reasons unrelated to the guard being tested here).
    """
    return {
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and not p.relative_to(folder).as_posix().startswith(".culler/")
    }


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _wait_for_phase_b() -> None:
    """`scan()` auto-kicks off `phaseb.start_phase_b` on a daemon thread
    (PLAN T7 decisions log) that this test doesn't otherwise wait on. Under
    `transaction=True` (TransactionTestCase), pytest-django's teardown
    flushes every table on the *main* connection immediately after the test
    body returns -- if that Phase B thread is still mid-query on its own
    connection, the flush's DELETE can hit a genuine `sqlite3.OperationalError:
    database table is locked`, not just the harmless unhandled-thread log
    noise a plain (non-transactional) `django_db` test would see. Draining it
    here (mirrors the UI's own scan-banner polling) avoids that race without
    touching `scan.py`/`phaseb.py` themselves.
    """
    progress = phaseb_module._current_phase_b
    if progress is not None:
        _wait_for(lambda: progress.finished)


@pytest.fixture(autouse=True)
def _reset_download_worker():
    """Mirrors `tests/test_downloads.py`'s own fixture: `downloads.py`'s
    worker thread/progress are module globals, so a stray thread from a
    previous test (in this file or another) must not be mistaken for
    "still running" by this file's `start_worker` calls, and vice versa.
    """
    if downloads_module._worker_thread is not None:
        downloads_module._worker_thread.join(timeout=5)
    yield
    if downloads_module._worker_thread is not None:
        downloads_module._worker_thread.join(timeout=5)


# --- 1. attach -> pull -> mixed local+remote timeline ------------------------


@pytest.mark.django_db
def test_attach_pull_mixed_timeline_interleaved_by_capture_date(client):
    unique = "t_t19_mixed"
    account = f"{unique}@example.com"
    slug = remote_state.account_slug(account)

    build_fixture_folder(
        settings.WORKING_FOLDER,
        {
            f"{unique}/local-a.jpg": {"datetime_original": "2025:06:14 06:00:00"},
            f"{unique}/local-b.jpg": {"datetime_original": "2025:06:14 20:00:00"},
        },
    )
    scan(settings.WORKING_FOLDER, ScanProgress())

    local_a = Photo.objects.get(relative_path=f"{unique}/local-a.jpg")
    local_b = Photo.objects.get(relative_path=f"{unique}/local-b.jpg")
    assert local_a.captured_at < local_b.captured_at

    # Remote captures interleaved strictly between the two local ones --
    # computed relative to the *actual* post-scan captured_at (not the raw
    # EXIF string), so this holds regardless of the test machine's local tz.
    r1_at = local_a.captured_at + timedelta(hours=1)
    r2_at = local_b.captured_at - timedelta(hours=1)
    assert local_a.captured_at < r1_at < r2_at < local_b.captured_at

    fake_client = FakeICloudClient(account, [_asset("r1", r1_at), _asset("r2", r2_at)])
    progress = _pull(settings.WORKING_FOLDER, fake_client)
    assert progress.errors == []

    remote_r1 = Photo.objects.get(account=account, remote_id="r1")
    remote_r2 = Photo.objects.get(account=account, remote_id="r2")

    response = client.get(reverse("grid"))
    assert response.status_code == 200
    body = response.content.decode()

    def _pos(pk: int) -> int:
        needle = f'"{reverse("preview", args=[pk])}"'
        idx = body.index(needle)
        assert idx >= 0
        return idx

    assert _pos(local_a.pk) < _pos(remote_r1.pk) < _pos(remote_r2.pk) < _pos(local_b.pk)

    # Remote cells carry the cloud badge; exactly the two remote photos.
    assert body.count("badge-cloud") == 2

    # Provenance filter (account slug) shows only that account's photos.
    response = client.get(reverse("grid"), {"provenance": slug})
    body = response.content.decode()
    assert f'"{reverse("preview", args=[remote_r1.pk])}"' in body
    assert f'"{reverse("preview", args=[remote_r2.pk])}"' in body
    assert f'"{reverse("preview", args=[local_a.pk])}"' not in body
    assert f'"{reverse("preview", args=[local_b.pk])}"' not in body


# --- 2/3. full remote cull loop via views; only selected originals on disk --


@pytest.mark.django_db(transaction=True)
def test_full_remote_cull_loop_only_selected_originals_land_on_disk(client, monkeypatch):
    unique = "t_t19_cull_loop"
    account = f"{unique}@example.com"
    slug = remote_state.account_slug(account)
    state_rel = f"icloud-state/{slug}.json"

    T0 = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
    fake_client = FakeICloudClient(
        account, [_asset("r_reject", T0), _asset("r_select", T0 + timedelta(minutes=1))]
    )
    progress = _pull(settings.WORKING_FOLDER, fake_client)
    assert progress.errors == []

    photo_reject = Photo.objects.get(account=account, remote_id="r_reject")
    photo_select = Photo.objects.get(account=account, remote_id="r_select")

    # --- reject: state JSON updated, no new files anywhere except it -------
    before_reject = _snapshot(settings.WORKING_FOLDER)
    response = client.post(
        reverse("set-status", args=[photo_reject.pk]),
        {"status": "rejected", "context": "grid"},
    )
    assert response.status_code == 200
    after_reject = _snapshot(settings.WORKING_FOLDER)

    assert after_reject - before_reject <= {state_rel}
    assert before_reject - after_reject == set()  # nothing deleted, ever (rule 2)

    photo_reject.refresh_from_db()
    assert photo_reject.status == Photo.STATUS_REJECTED
    assert photo_reject.source == Photo.SOURCE_ICLOUD
    state = remote_state.load_state(settings.WORKING_FOLDER, account)
    assert state.decisions == {"r_reject": "rejected"}

    # --- select: async original download lands at selected/{slug}/{name} --
    monkeypatch.setattr(downloads_module, "_client_for_account", lambda acct: fake_client)
    before_select = _snapshot(settings.WORKING_FOLDER)
    response = client.post(
        reverse("set-status", args=[photo_select.pk]),
        {"status": "selected", "context": "grid"},
    )
    assert response.status_code == 200
    # The download itself is async -- this request never blocks on it.
    photo_select.refresh_from_db()
    assert photo_select.source == Photo.SOURCE_ICLOUD

    _wait_for(lambda: Photo.objects.get(pk=photo_select.pk).source == Photo.SOURCE_LOCAL)
    downloads_module._worker_thread.join(timeout=5)

    photo_select.refresh_from_db()
    assert photo_select.source == Photo.SOURCE_LOCAL
    assert photo_select.relative_path == f"selected/{slug}/r_select.jpg"
    dest = settings.WORKING_FOLDER / "selected" / slug / "r_select.jpg"
    assert dest.exists()

    after_select = _snapshot(settings.WORKING_FOLDER)
    # SPEC §18 acceptance core: the ONLY new file anywhere is the selected
    # original -- nothing else materialized for the rejected photo, and the
    # state file's own modification never shows up as a "new" path.
    assert after_select - before_select == {f"selected/{slug}/r_select.jpg"}

    # --- 3. only selected originals on disk --------------------------------
    selected_dir = settings.WORKING_FOLDER / "selected" / slug
    assert [p.name for p in selected_dir.iterdir()] == ["r_select.jpg"]

    # grid?status=selected shows the downloaded photo
    response = client.get(reverse("grid"), {"status": "selected"})
    assert f'"{reverse("preview", args=[photo_select.pk])}"' in response.content.decode()


# --- 4. state survives `.culler/` cache deletion ------------------------------


@pytest.mark.django_db(transaction=True)
def test_state_survives_culler_cache_deletion_and_repull(client, monkeypatch):
    unique = "t_t19_cache_loss"
    account = f"{unique}@example.com"

    T0 = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
    fake_client = FakeICloudClient(
        account,
        [
            _asset("r_reject", T0),
            _asset("r_select", T0 + timedelta(minutes=1)),
            _asset("r_optional", T0 + timedelta(minutes=2)),
        ],
    )
    assert _pull(settings.WORKING_FOLDER, fake_client).errors == []

    photo_reject = Photo.objects.get(account=account, remote_id="r_reject")
    photo_select = Photo.objects.get(account=account, remote_id="r_select")

    client.post(
        reverse("set-status", args=[photo_reject.pk]), {"status": "rejected", "context": "grid"}
    )

    monkeypatch.setattr(downloads_module, "_client_for_account", lambda acct: fake_client)
    client.post(
        reverse("set-status", args=[photo_select.pk]), {"status": "selected", "context": "grid"}
    )
    _wait_for(lambda: Photo.objects.get(pk=photo_select.pk).source == Photo.SOURCE_LOCAL)
    downloads_module._worker_thread.join(timeout=5)

    downloaded_rel_path = Photo.objects.get(pk=photo_select.pk).relative_path
    assert (settings.WORKING_FOLDER / downloaded_rel_path).exists()

    # `.culler/` cache-loss stand-in (per tests/test_integration.py's own
    # documented convention: we can't literally delete the live sqlite file
    # this session runs on, so we delete every Photo row -- the equivalent
    # event for what this test actually checks) PLUS the remote preview
    # cache files under `.culler/previews/`.
    Photo.objects.all().delete()
    for f in (settings.WORKING_FOLDER / ".culler" / "previews").glob("icloud-*"):
        f.unlink()

    progress = _pull(settings.WORKING_FOLDER, fake_client)
    assert progress.errors == []

    # rejected photo's row recreated with status rejected, from state JSON.
    reject_row = Photo.objects.get(account=account, remote_id="r_reject")
    assert reject_row.status == Photo.STATUS_REJECTED
    assert reject_row.source == Photo.SOURCE_ICLOUD

    # downloaded photo NOT resurrected as remote -- state.downloaded honored.
    assert not Photo.objects.filter(remote_id="r_select").exists()

    # a filesystem scan indexes the downloaded original as local + selected.
    scan_progress = ScanProgress()
    scan(settings.WORKING_FOLDER, scan_progress)
    assert scan_progress.errors == []
    _wait_for_phase_b()
    local_row = Photo.objects.get(relative_path=downloaded_rel_path)
    assert local_row.source == Photo.SOURCE_LOCAL
    assert local_row.status == Photo.STATUS_SELECTED

    # previews re-prefetched for the still-remote rows.
    assert previews_module.remote_preview_dest(
        settings.WORKING_FOLDER, account, "r_reject"
    ).exists()
    assert previews_module.remote_preview_dest(
        settings.WORKING_FOLDER, account, "r_optional"
    ).exists()


# --- 5. incremental re-pull ----------------------------------------------------


@pytest.mark.django_db
def test_incremental_repull_only_processes_new_asset_decisions_intact():
    unique = "t_t19_incremental"
    account = f"{unique}@example.com"

    T0 = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
    fake_client = FakeICloudClient(
        account, [_asset("r1", T0), _asset("r2", T0 + timedelta(minutes=1))]
    )
    assert _pull(settings.WORKING_FOLDER, fake_client).errors == []

    photo_r1 = Photo.objects.get(account=account, remote_id="r1")
    culling.apply_status_any(settings.WORKING_FOLDER, photo_r1, Photo.STATUS_REJECTED)

    # Second pull's listing repeats both known assets AND yields one new one.
    fake_client.assets.append(_asset("r3", T0 + timedelta(minutes=2)))

    progress = _pull(settings.WORKING_FOLDER, fake_client)

    assert progress.total == 1  # only r3 -- both previews already cached, no repairs
    assert progress.done == 1
    assert progress.errors == []
    assert Photo.objects.filter(account=account).count() == 3
    assert Photo.objects.filter(account=account, remote_id="r3").exists()

    photo_r1.refresh_from_db()
    assert photo_r1.status == Photo.STATUS_REJECTED  # decision intact, no duplicate row
    assert Photo.objects.filter(account=account, remote_id="r1").count() == 1


# --- 6. expired session UX -----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_expired_session_reauth_message_and_graceful_select_failure(client, monkeypatch):
    unique = "t_t19_expired"
    account = f"{unique}@example.com"

    T0 = datetime(2025, 6, 14, 10, 0, 0, tzinfo=UTC)
    fake_client = FakeICloudClient(account, [_asset("r1", T0)])
    assert _pull(settings.WORKING_FOLDER, fake_client).errors == []
    photo = Photo.objects.get(account=account, remote_id="r1")

    class _ExpiredClient:
        @classmethod
        def from_session(cls, email):
            return None

    monkeypatch.setattr(views_module, "ICloudClient", _ExpiredClient)

    response = client.post(reverse("account-pull"), {"account": account})
    assert response.status_code == 200
    body = response.content.decode()
    assert "re-authenticate" in body
    assert account in body

    # select on a remote photo whose account has no live client -- must
    # never bubble an exception up to the response.
    monkeypatch.setattr(downloads_module, "_client_for_account", lambda acct: None)
    response = client.post(
        reverse("set-status", args=[photo.pk]), {"status": "selected", "context": "grid"}
    )
    assert response.status_code == 200

    _wait_for(
        lambda: (
            downloads_module._worker_thread is not None
            and not downloads_module._worker_thread.is_alive()
        )
    )

    photo.refresh_from_db()
    assert photo.status == Photo.STATUS_SELECTED
    assert photo.source == Photo.SOURCE_ICLOUD  # stays pending -- no download happened
    assert downloads_module._last_progress is not None
    assert any(
        account in e and "re-authentication" in e for e in downloads_module._last_progress.errors
    )


# --- 7. dupes interaction guard -------------------------------------------------


@pytest.mark.django_db
def test_remote_rows_never_join_dupe_grouping_after_phase_b():
    unique = "t_t19_dupes_guard"
    account = f"{unique}@example.com"

    build_fixture_folder(
        settings.WORKING_FOLDER, {f"{unique}/a.jpg": {"datetime_original": "2025:06:14 10:00:00"}}
    )
    # Byte-identical copy -> exact-dupe pair once Phase B hashes both.
    shutil.copyfile(
        settings.WORKING_FOLDER / f"{unique}/a.jpg",
        settings.WORKING_FOLDER / f"{unique}/a-copy.jpg",
    )
    scan(settings.WORKING_FOLDER, ScanProgress())

    photo_a = Photo.objects.get(relative_path=f"{unique}/a.jpg")
    base = photo_a.captured_at

    # Remote assets captured within the ±8s near-dupe window of the local
    # exact-dupe pair -- if the source="icloud" exclusion in phaseb.py ever
    # regresses, these would be the ones that leak into pairing.
    fake_client = FakeICloudClient(
        account, [_asset("r1", base), _asset("r2", base + timedelta(seconds=2))]
    )
    assert _pull(settings.WORKING_FOLDER, fake_client).errors == []

    progress = PhaseBProgress()
    run_phase_b(settings.WORKING_FOLDER, progress)
    assert progress.errors == []

    remote_pks = set(
        Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=account).values_list(
            "pk", flat=True
        )
    )
    assert len(remote_pks) == 2  # sanity: both remote rows exist

    pair_pks: set[int] = set()
    for pair in DuplicatePair.objects.all():
        pair_pks.add(pair.photo_a_id)
        pair_pks.add(pair.photo_b_id)
    assert not (pair_pks & remote_pks)

    # Remote rows never get hashed (no local file), so they can never enter
    # exact-dupe grouping either.
    assert not Photo.objects.filter(source=Photo.SOURCE_ICLOUD).exclude(sha256=None).exists()

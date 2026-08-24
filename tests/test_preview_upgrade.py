"""On-demand "medium" preview upgrade worker (PLAN T22). No network, no real
`pyicloud`/`ICloudClient` -- everything is faked at the
`preview_upgrade._client_for_account` seam, mirroring `test_downloads.py`'s
pattern. None of this module touches the DB, so `Photo` instances here are
never saved (same convention as `test_previews.py`'s pure-function tests).
"""

from __future__ import annotations

import threading
import time

import pytest

from maier.core import preview_upgrade, previews
from maier.core.models import Photo


def _remote_photo(remote_id: str, account: str = "luis@example.com", **overrides) -> Photo:
    kwargs = dict(media_type=Photo.MEDIA_IMAGE)
    kwargs.update(overrides)
    return Photo(source=Photo.SOURCE_ICLOUD, account=account, remote_id=remote_id, **kwargs)


class FakeClient:
    def __init__(self, payload: bytes = b"medium-bytes", fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.payload = payload
        self.fail = fail

    def download(self, remote_id, version, dest):
        self.calls.append((remote_id, version))
        if self.fail:
            raise RuntimeError("simulated download failure")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)


def _wait_for_content(path, timeout: float = 5) -> None:
    """Wait until `path` exists AND has bytes -- `dest.exists()` alone races
    the worker thread's plain `write_bytes` (file created before content
    lands; flaked on the v0.1.0 release runner, 2026-08-24)."""
    _wait_for(lambda: path.exists() and len(path.read_bytes()) > 0, timeout=timeout)


def _wait_for(predicate, timeout: float = 5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


@pytest.fixture(autouse=True)
def _reset_state():
    """Module globals (pending set + per-account client cache) persist across
    tests (there's no per-run boundary like downloads.py's worker loop) --
    clear them so one test's monkeypatched client/account can't leak into
    the next.
    """
    preview_upgrade._pending.clear()
    preview_upgrade._clients.clear()
    yield
    preview_upgrade._pending.clear()
    preview_upgrade._clients.clear()


# --- happy path -------------------------------------------------------------


def test_enqueue_fetches_medium_to_expected_dest(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(preview_upgrade, "_client_for_account", lambda account: client)
    photo = _remote_photo("r1")

    preview_upgrade.enqueue_medium(tmp_path, photo)

    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r1")
    _wait_for_content(dest)
    assert dest.read_bytes() == b"medium-bytes"
    assert client.calls == [("r1", "medium")]


def test_video_uses_medium_image_version(tmp_path, monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(preview_upgrade, "_client_for_account", lambda account: client)
    photo = _remote_photo("r2", media_type=Photo.MEDIA_VIDEO)

    preview_upgrade.enqueue_medium(tmp_path, photo)

    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r2")
    _wait_for_content(dest)
    assert client.calls == [("r2", "medium_image")]


# --- no-op cases --------------------------------------------------------


def test_noop_when_medium_already_cached(tmp_path, monkeypatch):
    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r3")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"already there")
    calls: list[str] = []
    monkeypatch.setattr(
        preview_upgrade, "_client_for_account", lambda account: calls.append(account)
    )
    photo = _remote_photo("r3")

    preview_upgrade.enqueue_medium(tmp_path, photo)
    time.sleep(0.1)

    assert calls == []
    assert dest.read_bytes() == b"already there"


def test_noop_for_local_photo(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        preview_upgrade, "_client_for_account", lambda account: calls.append(account)
    )
    photo = Photo(source=Photo.SOURCE_LOCAL, relative_path="a.jpg", media_type=Photo.MEDIA_IMAGE)

    preview_upgrade.enqueue_medium(tmp_path, photo)
    time.sleep(0.1)

    assert calls == []


def test_noop_when_remote_id_missing(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        preview_upgrade, "_client_for_account", lambda account: calls.append(account)
    )
    photo = Photo(
        source=Photo.SOURCE_ICLOUD,
        account="luis@example.com",
        remote_id=None,
        media_type=Photo.MEDIA_IMAGE,
    )

    preview_upgrade.enqueue_medium(tmp_path, photo)
    time.sleep(0.1)

    assert calls == []


# --- session expired ---------------------------------------------------


def test_session_none_drops_silently(tmp_path, monkeypatch):
    monkeypatch.setattr(preview_upgrade, "_client_for_account", lambda account: None)
    photo = _remote_photo("r4")

    preview_upgrade.enqueue_medium(tmp_path, photo)

    _wait_for(lambda: ("luis@example.com", "r4") not in preview_upgrade._pending)
    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r4")
    assert not dest.exists()


# --- single-flight -------------------------------------------------------


def test_single_flight_per_remote_id(tmp_path, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    class BlockingClient(FakeClient):
        def download(self, remote_id, version, dest):
            started.set()
            release.wait(timeout=5)
            super().download(remote_id, version, dest)

    client = BlockingClient()
    monkeypatch.setattr(preview_upgrade, "_client_for_account", lambda account: client)
    photo = _remote_photo("r5")

    preview_upgrade.enqueue_medium(tmp_path, photo)
    assert started.wait(timeout=5)
    # A second enqueue while the first fetch is still in flight for the same
    # (account, remote_id) must not submit a duplicate fetch.
    preview_upgrade.enqueue_medium(tmp_path, photo)
    release.set()

    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r5")
    _wait_for_content(dest)
    time.sleep(0.1)  # let a stray duplicate submission (were there a bug) finish too
    assert client.calls == [("r5", "medium")]


# --- failure + retry -----------------------------------------------------


def test_failure_drops_from_pending_and_retries_on_next_enqueue(tmp_path, monkeypatch):
    client = FakeClient(fail=True)
    monkeypatch.setattr(preview_upgrade, "_client_for_account", lambda account: client)
    photo = _remote_photo("r6")

    preview_upgrade.enqueue_medium(tmp_path, photo)
    _wait_for(lambda: ("luis@example.com", "r6") not in preview_upgrade._pending)
    dest = previews.remote_medium_dest(tmp_path, "luis@example.com", "r6")
    assert not dest.exists()

    client.fail = False
    preview_upgrade.enqueue_medium(tmp_path, photo)
    _wait_for_content(dest)
    assert client.calls == [("r6", "medium"), ("r6", "medium")]


# --- client caching --------------------------------------------------------


def test_client_resolved_once_per_account(tmp_path, monkeypatch):
    calls: list[str] = []

    def _resolve(account):
        calls.append(account)
        return FakeClient()

    monkeypatch.setattr(preview_upgrade, "_client_for_account", _resolve)
    photo_a = _remote_photo("r7")
    photo_b = _remote_photo("r8")

    preview_upgrade.enqueue_medium(tmp_path, photo_a)
    dest_a = previews.remote_medium_dest(tmp_path, "luis@example.com", "r7")
    _wait_for_content(dest_a)

    preview_upgrade.enqueue_medium(tmp_path, photo_b)
    dest_b = previews.remote_medium_dest(tmp_path, "luis@example.com", "r8")
    _wait_for_content(dest_b)

    assert calls == ["luis@example.com"]

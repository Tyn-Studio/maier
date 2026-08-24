"""No network, no real pyicloud service: everything is faked at the
`icloud.PyiCloudService` import seam.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from django.conf import settings
from pyicloud.exceptions import (
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudServiceUnavailable,
)

from maier.core import icloud

# --- fakes ------------------------------------------------------------


class FakeAsset:
    def __init__(
        self,
        id: str,
        filename: str,
        created: datetime,
        size: int | None,
        item_type: str,
        *,
        versions: dict | None = None,
        download_bytes: bytes = b"",
        download_error: Exception | None = None,
        is_live_photo: bool = False,
        live_video_bytes: bytes = b"",
        live_video_error: Exception | None = None,
    ) -> None:
        self.id = id
        self.filename = filename
        self.created = created
        self.size = size
        self.item_type = item_type
        self.versions = (
            versions if versions is not None else {"thumb": {}, "medium": {}, "original": {}}
        )
        self._download_bytes = download_bytes
        self._download_error = download_error
        self.is_live_photo = is_live_photo
        self._live_video_bytes = live_video_bytes
        self._live_video_error = live_video_error

    def download(self, version: str) -> bytes | None:
        if version == "original_video":
            if self._live_video_error is not None:
                raise self._live_video_error
            return self._live_video_bytes
        if self._download_error is not None:
            raise self._download_error
        return self._download_bytes


class FakePhotoAlbum:
    def __init__(self, assets: list[FakeAsset]) -> None:
        self._assets = assets

    def __iter__(self):
        return iter(self._assets)

    def get(self, remote_id: str) -> FakeAsset | None:
        for asset in self._assets:
            if asset.id == remote_id:
                return asset
        return None


class FakePhotos:
    def __init__(self, assets: list[FakeAsset]) -> None:
        self.all = FakePhotoAlbum(assets)


class FakeService:
    def __init__(
        self,
        apple_id: str,
        cookie_directory: str,
        assets: list[FakeAsset],
        requires_2fa: bool,
        twofa_code: str,
    ) -> None:
        self.apple_id = apple_id
        self.cookie_directory = cookie_directory
        self.requires_2fa = requires_2fa
        self._twofa_code = twofa_code
        self.photos = FakePhotos(assets)

    def validate_2fa_code(self, code: str) -> bool:
        if code == self._twofa_code:
            self.requires_2fa = False
            return True
        return False


class FakeServiceFactory:
    """Stand-in for the `PyiCloudService` class, configured per test."""

    def __init__(
        self,
        *,
        fail_login: Exception | None = None,
        requires_2fa: bool = False,
        assets: list[FakeAsset] | None = None,
        valid_sessions: set[str] | None = None,
        twofa_code: str = "123456",
        fail_session_reuse: Exception | None = None,
    ) -> None:
        self.fail_login = fail_login
        self.requires_2fa_initial = requires_2fa
        self.assets = assets or []
        self.valid_sessions = valid_sessions or set()
        self.twofa_code = twofa_code
        self.fail_session_reuse = fail_session_reuse
        self.calls: list[tuple[str, str | None, str]] = []

    def __call__(self, apple_id, password=None, cookie_directory=None, **kwargs):
        self.calls.append((apple_id, password, cookie_directory))
        is_session_reuse = password == ""
        if is_session_reuse:
            if self.fail_session_reuse is not None:
                raise self.fail_session_reuse
            if apple_id not in self.valid_sessions:
                raise PyiCloudFailedLoginException("no valid session")
            return FakeService(apple_id, cookie_directory, self.assets, False, self.twofa_code)

        if self.fail_login is not None:
            raise self.fail_login
        return FakeService(
            apple_id, cookie_directory, self.assets, self.requires_2fa_initial, self.twofa_code
        )


@pytest.fixture(autouse=True)
def _global_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", tmp_path / "global-data")
    return tmp_path / "global-data"


def _install_factory(monkeypatch, **kwargs) -> FakeServiceFactory:
    factory = FakeServiceFactory(**kwargs)
    monkeypatch.setattr(icloud, "PyiCloudService", factory)
    return factory


# --- login --------------------------------------------------------------


def test_login_ok(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=False)

    client = icloud.ICloudClient.login("user@example.com", "hunter2")

    assert isinstance(client, icloud.ICloudClient)
    assert client.account == "user@example.com"


def test_login_creates_session_dir_under_global_data_dir(monkeypatch, _global_data_dir):
    _install_factory(monkeypatch, requires_2fa=False)

    icloud.ICloudClient.login("User@Example.com", "hunter2")

    expected = _global_data_dir / "icloud-sessions" / icloud.account_slug("User@Example.com")
    assert expected.is_dir()


def test_login_raises_two_factor_required_with_usable_client(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=True, twofa_code="000000")

    with pytest.raises(icloud.TwoFactorRequired) as exc_info:
        icloud.ICloudClient.login("user@example.com", "hunter2")

    pending = exc_info.value.client
    assert isinstance(pending, icloud.ICloudClient)
    assert pending.account == "user@example.com"
    assert pending.submit_2fa("000000") is True


def test_login_bad_credentials_raises_icloud_error_not_pyicloud_type(monkeypatch):
    _install_factory(
        monkeypatch, fail_login=PyiCloudFailedLoginException("Invalid email/password combination.")
    )

    with pytest.raises(icloud.ICloudError):
        icloud.ICloudClient.login("user@example.com", "wrongpass")


@pytest.mark.parametrize(
    "exc",
    [
        PyiCloudAPIResponseException("boom", 500),
        PyiCloudServiceUnavailable("Photos service not available"),
        OSError("disk full"),
    ],
)
def test_login_wraps_every_pyicloud_error_type(monkeypatch, exc):
    _install_factory(monkeypatch, fail_login=exc)

    with pytest.raises(icloud.ICloudError):
        icloud.ICloudClient.login("user@example.com", "hunter2")


# --- submit_2fa -----------------------------------------------------------


def test_submit_2fa_failure_returns_false(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=True, twofa_code="000000")

    with pytest.raises(icloud.TwoFactorRequired) as exc_info:
        icloud.ICloudClient.login("user@example.com", "hunter2")

    assert exc_info.value.client.submit_2fa("999999") is False


def test_submit_2fa_wraps_pyicloud_errors(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=True)

    with pytest.raises(icloud.TwoFactorRequired) as exc_info:
        icloud.ICloudClient.login("user@example.com", "hunter2")

    client = exc_info.value.client
    client._service.validate_2fa_code = lambda code: (_ for _ in ()).throw(
        PyiCloudAPIResponseException("boom", 500)
    )
    with pytest.raises(icloud.ICloudError):
        client.submit_2fa("123456")


# --- from_session ---------------------------------------------------------


def test_from_session_returns_none_when_no_session_dir(monkeypatch):
    _install_factory(monkeypatch)

    assert icloud.ICloudClient.from_session("nobody@example.com") is None


def test_from_session_returns_client_when_session_valid(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=False, valid_sessions={"user@example.com"})
    icloud.ICloudClient.login("user@example.com", "hunter2")  # creates the session dir

    client = icloud.ICloudClient.from_session("user@example.com")

    assert isinstance(client, icloud.ICloudClient)
    assert client.account == "user@example.com"


def test_from_session_returns_none_when_session_invalid(monkeypatch):
    _install_factory(monkeypatch, requires_2fa=False, valid_sessions=set())
    icloud.ICloudClient.login("user@example.com", "hunter2")  # creates the session dir

    assert icloud.ICloudClient.from_session("user@example.com") is None


def test_from_session_returns_none_when_session_still_requires_2fa(monkeypatch):
    factory = _install_factory(monkeypatch, requires_2fa=False, valid_sessions={"user@example.com"})
    icloud.ICloudClient.login("user@example.com", "hunter2")

    def call_with_2fa(apple_id, password=None, cookie_directory=None, **kwargs):
        service = factory(apple_id, password=password, cookie_directory=cookie_directory, **kwargs)
        service.requires_2fa = True
        return service

    # `icloud.PyiCloudService` is called as a plain function, so replacing
    # the module attribute (rather than the `factory` instance's `__call__`)
    # is what actually intercepts the next construction -- an instance-level
    # `__call__` override wouldn't be consulted by `factory(...)`.
    monkeypatch.setattr(icloud, "PyiCloudService", call_with_2fa)

    assert icloud.ICloudClient.from_session("user@example.com") is None


def test_from_session_never_raises_on_network_error(monkeypatch):
    _install_factory(
        monkeypatch,
        requires_2fa=False,
        fail_session_reuse=PyiCloudAPIResponseException("network down", 503),
    )
    icloud.ICloudClient.login("user@example.com", "hunter2")

    assert icloud.ICloudClient.from_session("user@example.com") is None


# --- list_assets ------------------------------------------------------


def _client_with_assets(monkeypatch, assets: list[FakeAsset]) -> icloud.ICloudClient:
    _install_factory(monkeypatch, requires_2fa=False, assets=assets)
    return icloud.ICloudClient.login("user@example.com", "hunter2")


def test_list_assets_maps_fields(monkeypatch):
    aware = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    naive = datetime(2026, 1, 3, 4, 5, 6)
    assets = [
        FakeAsset("a1", "IMG_0001.jpg", aware, 12345, "image"),
        FakeAsset("a2", "IMG_0002.mov", naive, 999, "movie"),
    ]
    client = _client_with_assets(monkeypatch, assets)

    results = list(client.list_assets(since=None))

    assert len(results) == 2
    first, second = results
    assert first == icloud.RemoteAsset("a1", "IMG_0001.jpg", aware, 12345, "image")
    assert second.remote_id == "a2"
    assert second.media_type == "video"
    assert second.captured_at.tzinfo is not None
    assert second.captured_at == naive.replace(tzinfo=UTC)


def test_list_assets_normalizes_non_utc_aware_datetime(monkeypatch):
    tz = timezone(timedelta(hours=-5))
    local_time = datetime(2026, 1, 1, 10, 0, 0, tzinfo=tz)
    assets = [FakeAsset("a1", "a.jpg", local_time, 1, "image")]
    client = _client_with_assets(monkeypatch, assets)

    (result,) = list(client.list_assets(since=None))

    assert result.captured_at == local_time.astimezone(UTC)
    assert result.captured_at.tzinfo is UTC


def test_list_assets_missing_size_defaults_to_zero(monkeypatch):
    assets = [FakeAsset("a1", "a.jpg", datetime(2026, 1, 1, tzinfo=UTC), None, "image")]
    client = _client_with_assets(monkeypatch, assets)

    (result,) = list(client.list_assets(since=None))

    assert result.size == 0


def test_list_assets_since_filter(monkeypatch):
    base = datetime(2026, 1, 1, tzinfo=UTC)
    assets = [
        FakeAsset("old", "old.jpg", base, 1, "image"),
        FakeAsset("boundary", "boundary.jpg", base + timedelta(hours=1), 1, "image"),
        FakeAsset("new", "new.jpg", base + timedelta(hours=2), 1, "image"),
    ]
    client = _client_with_assets(monkeypatch, assets)

    results = list(client.list_assets(since=base + timedelta(hours=1)))

    assert [r.remote_id for r in results] == ["new"]


def test_list_assets_wraps_pyicloud_errors(monkeypatch):
    class BrokenAlbum:
        def __iter__(self):
            raise PyiCloudAPIResponseException("boom", 500)

    _install_factory(monkeypatch, requires_2fa=False)
    client = icloud.ICloudClient.login("user@example.com", "hunter2")
    client._service.photos.all = BrokenAlbum()

    with pytest.raises(icloud.ICloudError):
        list(client.list_assets(since=None))


# --- download -----------------------------------------------------------


def test_download_writes_exact_bytes_via_tmp_replace(monkeypatch, tmp_path):
    payload = b"hello world" * 1000
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            len(payload),
            "image",
            download_bytes=payload,
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))  # populate the asset cache

    dest = tmp_path / "out" / "a.jpg"
    client.download("a1", "original", dest)

    assert dest.read_bytes() == payload
    assert not (dest.parent / "a.jpg.part").exists()
    assert list(dest.parent.iterdir()) == [dest]


def test_download_looks_up_asset_on_cache_miss(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1", "a.jpg", datetime(2026, 1, 1, tzinfo=UTC), 5, "image", download_bytes=b"hello"
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    # No prior list_assets() call in this instance -- forces the get() path.

    dest = tmp_path / "a.jpg"
    client.download("a1", "original", dest)

    assert dest.read_bytes() == b"hello"


def test_download_unknown_asset_raises_icloud_error(monkeypatch, tmp_path):
    client = _client_with_assets(monkeypatch, [])

    with pytest.raises(icloud.ICloudError):
        client.download("missing", "original", tmp_path / "a.jpg")


def test_download_falls_back_to_original_when_version_missing(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            versions={"original": {}},  # no "thumb"
            download_bytes=b"original-bytes",
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.jpg"
    client.download("a1", "thumb", dest)

    assert dest.read_bytes() == b"original-bytes"


def test_download_rejects_unknown_version_name(monkeypatch, tmp_path):
    assets = [FakeAsset("a1", "a.jpg", datetime(2026, 1, 1, tzinfo=UTC), 5, "image")]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    with pytest.raises(icloud.ICloudError):
        client.download("a1", "huge", tmp_path / "a.jpg")


def test_download_raises_on_asset_download_failure_and_leaves_no_partial(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            download_error=PyiCloudAPIResponseException("connection reset", 500),
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.jpg"
    with pytest.raises(icloud.ICloudError):
        client.download("a1", "original", dest)

    assert not dest.exists()
    assert not (tmp_path / "a.jpg.part").exists()


def test_download_raises_and_cleans_up_tmp_file_when_replace_fails(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            download_bytes=b"partial-write",
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    def broken_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(icloud.os, "replace", broken_replace)

    dest = tmp_path / "a.jpg"
    with pytest.raises(icloud.ICloudError):
        client.download("a1", "original", dest)

    assert not dest.exists()
    assert not (tmp_path / "a.jpg.part").exists()


def test_download_no_data_raises_icloud_error(monkeypatch, tmp_path):
    assets = [
        FakeAsset("a1", "a.jpg", datetime(2026, 1, 1, tzinfo=UTC), 5, "image", download_bytes=None)
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.jpg"
    with pytest.raises(icloud.ICloudError):
        client.download("a1", "original", dest)

    assert not dest.exists()


# --- list_assets: is_live (T20) -------------------------------------------


def test_list_assets_captures_is_live_flag(monkeypatch):
    assets = [
        FakeAsset(
            "a1",
            "IMG_0001.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            is_live_photo=True,
        ),
        FakeAsset("a2", "IMG_0002.jpg", datetime(2026, 1, 1, tzinfo=UTC), 5, "image"),
    ]
    client = _client_with_assets(monkeypatch, assets)

    results = list(client.list_assets(since=None))

    assert results[0].is_live is True
    assert results[1].is_live is False


# --- download_live_video (T20) ---------------------------------------------


def test_download_live_video_returns_false_when_not_live_photo(monkeypatch, tmp_path):
    assets = [
        FakeAsset("a1", "a.jpg", datetime(2026, 1, 1, tzinfo=UTC), 5, "image", is_live_photo=False)
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.mov"
    assert client.download_live_video("a1", dest) is False
    assert not dest.exists()


def test_download_live_video_writes_bytes_and_returns_true(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            is_live_photo=True,
            versions={"thumb": {}, "medium": {}, "original": {}, "original_video": {}},
            live_video_bytes=b"movie-bytes",
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.mov"
    assert client.download_live_video("a1", dest) is True
    assert dest.read_bytes() == b"movie-bytes"
    assert not (dest.parent / "a.mov.part").exists()


def test_download_live_video_returns_false_when_resource_missing_despite_flag(
    monkeypatch, tmp_path
):
    # Defensive branch: is_live_photo True but "original_video" absent from
    # .versions shouldn't normally happen (both gate on the same underlying
    # CloudKit field -- see module docstring) but must degrade gracefully.
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            is_live_photo=True,
            versions={"thumb": {}, "medium": {}, "original": {}},
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    assert client.download_live_video("a1", tmp_path / "a.mov") is False


def test_download_live_video_wraps_download_failure_as_icloud_error(monkeypatch, tmp_path):
    assets = [
        FakeAsset(
            "a1",
            "a.jpg",
            datetime(2026, 1, 1, tzinfo=UTC),
            5,
            "image",
            is_live_photo=True,
            versions={"thumb": {}, "medium": {}, "original": {}, "original_video": {}},
            live_video_error=PyiCloudAPIResponseException("boom", 500),
        )
    ]
    client = _client_with_assets(monkeypatch, assets)
    list(client.list_assets(since=None))

    dest = tmp_path / "a.mov"
    with pytest.raises(icloud.ICloudError):
        client.download_live_video("a1", dest)
    assert not dest.exists()


def test_download_live_video_unknown_asset_raises_icloud_error(monkeypatch, tmp_path):
    client = _client_with_assets(monkeypatch, [])

    with pytest.raises(icloud.ICloudError):
        client.download_live_video("missing", tmp_path / "a.mov")


# --- account_slug -----------------------------------------------------


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("User@Example.com", "user-example-com"),
        ("first.last+tag@sub.example.co.uk", "first-last-tag-sub-example-co-uk"),
        ("  spaced@example.com  ", "spaced-example-com"),
        ("!!!", "account"),
    ],
)
def test_account_slug(email, expected):
    assert icloud.account_slug(email) == expected


def test_session_dir_under_monkeypatched_global_data_dir(_global_data_dir):
    path = icloud._session_dir("user@example.com")
    assert path == _global_data_dir / "icloud-sessions" / "user-example-com"


# --- forget_session (PLAN T21: disconnect account) -------------------------


def test_forget_session_deletes_session_dir(_global_data_dir):
    session_dir = icloud._session_dir("user@example.com")
    session_dir.mkdir(parents=True)
    (session_dir / "cookie.txt").write_text("token")

    icloud.ICloudClient.forget_session("user@example.com")

    assert not session_dir.exists()


def test_forget_session_missing_dir_is_noop(_global_data_dir):
    icloud.ICloudClient.forget_session("never-logged-in@example.com")  # must not raise


# --- public surface guard (hard rule 8: read-only) ------------------------


def test_public_surface_is_read_only():
    public_callables = {
        name
        for name in dir(icloud.ICloudClient)
        if not name.startswith("_") and callable(getattr(icloud.ICloudClient, name))
    }
    assert public_callables == {
        "login",
        "from_session",
        "submit_2fa",
        "list_assets",
        "download",
        "download_live_video",
        # read-only cache introspection (pull.py defers cache-miss bulk
        # fetches during enumeration -- 2026-08-24 worker-starvation fix)
        "has_asset_cached",
        # T21: deletes OUR OWN session-token store on disk -- never touches
        # pyicloud/Apple's API, so it doesn't violate read-only (see
        # `forget_session`'s docstring in icloud.py).
        "forget_session",
    }


def test_remote_asset_is_a_plain_dataclass_with_no_methods():
    field_names = {f for f in icloud.RemoteAsset.__dataclass_fields__}
    assert field_names == {"remote_id", "filename", "captured_at", "size", "media_type", "is_live"}


def test_module_defines_no_delete_or_write_helpers():
    forbidden = {"delete", "upload", "favorite", "unfavorite", "trust", "create_album", "logout"}
    public_names = {name for name in dir(icloud) if not name.startswith("_")}
    assert not (forbidden & public_names)


def test_download_annotation_documents_path_argument():
    # icloud.py uses `from __future__ import annotations`, so annotations are
    # unevaluated strings at runtime -- compare against the source spelling.
    assert icloud.ICloudClient.download.__annotations__.get("dest") == "Path"

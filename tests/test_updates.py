"""In-app update notification (PLAN T27). All network-free: `urlopen` is
monkeypatched at the seam `updates._fetch_latest_release` calls into
(`urllib.request.urlopen`), matching the pattern in test_exiftool.py's
`_fetch`/network-seam tests.
"""

import json
import time
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest
from django.conf import settings

from maier import __version__
from maier.core import updates


@pytest.fixture(autouse=True)
def _global_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "global-data"
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", data_dir)
    return data_dir


@pytest.fixture(autouse=True)
def _reset_updates_cache():
    updates._reset_cache_for_tests()
    yield
    updates._reset_cache_for_tests()


class _FakeResponse:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _urlopen_returning(payload: dict):
    def _fake_urlopen(request, timeout=None):
        return _FakeResponse(payload)

    return _fake_urlopen


def _urlopen_raising(exc: Exception):
    def _fake_urlopen(request, timeout=None):
        raise exc

    return _fake_urlopen


def _urlopen_returning_garbage():
    class _GarbageResponse:
        def read(self):
            return b"not json{{{"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def _fake_urlopen(request, timeout=None):
        return _GarbageResponse()

    return _fake_urlopen


# --- _parse_version -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1.2.3", (1, 2, 3)),
        ("v1.2.3", (1, 2, 3)),
        ("V1.2.3", (1, 2, 3)),
        ("0.1.0", (0, 1, 0)),
        ("2", (2,)),
        ("1.2.3.4", (1, 2, 3, 4)),
    ],
)
def test_parse_version_valid(raw, expected):
    assert updates._parse_version(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "v1.2.3-beta",
        "1.2.3-rc1",
        "not-a-version",
        "",
        "v",
        "1.2.3+build5",
        "latest",
    ],
)
def test_parse_version_junk_returns_none(raw):
    assert updates._parse_version(raw) is None


# --- check_for_update -----------------------------------------------------


def test_check_for_update_returns_info_when_newer(monkeypatch):
    major, minor, patch = (int(p) for p in __version__.split("."))
    newer_tag = f"v{major}.{minor}.{patch + 1}"
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning(
            {"tag_name": newer_tag, "html_url": "https://github.com/Tyn-Studio/maier/releases/x"}
        ),
    )

    result = updates.check_for_update()

    assert result == updates.UpdateInfo(
        version=f"{major}.{minor}.{patch + 1}",
        url="https://github.com/Tyn-Studio/maier/releases/x",
    )


def test_check_for_update_returns_none_when_equal(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning(
            {"tag_name": f"v{__version__}", "html_url": "https://github.com/Tyn-Studio/maier"}
        ),
    )

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_when_older(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning(
            {"tag_name": "v0.0.1", "html_url": "https://github.com/Tyn-Studio/maier"}
        ),
    )

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_raising(
            urllib.error.HTTPError(updates.RELEASES_API, 404, "Not Found", None, None)
        ),
    )

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_timeout(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_raising(TimeoutError("timed out")),
    )

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_url_error(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_raising(urllib.error.URLError("no network")),
    )

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_garbage_json(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _urlopen_returning_garbage())

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_missing_fields(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _urlopen_returning({"oops": True}))

    assert updates.check_for_update() is None


def test_check_for_update_returns_none_on_unparseable_remote_tag(monkeypatch):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning(
            {"tag_name": "not-a-version", "html_url": "https://github.com/Tyn-Studio/maier"}
        ),
    )

    assert updates.check_for_update() is None


# --- 24h cache --------------------------------------------------------------


def test_fresh_check_writes_state_file(monkeypatch, _global_data_dir):
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning({"tag_name": "v0.0.1", "html_url": "https://x"}),
    )

    result = updates._check_and_cache()

    assert result is None  # v0.0.1 is older than __version__
    state_path = _global_data_dir / updates._STATE_FILENAME
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert "checked_at" in data
    assert data["result"] is None


def test_second_call_within_window_reads_file_not_network(monkeypatch):
    calls = []

    def _fake_urlopen(request, timeout=None):
        calls.append(1)
        return _FakeResponse({"tag_name": "v0.0.1", "html_url": "https://x"})

    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen)

    updates._check_and_cache()
    assert len(calls) == 1

    updates._reset_cache_for_tests()  # simulate a fresh process (in-memory only)
    updates._check_and_cache()
    assert len(calls) == 1  # second call served from the state file, not the network


def test_stale_file_triggers_recheck(monkeypatch, _global_data_dir):
    calls = []

    def _fake_urlopen(request, timeout=None):
        calls.append(1)
        return _FakeResponse({"tag_name": "v0.0.1", "html_url": "https://x"})

    monkeypatch.setattr(updates.urllib.request, "urlopen", _fake_urlopen)

    stale_time = datetime.now(UTC) - timedelta(hours=25)
    updates._save_state(stale_time, None)

    updates._check_and_cache()

    assert len(calls) == 1


def test_corrupt_state_file_tolerated(monkeypatch, _global_data_dir):
    _global_data_dir.mkdir(parents=True, exist_ok=True)
    (_global_data_dir / updates._STATE_FILENAME).write_text("not json{{{")

    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning({"tag_name": "v0.0.1", "html_url": "https://x"}),
    )

    result = updates._check_and_cache()

    assert result is None
    # tolerated: the corrupt file was quietly replaced with a valid one
    data = json.loads((_global_data_dir / updates._STATE_FILENAME).read_text())
    assert "checked_at" in data


def test_latest_known_update_reflects_cached_result(monkeypatch):
    major, minor, patch = (int(p) for p in __version__.split("."))
    newer_tag = f"v{major}.{minor}.{patch + 1}"
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning({"tag_name": newer_tag, "html_url": "https://x"}),
    )

    assert updates.latest_known_update() is None  # nothing checked yet this process

    updates._check_and_cache()

    result = updates.latest_known_update()
    assert result is not None
    assert result.version == f"{major}.{minor}.{patch + 1}"


def test_start_background_check_populates_cache(monkeypatch):
    major, minor, patch = (int(p) for p in __version__.split("."))
    newer_tag = f"v{major}.{minor}.{patch + 1}"
    monkeypatch.setattr(
        updates.urllib.request,
        "urlopen",
        _urlopen_returning({"tag_name": newer_tag, "html_url": "https://x"}),
    )

    updates.start_background_check()

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if updates.latest_known_update() is not None:
            break
        time.sleep(0.02)

    result = updates.latest_known_update()
    assert result is not None
    assert result.version == f"{major}.{minor}.{patch + 1}"


def test_start_background_check_never_raises_on_broken_network(monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", _urlopen_raising(OSError("boom")))

    updates.start_background_check()  # must not raise synchronously

    time.sleep(0.05)
    assert updates.latest_known_update() is None

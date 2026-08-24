"""In-app update notification (CTO request, PLAN T27): Maier never
self-modifies -- this module only checks whether a newer release exists on
GitHub and surfaces a "Download" link. It never downloads or applies
anything.

The repo is currently PRIVATE (`Tyn-Studio/maier`); the GitHub releases API
404s for a private repo without auth, which this module treats as just
another "no update" outcome -- it is designed to start working silently the
moment the repo goes public, with no code change required.

Caching: a background daemon thread (`start_background_check`) runs
`check_for_update` at most once per 24h *across restarts*, persisting the
last-check timestamp + result to `settings.GLOBAL_DATA_DIR /
"update-check.json"` (atomic write, corrupt-tolerant -- same pattern as
`recents.py` / `remote_state.py`). `latest_known_update()` is the read-only
accessor views.py uses to render the banner; it never blocks on network.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from maier import __version__

logger = logging.getLogger("maier.updates")

RELEASES_API = "https://api.github.com/repos/Tyn-Studio/maier/releases/latest"
RELEASES_PAGE = "https://github.com/Tyn-Studio/maier/releases"

_REQUEST_TIMEOUT_SECONDS = 5
_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
_STATE_FILENAME = "update-check.json"

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see core/previews.py / recents.py / remote_state.py for the same
# workaround).
_CHECK_ERRORS = (
    urllib.error.URLError,
    urllib.error.HTTPError,
    TimeoutError,
    OSError,
    ValueError,
    KeyError,
    TypeError,
)
_STATE_LOAD_ERRORS = (json.JSONDecodeError, ValueError, TypeError, KeyError, OSError)


@dataclass
class UpdateInfo:
    version: str
    url: str


_check_lock = threading.Lock()
_cached_result: UpdateInfo | None = None
_cache_populated = False


def _parse_version(raw: str) -> tuple[int, ...] | None:
    """Parse a dotted-int version, tolerating a leading "v" and junk
    (pre-release suffixes, build metadata, garbage) by returning None rather
    than raising. "1.2.3" -> (1, 2, 3); "v1.2.3-beta" -> None (deliberately
    conservative: an unparseable remote tag is treated as "not newer" rather
    than guessed at).
    """
    text = raw.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    if not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def _is_newer(remote: tuple[int, ...], local: tuple[int, ...]) -> bool:
    length = max(len(remote), len(local))
    remote_padded = remote + (0,) * (length - len(remote))
    local_padded = local + (0,) * (length - len(local))
    return remote_padded > local_padded


def _state_path() -> Path:
    from django.conf import settings

    return settings.GLOBAL_DATA_DIR / _STATE_FILENAME


def _fetch_latest_release() -> dict:
    """Network seam: the only function that touches the network. Tests
    monkeypatch `urllib.request.urlopen` directly.
    """
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "maier-update-checker",
        },
    )
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read()
    return json.loads(raw)


def check_for_update() -> UpdateInfo | None:
    """GET the GitHub releases API and compare against `__version__`. Any
    failure whatsoever (network, 404 while the repo is private, rate limit,
    malformed JSON, unparseable tag) returns None -- this must never raise
    and must never surface above debug logging (a private/offline repo is an
    expected, routine outcome, not an error).
    """
    try:
        data = _fetch_latest_release()
        tag_name = data["tag_name"]
        html_url = data["html_url"]
    except _CHECK_ERRORS as exc:
        logger.debug("update check failed (treated as no update available): %r", exc)
        return None

    remote_version = _parse_version(tag_name)
    local_version = _parse_version(__version__)
    if remote_version is None or local_version is None:
        return None

    if not _is_newer(remote_version, local_version):
        return None

    version_str = tag_name.strip()
    if version_str.startswith(("v", "V")):
        version_str = version_str[1:]
    return UpdateInfo(version=version_str, url=html_url)


# --- 24h cache (persisted to GLOBAL_DATA_DIR, in-memory mirror for reads) ---


def _load_state() -> tuple[datetime | None, UpdateInfo | None]:
    try:
        raw = _state_path().read_text()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state file does not contain a JSON object")
        checked_at_raw = data.get("checked_at")
        checked_at = datetime.fromisoformat(checked_at_raw) if checked_at_raw else None
        result_raw = data.get("result")
        result = None
        if result_raw:
            result = UpdateInfo(version=str(result_raw["version"]), url=str(result_raw["url"]))
    except _STATE_LOAD_ERRORS:
        return None, None
    return checked_at, result


def _save_state(checked_at: datetime, result: UpdateInfo | None) -> None:
    path = _state_path()
    state_dir = path.parent
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return  # best-effort cache; never fail the caller over this

    payload = {
        "checked_at": checked_at.isoformat(),
        "result": {"version": result.version, "url": result.url} if result else None,
    }

    try:
        fd, tmp_name = tempfile.mkstemp(dir=state_dir, prefix=f".{path.name}-", suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _check_and_cache(force: bool = False) -> UpdateInfo | None:
    """Consult the persisted state file first (fresh within 24h -> no
    network); otherwise runs `check_for_update` and persists the outcome.
    Holds `_check_lock` for the duration so concurrent callers (e.g. the
    background thread racing a direct test call) don't double-hit the
    network. Updates the in-memory mirror either way.
    """
    global _cached_result, _cache_populated

    with _check_lock:
        now = datetime.now(UTC)
        if not force:
            checked_at, result = _load_state()
            if checked_at is not None:
                age = (now - checked_at).total_seconds()
                if 0 <= age < _CHECK_INTERVAL_SECONDS:
                    _cached_result = result
                    _cache_populated = True
                    return result

        result = check_for_update()
        _save_state(now, result)
        _cached_result = result
        _cache_populated = True
        return result


def latest_known_update() -> UpdateInfo | None:
    """Read-only accessor for views.py: never touches the network, never
    blocks. Returns the in-memory cached result (populated by
    `start_background_check` / a direct `_check_and_cache` call), or None if
    nothing has run yet this process.
    """
    return _cached_result if _cache_populated else None


def start_background_check() -> None:
    """Spawn a daemon thread that loads/refreshes the 24h-cached update
    check. Safe to call on every boot: within the 24h window it reads the
    cached result from disk instead of hitting the network. Never raises --
    the caller (cli.py) must never fail to boot over this.
    """

    def _run() -> None:
        try:
            _check_and_cache()
        except Exception:
            logger.debug("background update check failed", exc_info=True)

    threading.Thread(target=_run, daemon=True).start()


def _reset_cache_for_tests() -> None:
    """Test hook: forces `latest_known_update()` back to "nothing checked
    yet this process" between tests that don't otherwise isolate module
    state.
    """
    global _cached_result, _cache_populated
    _cached_result = None
    _cache_populated = False

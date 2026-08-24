"""Read-only wrapper over pyicloud 2.x (SPEC §18, CLAUDE.md hard rules 8-9).

Public surface is deliberately narrow: login/session reuse, 2FA submission,
metadata listing, and downloads. No method here ever calls a pyicloud method
that deletes, favorites, uploads, or otherwise writes to the account --
`test_icloud.py::test_public_surface_is_read_only` guards this by
introspection.

Passwords are used transiently for `login()` only and never persisted; only
pyicloud's own session/cookie files (which hold session + trust tokens) are
kept, under `settings.GLOBAL_DATA_DIR / "icloud-sessions" / <account-slug>/`.

pyicloud 2.6.5 API notes (verified by inspecting `PyiCloudService.__init__`'s
signature and `src/pyicloud/services/photos_cloudkit/service.py` in the
installed wheel; report these to the lead so they can be sanity-checked):

- `PyiCloudService.__init__(apple_id, password=None, cookie_directory=None,
  ..., authenticate=True, ...)`. The session/cookie store kwarg is
  `cookie_directory` (not `session_directory`).
- Constructing with `authenticate=True` (the default) does NOT raise when
  Apple demands 2FA -- Apple sends a session token alongside the 2FA
  challenge, so the constructor completes normally with
  `service.requires_2fa == True`. Bad credentials DO raise
  `PyiCloudFailedLoginException` from inside the constructor.
- 2FA: `service.requires_2fa` (bool property) and
  `service.validate_2fa_code(code) -> bool`, which internally calls
  `trust_session()` on success -- no separate trust-session step is needed.
- Session reuse: passing `password=""` (an empty string, NOT `None`) skips
  pyicloud's automatic OS-keyring password lookup (`get_password_from_keyring`
  only fires when password is exactly `None`) while still letting pyicloud
  validate/refresh the session token persisted in `cookie_directory`. An
  invalid/expired/absent session raises a `PyiCloudException` subclass from
  the constructor in this codepath.
- Photos: `service.photos.all` is an iterable `PhotoAlbum` (not a plain
  list); each yielded `PhotoAsset` has `.id`, `.filename`,
  `.created` (already an aware-UTC `datetime`), `.size` (`int | None`),
  `.item_type` (`"image"` or `"movie"` -- NOT `"video"`), `.versions`
  (`dict[str, dict]`), and `.download(version) -> bytes | None`. Direct
  id lookup without a prior full iteration: `service.photos.all.get(id)`.
- Version keys: both of `PhotoAsset`'s internal lookup tables (still
  photos vs. movies) already use the literal keys `"thumb"`, `"medium"`,
  `"original"` -- no size-name remapping is needed, just a fallback to
  `"original"` when the requested version isn't present on a given asset.
- `PhotoAsset.download()` returns the *entire* rendition as `bytes`
  (pyicloud reads the streamed HTTP response into memory itself --
  `response.raw.read()`), not a chunked-iterable response object. This is a
  deviation from the brief's assumption of `iter_content()`-based streaming:
  see `download()` below.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudException, PyiCloudFailedLoginException

# Canonical slug implementation lives in remote_state (dependency-light);
# re-exported here so callers can import it from either module.
from .remote_state import account_slug

logger = logging.getLogger("maier.icloud")

# Named tuple (not an inline `except (A, B):` literal) to sidestep a known
# ruff 0.16.4 formatter bug that mangles inline except-tuples onto one line
# into invalid syntax -- see exiftool.py/previews.py for the same workaround.
_PYICLOUD_ERRORS = (PyiCloudException, OSError)

_VERSION_NAMES = ("thumb", "medium", "original")
_CHUNK_SIZE = 1024 * 1024


class TwoFactorRequired(Exception):
    """Raised by `ICloudClient.login` when Apple demands a 2FA code.

    Carries the pending, already-constructed client on `.client` so the
    caller submits the code on the SAME session (`client.submit_2fa(code)`)
    rather than starting a fresh login.
    """

    def __init__(self, client: ICloudClient) -> None:
        super().__init__(f"Two-factor authentication required for {client.account}")
        self.client = client


class ICloudError(Exception):
    """Wraps every pyicloud/network/session-store failure. Callers never see
    pyicloud's own exception types.
    """


@dataclass
class RemoteAsset:
    remote_id: str
    filename: str
    captured_at: datetime  # aware UTC
    size: int
    media_type: str  # "image" | "video"


def _session_dir(email: str) -> Path:
    return settings.GLOBAL_DATA_DIR / "icloud-sessions" / account_slug(email)


def _media_type(item_type: str) -> str:
    return "video" if item_type == "movie" else "image"


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _to_remote_asset(asset: object) -> RemoteAsset:
    size = getattr(asset, "size", None) or 0
    item_type = getattr(asset, "item_type", "image")
    return RemoteAsset(
        remote_id=asset.id,
        filename=asset.filename,
        captured_at=_aware_utc(asset.created),
        size=size,
        media_type=_media_type(item_type),
    )


class ICloudClient:
    """Thin, read-only wrapper over one authenticated `pyicloud.PyiCloudService`."""

    def __init__(self, account: str, service: object) -> None:
        self.account = account
        self._service = service
        # Populated as list_assets() iterates; download() consults it first
        # and falls back to a direct `photos.all.get(id)` lookup on a miss
        # (e.g. download() called in a fresh process/client without a prior
        # list_assets() pass in this instance).
        self._asset_cache: dict[str, object] = {}

    @classmethod
    def login(cls, email: str, password: str) -> ICloudClient:
        session_dir = _session_dir(email)
        try:
            session_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ICloudError(f"Could not create iCloud session directory: {exc}") from exc

        try:
            service = PyiCloudService(email, password, cookie_directory=str(session_dir))
        except PyiCloudFailedLoginException as exc:
            raise ICloudError(f"Invalid Apple ID or password for {email}") from exc
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"iCloud login failed for {email}: {exc}") from exc

        client = cls(email, service)
        if service.requires_2fa:
            raise TwoFactorRequired(client)
        return client

    @classmethod
    def from_session(cls, email: str) -> ICloudClient | None:
        session_dir = _session_dir(email)
        if not session_dir.exists():
            return None

        try:
            # password="" (NOT None) skips pyicloud's OS-keyring lookup while
            # still letting it validate the persisted session token -- see
            # the module docstring's "Session reuse" note.
            service = PyiCloudService(email, password="", cookie_directory=str(session_dir))
        except _PYICLOUD_ERRORS:
            logger.info("iCloud session for %s is missing or invalid", email)
            return None

        if service.requires_2fa:
            logger.info("iCloud session for %s needs re-authentication (2FA)", email)
            return None

        return cls(email, service)

    def submit_2fa(self, code: str) -> bool:
        try:
            return bool(self._service.validate_2fa_code(code))
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"2FA verification failed: {exc}") from exc

    def list_assets(self, since: datetime | None) -> Iterator[RemoteAsset]:
        """Enumerate metadata for every photo/video in the account's main library.

        `since` filters client-side only: pyicloud's Photos API offers no
        reliable server-side date filter, so a pull still enumerates
        metadata for the whole library every time. "Incremental" per SPEC
        §18 means skipping re-DOWNLOADS of unchanged items, not skipping
        metadata enumeration -- callers (T16 `pull.py`) upsert by
        `remote_id`, so re-seeing older items here is cheap and idempotent.
        """
        since_utc = _aware_utc(since) if since is not None else None
        try:
            for asset in self._service.photos.all:
                self._asset_cache[asset.id] = asset
                remote_asset = _to_remote_asset(asset)
                if since_utc is not None and remote_asset.captured_at <= since_utc:
                    continue
                yield remote_asset
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Listing iCloud assets failed: {exc}") from exc

    def _find_asset(self, remote_id: str) -> object:
        cached = self._asset_cache.get(remote_id)
        if cached is not None:
            return cached
        try:
            asset = self._service.photos.all.get(remote_id)
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Could not look up iCloud asset {remote_id}: {exc}") from exc
        if asset is None:
            raise ICloudError(f"iCloud asset not found: {remote_id}")
        self._asset_cache[remote_id] = asset
        return asset

    def download(self, remote_id: str, version: str, dest: Path) -> None:
        """Download a read-only rendition of one asset to `dest`.

        Writes to a sibling `<name>.part` file, then `os.replace`s it into
        place: `dest` either ends up complete, or (on any failure) is left
        untouched with no partial file lingering next to it.
        """
        if version not in _VERSION_NAMES:
            raise ICloudError(f"Unknown iCloud asset version: {version!r}")

        asset = self._find_asset(remote_id)

        try:
            available = asset.versions
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Could not read versions for {remote_id}: {exc}") from exc
        use_version = version if version in available else "original"

        try:
            data = asset.download(use_version)
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Downloading iCloud asset {remote_id} failed: {exc}") from exc
        if data is None:
            raise ICloudError(f"iCloud asset {remote_id} has no '{use_version}' rendition")

        tmp_path = dest.parent / f"{dest.name}.part"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("wb") as f:
                # `data` is already fully in memory (see module docstring);
                # writing it in chunks here just bounds the copy loop, it
                # does not reduce pyicloud's own peak memory usage.
                for offset in range(0, len(data), _CHUNK_SIZE):
                    f.write(data[offset : offset + _CHUNK_SIZE])
            os.replace(tmp_path, dest)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            raise ICloudError(f"Writing downloaded iCloud asset {remote_id} failed: {exc}") from exc

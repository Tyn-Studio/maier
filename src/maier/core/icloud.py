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

Live Photos (T20, investigated in
`src/pyicloud/services/photos_cloudkit/service.py` of the installed 2.6.5
wheel):

- `PhotoAsset.is_live_photo` (service.py:1792-1798) is a bool property that
  reads `resOriginalVidComplFileType` directly off the already-fetched
  `CPLMaster` record (`self._master_record`, populated during the SAME
  listing query that yields the asset -- see `_process_asset_page` around
  service.py:1039-1067, which builds each `PhotoAsset` from a master+asset
  record pair already in hand). Reading `is_live_photo` therefore costs no
  extra network round trip, at listing time or later.
- `PhotoAsset.PHOTO_VERSION_LOOKUP` (service.py:1672-1681) maps the version
  key `"original_video"` to CloudKit field prefix `"resOriginalVidCompl"`
  (i.e. field `resOriginalVidComplRes`). `PhotoAsset.resources`
  (service.py:1801-1821) builds a `PhotoResource` for every key in
  `PHOTO_VERSION_LOOKUP` whose backing CloudKit field is present on the
  master record -- for a non-Live-Photo image, `resOriginalVidComplRes` is
  simply absent, so `"original_video"` never appears in `.versions`/
  `.resources` at all (mirrors `is_live_photo`, which gates on the sibling
  `...FileType` field). `mappers.build_photo_resource` (mappers.py:121-171)
  also renames the resource to `<stem>.MOV` automatically.
- `PhotoAsset.download()` (service.py:1831-1840) is version-agnostic -- it
  just resolves `download_url(version)` via `.resources.get(version)` and
  fetches it, so `"original_video"` downloads through the exact same code
  path as `"thumb"`/`"medium"`/`"original"`. `download_live_video()` below
  therefore reuses this class's own `download()` method rather than
  duplicating the atomic-write logic.
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
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

# pyicloud sets NO request timeouts anywhere; when Apple throttles a query it
# TARPITS it (holds the connection open without responding), which left our
# preview workers blocked in ssl.read forever (stack-dumped live,
# 2026-08-24: frozen at "previews 0 / 2788"). requests passes timeout=None
# to urllib3, whose sockets then honor this process-wide default -- a
# blunt but complete safety net for every pyicloud call.
socket.setdefaulttimeout(60)

logger = logging.getLogger("maier.icloud")

# Named tuple (not an inline `except (A, B):` literal) to sidestep a known
# ruff 0.16.4 formatter bug that mangles inline except-tuples onto one line
# into invalid syntax -- see exiftool.py/previews.py for the same workaround.
_PYICLOUD_ERRORS = (PyiCloudException, OSError)

# thumb/medium/original exist on every asset (for videos, medium/thumb are
# MP4 renditions). Video assets additionally expose JPEG poster frames as
# "medium_image"/"thumb_image" (verified against a real library,
# 2026-08-24) -- callers fetching an *image preview of a video* must use
# those; falling back to "original" would hand back video bytes.
# "original_video" (T20) is a Live Photo's paired video component -- only
# present on `.versions` for assets where `is_live_photo` is True (see
# module docstring); `download_live_video()` is the only caller.
_VERSION_NAMES = (
    "thumb",
    "medium",
    "original",
    "thumb_image",
    "medium_image",
    "original_video",
)
_IMAGE_ONLY_FALLBACKS = {
    "medium_image": ("medium_image", "thumb_image"),
    "thumb_image": ("thumb_image", "medium_image"),
}
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
    # T20: cheap at listing time (see module docstring -- `is_live_photo`
    # reads a field already present on the master record fetched during
    # listing, no extra request). Defaults False so existing positional
    # `RemoteAsset(...)` call sites/tests are unaffected.
    is_live: bool = False


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
        is_live=bool(getattr(asset, "is_live_photo", False)),
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

    @classmethod
    def forget_session(cls, email: str) -> None:
        """Delete this account's local session-token store (PLAN M5 T21,
        "Disconnect account"). Lives on this read-only-guarded class (see
        `test_public_surface_is_read_only`) but is NOT an iCloud write --
        despite the name, it never calls into pyicloud or Apple's API at
        all; it only removes files under OUR OWN
        `GLOBAL_DATA_DIR/icloud-sessions/<slug>/` (the cookie directory
        `login`/`from_session` create and read), so a subsequent
        `from_session` call for this account correctly returns `None` until
        the user re-authenticates. Idempotent: an absent/already-removed
        session dir is a no-op.
        """
        shutil.rmtree(_session_dir(email), ignore_errors=True)

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
        album = self._service.photos.all
        try:
            # Empirically INVERTED naming (probed live against a real
            # library, 2026-08-24): DirectionEnum.ASCENDING yields
            # NEWEST-first; pyicloud's ALL_PHOTOS default (DESCENDING)
            # yields oldest-first. Newest-first matters: the user's working
            # range is almost always recent, so enumerating newest-first
            # caches those assets -- and unblocks their preview fetches --
            # in the first minute instead of the last.
            from pyicloud.services.photos_cloudkit.constants import DirectionEnum

            album._direction = DirectionEnum.ASCENDING
        except Exception:
            pass  # older/newer pyicloud internals: keep the default order
        try:
            for asset in album:
                self._asset_cache[asset.id] = asset
                remote_asset = _to_remote_asset(asset)
                if since_utc is not None and remote_asset.captured_at <= since_utc:
                    continue
                yield remote_asset
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Listing iCloud assets failed: {exc}") from exc

    def has_asset_cached(self, remote_id: str) -> bool:
        """True when `download(remote_id, ...)` can run without an album
        lookup. `photos.all.get(id)` walks the album's shared pagination --
        calling it from preview workers WHILE `list_assets` is enumerating
        the same album starves the workers behind the enumerator (observed
        live: "previews 0 / 2788" for minutes, 2026-08-24). Callers doing
        bulk fetches during an active enumeration should defer cache-miss
        items until the enumeration has cached them (pull.py does).
        """
        return remote_id in self._asset_cache

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
        if version in _IMAGE_ONLY_FALLBACKS:
            # An image rendition of a video must never fall back to
            # "original" (video bytes where the caller expects a JPEG).
            use_version = next((v for v in _IMAGE_ONLY_FALLBACKS[version] if v in available), None)
            if use_version is None:
                raise ICloudError(f"iCloud asset {remote_id} has no image rendition")
        else:
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

    def download_live_video(self, remote_id: str, dest: Path) -> bool:
        """Fetch a Live Photo's paired video component to `dest` (T20, SPEC
        §18). Returns False -- nothing written -- when the asset isn't a
        Live Photo (or, defensively, when `is_live_photo` is True but the
        "original_video" resource still isn't present -- see module
        docstring, this shouldn't normally happen since both gate on the
        same underlying field). Raises `ICloudError` when the asset IS a
        Live Photo but the video component can't be fetched/written -- same
        atomic `.part` + `os.replace` semantics as `download()`, which this
        reuses directly (see module docstring: `"original_video"` downloads
        through the identical pyicloud code path as every other version).
        """
        asset = self._find_asset(remote_id)
        if not getattr(asset, "is_live_photo", False):
            return False

        try:
            available = asset.versions
        except _PYICLOUD_ERRORS as exc:
            raise ICloudError(f"Could not read versions for {remote_id}: {exc}") from exc
        if "original_video" not in available:
            return False

        self.download(remote_id, "original_video", dest)
        return True

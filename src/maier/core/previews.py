"""Preview generation: 2048px-long-edge JPEGs cached under
`.maier/previews/`, keyed by content so they survive moves. See SPEC.md
§6 Phase B item 1 and §10 (review-screen perf).
"""

import hashlib
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from . import exiftool as exiftool_module
from .models import Photo, absolute_path_for
from .remote_state import _slug as _account_slug

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pillow-heif optional at runtime; HEIC just won't decode
    pass

MAX_DIMENSION = 2048
JPEG_QUALITY = 82

RAW_EXTENSIONS = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2"}
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".avi"}

_PLACEHOLDER_NAME = "_placeholder.jpg"
_PLACEHOLDER_SIZE = (2048, 1365)
_PLACEHOLDER_COLOR = (51, 51, 51)

_CONTENT_KEY_CHUNK = 65536  # first 64KiB

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line.
_PREVIEW_GENERATION_ERRORS = (OSError, UnidentifiedImageError, ValueError)
# T28 (flagged): `_preview_key`/`preview_path` can now raise ValueError (via
# `absolute_path_for`) for an unresolvable `@src/...` row, in addition to the
# pre-existing OSError from a bad stat -- both fall back to the placeholder.
_PREVIEW_KEY_ERRORS = (OSError, ValueError)


def _previews_dir(folder: Path) -> Path:
    d = folder / ".maier" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _content_key(src: Path) -> str:
    """Cheap content-derived key: sha1 of the first 64KiB + file size.

    Fast (no full-file read) and stable across moves; used only when
    `sha256` hasn't been computed yet (Phase B not run).
    """
    size = src.stat().st_size
    h = hashlib.sha1()
    with src.open("rb") as f:
        h.update(f.read(_CONTENT_KEY_CHUNK))
    h.update(str(size).encode())
    return h.hexdigest()


def _preview_key(folder: Path, photo: Photo) -> str:
    if photo.sha256:
        return photo.sha256
    # T28 (flagged, minimal edit): was `folder / photo.relative_path`, which
    # builds a bogus path for `@src/...` source rows -- route through
    # `absolute_path_for` so it resolves against the photo's actual source
    # root instead. May raise ValueError for an unresolvable row; caller
    # catches it via `_PREVIEW_KEY_ERRORS`.
    src = absolute_path_for(photo, folder)
    return _content_key(src)


def _is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_EXTENSIONS


def _is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _placeholder_path(folder: Path) -> Path:
    dest = _previews_dir(folder) / _PLACEHOLDER_NAME
    if not dest.exists():
        img = Image.new("RGB", _PLACEHOLDER_SIZE, _PLACEHOLDER_COLOR)
        img.save(dest, "JPEG", quality=JPEG_QUALITY)
    return dest


def _generate_image_preview(src: Path, dest: Path) -> None:
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        img.save(dest, "JPEG", quality=JPEG_QUALITY)


def remote_preview_dest(folder: Path, account: str, remote_id: str) -> Path:
    """Cache path for a remote (iCloud) photo's bulk-synced "thumb" tier
    (~60KB, plenty for grid cells -- SPEC §18: "Thumbnails/medium previews
    cache under `.maier/previews/` keyed by `remote_id`"). Despite the name,
    this is the THUMB tier as of PLAN T22 (2026-08-24): `pull.py` fetches
    "thumb"/"thumb_image" into this path for every remote row so the grid is
    fully browsable without waiting on the much larger "medium" tier. The
    grid keeps using this path forever -- `preview_path` below is unchanged.
    Sharper on-demand quality for the review screen lives at
    `remote_medium_dest` instead (PLAN T22, `core/preview_upgrade.py`).
    Shared with `core/pull.py`, which writes the file at this exact path --
    keep the two in sync.
    """
    return _previews_dir(folder) / f"icloud-{_account_slug(account)}-{remote_id}.jpg"


def remote_medium_dest(folder: Path, account: str, remote_id: str) -> Path:
    """Cache path for a remote (iCloud) photo's on-demand "medium" preview
    (PLAN T22, CTO-approved 2026-08-24 design: thumb-first instant render,
    background medium swap-in, neighbour prefetch). Fetched at most once per
    photo, ever, only when it's actually opened in the review screen (~1MB
    at review-quality vs. the bulk thumb's ~60KB) -- cached forever
    afterwards, same as every other preview tier. Shared with
    `core/preview_upgrade.py`, which writes the file at this exact path.
    """
    return _previews_dir(folder) / f"icloud-{_account_slug(account)}-{remote_id}-medium.jpg"


def best_remote_preview(folder: Path, photo: Photo) -> Path:
    """Best cached preview for a remote row, without ever hitting the
    network from the request path (PLAN T22, mirrors `preview_path`'s own
    "never raises, never fetches" contract): the sharp on-demand "medium" if
    it has landed, else the bulk-synced "thumb", else the shared
    placeholder. Callers upgrade a photo to medium in the background via
    `core.preview_upgrade.enqueue_medium` -- this function only reads
    whatever is already on disk.
    """
    medium = remote_medium_dest(folder, photo.account, photo.remote_id or "")
    if medium.exists():
        return medium
    thumb = remote_preview_dest(folder, photo.account, photo.remote_id or "")
    if thumb.exists():
        return thumb
    return _placeholder_path(folder)


def preview_path(folder: Path, photo: Photo) -> Path:
    """Return the cached preview path, generating it first if absent.

    Never raises: any failure (missing source, unreadable/corrupt file,
    exiftool absent/failing on RAW, video extension) falls back to a shared
    placeholder image.

    Remote (iCloud) rows have no local file (SPEC §18): the medium preview
    is prefetched by `core/pull.py` ahead of time, so this never hits the
    network from the request path -- if it isn't cached yet, callers get
    the placeholder until the next pull completes.
    """
    if photo.source == Photo.SOURCE_ICLOUD:
        dest = remote_preview_dest(folder, photo.account, photo.remote_id or "")
        return dest if dest.exists() else _placeholder_path(folder)

    # T28 (flagged, minimal edit): was `folder / photo.relative_path`, which
    # builds a bogus path for `@src/...` source rows -- route through
    # `absolute_path_for` so this resolves against the photo's actual source
    # root instead of the library folder.
    try:
        src = absolute_path_for(photo, folder)
    except ValueError:
        return _placeholder_path(folder)

    if _is_video(src):
        return _placeholder_path(folder)

    if _is_raw(src):
        return _raw_preview_path(folder, photo, src)

    try:
        key = _preview_key(folder, photo)
    except _PREVIEW_KEY_ERRORS:
        return _placeholder_path(folder)

    dest = _previews_dir(folder) / f"{key}.jpg"
    if dest.exists():
        return dest

    try:
        _generate_image_preview(src, dest)
    except _PREVIEW_GENERATION_ERRORS:
        dest.unlink(missing_ok=True)
        return _placeholder_path(folder)

    return dest


def _raw_preview_path(folder: Path, photo: Photo, src: Path) -> Path:
    """RAW embedded-preview extraction (SPEC §6 Phase B item 1): extract via
    exiftool into a temp file, then run it through the normal resize/orient
    pipeline into the content-keyed cache path. Falls back to the shared
    placeholder whenever exiftool is absent or extraction/decoding fails.
    """
    exiftool_path = exiftool_module.find_exiftool()
    if exiftool_path is None:
        return _placeholder_path(folder)

    try:
        key = _preview_key(folder, photo)
    except _PREVIEW_KEY_ERRORS:
        return _placeholder_path(folder)

    dest = _previews_dir(folder) / f"{key}.jpg"
    if dest.exists():
        return dest

    with tempfile.TemporaryDirectory() as tmp_dir:
        extracted = Path(tmp_dir) / "extracted.jpg"
        if not exiftool_module.extract_embedded_preview(exiftool_path, src, extracted):
            return _placeholder_path(folder)

        try:
            _generate_image_preview(extracted, dest)
        except _PREVIEW_GENERATION_ERRORS:
            dest.unlink(missing_ok=True)
            return _placeholder_path(folder)

    return dest

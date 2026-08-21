"""Preview generation: 2048px-long-edge JPEGs cached under
`.culler/previews/`, keyed by content so they survive moves. See SPEC.md
§6 Phase B item 1 and §10 (review-screen perf).
"""

import hashlib
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import Photo

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


def _previews_dir(folder: Path) -> Path:
    d = folder / ".culler" / "previews"
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
    src = folder / photo.relative_path
    return _content_key(src)


def _is_raw_or_video(path: Path) -> bool:
    ext = path.suffix.lower()
    return ext in RAW_EXTENSIONS or ext in VIDEO_EXTENSIONS


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


def preview_path(folder: Path, photo: Photo) -> Path:
    """Return the cached preview path, generating it first if absent.

    Never raises: any failure (missing source, unreadable/corrupt file,
    RAW/video extension) falls back to a shared placeholder image.
    """
    src = folder / photo.relative_path

    if _is_raw_or_video(src):
        return _placeholder_path(folder)

    try:
        key = _preview_key(folder, photo)
    except OSError:
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

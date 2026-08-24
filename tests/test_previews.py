import io
import stat
from datetime import UTC, datetime

import pytest
from django.conf import settings
from django.urls import reverse
from PIL import Image

from maier.core import exiftool as exiftool_module
from maier.core import previews
from maier.core.models import Photo, Source, sentinel_for_source
from maier.core.previews import _content_key, _preview_key, preview_path, remote_preview_dest

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_exiftool_cache():
    exiftool_module._reset_cache()
    yield
    exiftool_module._reset_cache()


def _photo(relative_path: str, sha256: str | None = None) -> Photo:
    return Photo(relative_path=relative_path, sha256=sha256)


def _db_photo(relative_path: str, **overrides) -> Photo:
    kwargs = dict(
        relative_path=relative_path,
        status=Photo.STATUS_OPTIONAL,
        provenance="",
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    kwargs.update(overrides)
    return Photo.objects.create(**kwargs)


def _save_jpeg(path, size=(3000, 2000), mode="RGB", exif=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new(mode, size, color=(200, 50, 50))
    kwargs = {}
    if exif is not None:
        kwargs["exif"] = exif
    img.save(path, "JPEG", **kwargs)


def _save_png(path, size=(100, 100), mode="RGBA"):
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new(mode, size, color=(10, 20, 30, 128))
    img.save(path, "PNG")


# --- key derivation ---------------------------------------------------


def test_preview_key_uses_sha256_when_set(tmp_path):
    photo = _photo("a.jpg", sha256="deadbeef" * 8)
    assert _preview_key(tmp_path, photo) == "deadbeef" * 8


def test_preview_key_falls_back_to_content_key(tmp_path):
    src = tmp_path / "a.jpg"
    _save_jpeg(src, size=(100, 80))
    photo = _photo("a.jpg", sha256=None)
    assert _preview_key(tmp_path, photo) == _content_key(src)


# --- generation ---------------------------------------------------------


def test_preview_generated_at_expected_path(tmp_path):
    src = tmp_path / "photo.jpg"
    _save_jpeg(src, size=(3000, 2000))
    photo = _photo("photo.jpg", sha256=None)

    result = preview_path(tmp_path, photo)

    expected = tmp_path / ".maier" / "previews" / f"{_content_key(src)}.jpg"
    assert result == expected
    assert result.exists()
    with Image.open(result) as img:
        assert img.format == "JPEG"
        assert max(img.size) <= previews.MAX_DIMENSION


def test_preview_keyed_by_sha256_when_present(tmp_path):
    src = tmp_path / "photo.jpg"
    _save_jpeg(src, size=(500, 400))
    sha = "abc123" * 10 + "abcd"
    photo = _photo("photo.jpg", sha256=sha)

    result = preview_path(tmp_path, photo)

    assert result == tmp_path / ".maier" / "previews" / f"{sha}.jpg"
    assert result.exists()


def test_second_call_does_not_regenerate(tmp_path, monkeypatch):
    src = tmp_path / "photo.jpg"
    _save_jpeg(src, size=(500, 400))
    photo = _photo("photo.jpg", sha256=None)

    calls = []
    original = previews._generate_image_preview

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(previews, "_generate_image_preview", counting)

    first = preview_path(tmp_path, photo)
    second = preview_path(tmp_path, photo)

    assert first == second
    assert len(calls) == 1


def test_no_upscale_of_small_image(tmp_path):
    src = tmp_path / "small.jpg"
    _save_jpeg(src, size=(50, 30))
    photo = _photo("small.jpg", sha256=None)

    result = preview_path(tmp_path, photo)

    with Image.open(result) as img:
        assert img.size == (50, 30)


def test_orientation_applied(tmp_path):
    src = tmp_path / "rotated.jpg"
    # store landscape pixel data (200x100) but flag orientation=6
    # (rotate 270 CW / display swaps width & height).
    img = Image.new("RGB", (200, 100), color=(1, 2, 3))
    exif = img.getexif()
    exif[274] = 6  # Orientation tag
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(buf.getvalue())

    photo = _photo("rotated.jpg", sha256=None)
    result = preview_path(tmp_path, photo)

    with Image.open(result) as out:
        assert out.size == (100, 200)


def test_rgba_png_converts_to_rgb(tmp_path):
    src = tmp_path / "alpha.png"
    _save_png(src, size=(300, 200), mode="RGBA")
    photo = _photo("alpha.png", sha256=None)

    result = preview_path(tmp_path, photo)

    with Image.open(result) as img:
        assert img.mode == "RGB"
        assert img.format == "JPEG"


# --- placeholder fallbacks ------------------------------------------------


def test_raw_extension_returns_placeholder(tmp_path):
    src = tmp_path / "photo.cr2"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not a real raw file")
    photo = _photo("photo.cr2", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"
    with Image.open(result) as img:
        assert img.size[0] == 2048


def test_raw_extension_placeholder_when_exiftool_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(exiftool_module, "find_exiftool", lambda: None)
    src = tmp_path / "photo.cr2"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not a real raw file")
    photo = _photo("photo.cr2", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"


def _make_fake_exiftool(tmp_path, jpeg_source):
    script = tmp_path / "fake_exiftool.sh"
    script.write_text(f"#!/bin/sh\ncat '{jpeg_source}'\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_raw_extension_generates_real_preview_via_fake_exiftool(tmp_path, monkeypatch):
    jpeg_source = tmp_path / "embedded.jpg"
    _save_jpeg(jpeg_source, size=(3000, 2000))
    script = _make_fake_exiftool(tmp_path, jpeg_source)
    monkeypatch.setattr(exiftool_module, "find_exiftool", lambda: script)

    raw_src = tmp_path / "photo.cr2"
    raw_src.write_bytes(b"not a real raw file")
    photo = _photo("photo.cr2", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name != "_placeholder.jpg"
    assert result == tmp_path / ".maier" / "previews" / f"{_content_key(raw_src)}.jpg"
    with Image.open(result) as img:
        assert img.format == "JPEG"
        assert max(img.size) <= previews.MAX_DIMENSION


def test_raw_extension_falls_back_when_extraction_fails(tmp_path, monkeypatch):
    script = tmp_path / "fake_exiftool.sh"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setattr(exiftool_module, "find_exiftool", lambda: script)

    raw_src = tmp_path / "photo.cr2"
    raw_src.write_bytes(b"not a real raw file")
    photo = _photo("photo.cr2", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"


def test_video_extension_returns_placeholder(tmp_path):
    src = tmp_path / "clip.mov"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"not a real video")
    photo = _photo("clip.mov", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"


def test_missing_file_returns_placeholder(tmp_path):
    photo = _photo("does-not-exist.jpg", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"
    assert result.exists()


def test_missing_file_with_sha256_returns_placeholder(tmp_path):
    photo = _photo("does-not-exist.jpg", sha256="ff" * 32)

    result = preview_path(tmp_path, photo)

    # key resolves from sha256 (no stat needed) but source open fails.
    assert result.name == "_placeholder.jpg"


def test_corrupt_file_returns_placeholder(tmp_path):
    src = tmp_path / "corrupt.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"this is definitely not a jpeg" * 10)
    photo = _photo("corrupt.jpg", sha256=None)

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"
    # no stray partial file left behind under the content key
    key = _content_key(src)
    assert not (tmp_path / ".maier" / "previews" / f"{key}.jpg").exists()


def test_placeholder_generated_once(tmp_path):
    src1 = tmp_path / "a.cr2"
    src2 = tmp_path / "b.cr2"
    for s in (src1, src2):
        s.write_bytes(b"raw")

    p1 = preview_path(tmp_path, _photo("a.cr2", sha256=None))
    mtime1 = p1.stat().st_mtime
    p2 = preview_path(tmp_path, _photo("b.cr2", sha256=None))
    mtime2 = p2.stat().st_mtime

    assert p1 == p2
    assert mtime1 == mtime2


# --- view -----------------------------------------------------------------


@pytest.mark.django_db
def test_preview_view_returns_jpeg_with_cache_headers(client):
    src = settings.WORKING_FOLDER / "view-photo.jpg"
    _save_jpeg(src, size=(400, 300))
    photo = _db_photo("view-photo.jpg")

    response = client.get(reverse("preview", args=[photo.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert response["Cache-Control"] == "public, max-age=31536000, immutable"
    content = b"".join(response.streaming_content)
    assert content[:2] == b"\xff\xd8"  # JPEG magic bytes


@pytest.mark.django_db
def test_preview_view_placeholder_is_never_cached(client):
    # A remote photo with no fetched preview serves the shared placeholder,
    # which MUST NOT carry immutable caching -- the browser would pin gray
    # squares forever even after the real preview lands (live finding,
    # 2026-08-24).
    photo = Photo.objects.create(
        source=Photo.SOURCE_ICLOUD,
        account="cachetest@example.com",
        remote_id="cache-r1",
        relative_path="@icloud/cachetest@example.com/cache-r1",
        status=Photo.STATUS_OPTIONAL,
        provenance="cachetest-example-com",
        file_size=1,
        file_mtime=0.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )

    response = client.get(reverse("preview", args=[photo.pk]))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_preview_view_404_for_unknown_pk(client):
    response = client.get(reverse("preview", args=[999999]))
    assert response.status_code == 404


# --- remote (iCloud) rows (SPEC §18, PLAN T16) ------------------------------


def _remote_photo(remote_id: str, account: str = "luis@example.com") -> Photo:
    return Photo(
        source=Photo.SOURCE_ICLOUD,
        account=account,
        remote_id=remote_id,
        relative_path=f"@icloud/{account}/{remote_id}",
    )


def test_remote_preview_returns_cached_file_when_present(tmp_path):
    photo = _remote_photo("r1")
    dest = remote_preview_dest(tmp_path, "luis@example.com", "r1")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"cached preview bytes")

    result = preview_path(tmp_path, photo)

    assert result == dest
    assert result.read_bytes() == b"cached preview bytes"


def test_remote_preview_returns_placeholder_when_not_yet_cached(tmp_path):
    photo = _remote_photo("r2")

    result = preview_path(tmp_path, photo)

    assert result.name == "_placeholder.jpg"
    assert result.exists()


def test_remote_preview_never_hits_network_when_uncached(tmp_path, monkeypatch):
    # No download hook exists on Photo/preview_path -- this asserts the
    # request-path code never tries to reach out for one. Since there is no
    # client argument at all to `preview_path`, the only way it *could*
    # fetch would be importing core.pull/core.icloud at call time; assert
    # neither module gets imported as a side effect of this call.
    import sys

    for mod in ("maier.core.pull", "maier.core.icloud"):
        sys.modules.pop(mod, None)

    photo = _remote_photo("r3")
    preview_path(tmp_path, photo)

    assert "maier.core.pull" not in sys.modules
    assert "maier.core.icloud" not in sys.modules


def test_remote_preview_dest_path_matches_pull_naming_scheme(tmp_path):
    dest = remote_preview_dest(tmp_path, "Luis@Example.com", "abc123")
    assert dest == tmp_path / ".maier" / "previews" / "icloud-luis-example-com-abc123.jpg"


# --- registered sources (SPEC §19, T28 -- M6 first wave) --------------------


@pytest.mark.django_db
def test_preview_generated_for_source_photo(tmp_path):
    # T28 flagged fix: preview_path/_preview_key used `folder /
    # photo.relative_path`, bogus for an `@src/...` sentinel row -- this
    # exercises the `absolute_path_for` routing (real file, real source root).
    library = tmp_path / "library"
    library.mkdir()
    source_dir = tmp_path / "external"
    source_dir.mkdir()
    source = Source.objects.create(kind=Source.KIND_LOCAL, name="external", path=str(source_dir))

    src = source_dir / "img.jpg"
    _save_jpeg(src, size=(3000, 2000))

    photo = Photo(
        relative_path=sentinel_for_source(source, "img.jpg"),
        sha256=None,
        source_ref=source,
    )

    result = preview_path(library, photo)

    assert result == library / ".maier" / "previews" / f"{_content_key(src)}.jpg"
    assert result.exists()
    with Image.open(result) as img:
        assert img.format == "JPEG"


def test_preview_path_source_row_missing_source_ref_falls_back_to_placeholder(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    photo = Photo(relative_path="@src/999/img.jpg", sha256=None, source_ref=None)

    result = preview_path(library, photo)

    assert result == previews._placeholder_path(library)

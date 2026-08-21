import stat
from pathlib import Path

import pytest
from django.conf import settings

from culler.core import exiftool


@pytest.fixture(autouse=True)
def _reset_exiftool_cache():
    exiftool._reset_cache()
    yield
    exiftool._reset_cache()


def _make_executable(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


# --- find_exiftool ----------------------------------------------------


def test_find_exiftool_returns_none_when_absent(monkeypatch):
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: None)
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", Path("/does/not/exist/culler-test"))

    assert exiftool.find_exiftool() is None


def test_find_exiftool_prefers_path(monkeypatch):
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: "/usr/bin/exiftool")

    assert exiftool.find_exiftool() == Path("/usr/bin/exiftool")


def test_find_exiftool_falls_back_to_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: None)
    fake = tmp_path / "exiftool"
    _make_executable(fake, "#!/bin/sh\necho hi\n")
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", tmp_path)

    assert exiftool.find_exiftool() == fake


def test_find_exiftool_caches_result(monkeypatch, tmp_path):
    calls = []

    def _which(name):
        calls.append(name)
        return "/usr/bin/exiftool"

    monkeypatch.setattr(exiftool.shutil, "which", _which)

    assert exiftool.find_exiftool() == Path("/usr/bin/exiftool")
    assert exiftool.find_exiftool() == Path("/usr/bin/exiftool")
    assert len(calls) == 1


# --- extract_embedded_preview ------------------------------------------

_FAKE_JPEG_BYTES = b"\xff\xd8\xff\xe0fake jpeg bytes\xff\xd9"


def test_extract_embedded_preview_writes_dest_bytes(tmp_path):
    script = tmp_path / "fake_exiftool.sh"
    jpeg_source = tmp_path / "source.jpg"
    jpeg_source.write_bytes(_FAKE_JPEG_BYTES)
    _make_executable(
        script,
        f"#!/bin/sh\ncat '{jpeg_source}'\n",
    )
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(script, tmp_path / "raw.cr2", dest)

    assert ok is True
    assert dest.read_bytes() == _FAKE_JPEG_BYTES


def test_extract_embedded_preview_nonzero_exit_returns_false(tmp_path):
    script = tmp_path / "fake_exiftool.sh"
    _make_executable(script, "#!/bin/sh\nexit 1\n")
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(script, tmp_path / "raw.cr2", dest)

    assert ok is False
    assert not dest.exists()


def test_extract_embedded_preview_retries_with_preview_image(tmp_path):
    """Branches on the flag argument: empty stdout for -JpgFromRaw, real
    bytes for -PreviewImage -- exercises the retry path.
    """
    jpeg_source = tmp_path / "source.jpg"
    jpeg_source.write_bytes(_FAKE_JPEG_BYTES)
    script = tmp_path / "fake_exiftool.sh"
    _make_executable(
        script,
        f"""#!/bin/sh
for arg in "$@"; do
    if [ "$arg" = "-PreviewImage" ]; then
        cat '{jpeg_source}'
        exit 0
    fi
done
exit 0
""",
    )
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(script, tmp_path / "raw.cr2", dest)

    assert ok is True
    assert dest.read_bytes() == _FAKE_JPEG_BYTES


def test_extract_embedded_preview_empty_output_returns_false(tmp_path):
    script = tmp_path / "fake_exiftool.sh"
    _make_executable(script, "#!/bin/sh\nexit 0\n")
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(script, tmp_path / "raw.cr2", dest)

    assert ok is False
    assert not dest.exists()


def test_extract_embedded_preview_missing_binary_returns_false(tmp_path):
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(tmp_path / "does-not-exist", tmp_path / "raw.cr2", dest)

    assert ok is False
    assert not dest.exists()


def test_extract_embedded_preview_timeout_returns_false(tmp_path, monkeypatch):
    import subprocess

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="exiftool", timeout=30)

    monkeypatch.setattr(exiftool.subprocess, "run", _raise_timeout)
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(tmp_path / "exiftool", tmp_path / "raw.cr2", dest)

    assert ok is False


def test_extract_embedded_preview_uses_flag_args_not_shell(tmp_path):
    """-JpgFromRaw and -PreviewImage must be passed as literal argv items,
    never shell-interpolated (no shell=True anywhere in the module).
    """
    script = tmp_path / "fake_exiftool.sh"
    jpeg_source = tmp_path / "source.jpg"
    jpeg_source.write_bytes(_FAKE_JPEG_BYTES)
    _make_executable(
        script,
        f"""#!/bin/sh
if [ "$1" = "-b" ] && [ "$2" = "-JpgFromRaw" ]; then
    cat '{jpeg_source}'
fi
""",
    )
    dest = tmp_path / "out.jpg"

    ok = exiftool.extract_embedded_preview(script, tmp_path / "some; rm -rf /.cr2", dest)

    assert ok is True
    assert dest.read_bytes() == _FAKE_JPEG_BYTES

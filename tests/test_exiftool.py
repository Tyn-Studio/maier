import hashlib
import io
import os
import stat
import tarfile
import threading
import time
from pathlib import Path

import pytest
from django.conf import settings

from culler.core import exiftool


@pytest.fixture(autouse=True)
def _reset_exiftool_cache():
    exiftool._reset_cache()
    yield
    exiftool._reset_cache()


@pytest.fixture(autouse=True)
def _reset_exiftool_pin(monkeypatch):
    """EXIFTOOL_SHA256 defaults to None (unpinned placeholder); individual
    download tests set it explicitly. Also make sure the dev escape hatch
    env var doesn't leak in from the real environment.
    """
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", None)
    monkeypatch.delenv("CULLER_ALLOW_UNPINNED_EXIFTOOL", raising=False)


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


def test_find_exiftool_falls_back_to_versioned_dir(monkeypatch, tmp_path):
    """T12: after PATH and the single-file data-dir probe, also check the
    versioned directory `ensure_exiftool()`'s download extracts into.
    """
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: None)
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", tmp_path)
    versioned = (
        tmp_path
        / f"exiftool-{exiftool.EXIFTOOL_VERSION}"
        / f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}"
        / "exiftool"
    )
    _make_executable(versioned, "#!/bin/sh\necho hi\n")

    assert exiftool.find_exiftool() == versioned


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


# --- ensure_exiftool (T12: pinned auto-download) ------------------------


def _build_tarball(dest: Path, *, malicious: bool = False) -> Path:
    """A real .tar.gz shaped like the upstream Image-ExifTool release:
    `Image-ExifTool-{VERSION}/exiftool` (executable sh stand-in) + a lib
    file alongside it. `malicious=True` adds a member escaping the
    destination via `..`, exercising the `filter="data"` safety net.
    """
    with tarfile.open(dest, "w:gz") as tf:
        script_bytes = b"#!/bin/sh\necho fake-exiftool\n"
        info = tarfile.TarInfo(name=f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}/exiftool")
        info.size = len(script_bytes)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(script_bytes))

        lib_bytes = b"# fake lib file\n"
        lib_info = tarfile.TarInfo(
            name=f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}/lib/Image/ExifTool/Fake.pm"
        )
        lib_info.size = len(lib_bytes)
        tf.addfile(lib_info, io.BytesIO(lib_bytes))

        if malicious:
            evil_bytes = b"pwned\n"
            evil_info = tarfile.TarInfo(name="../evil")
            evil_info.size = len(evil_bytes)
            tf.addfile(evil_info, io.BytesIO(evil_bytes))
    return dest


def _sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _setup_download_env(monkeypatch, tmp_path):
    """Common wiring: no PATH exiftool, fresh isolated global data dir."""
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: None)
    data_dir = tmp_path / "global-data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", data_dir)
    return data_dir


def test_ensure_exiftool_returns_path_found_without_downloading(monkeypatch):
    monkeypatch.setattr(exiftool.shutil, "which", lambda name: "/usr/bin/exiftool")

    def _boom(url, dest):
        raise AssertionError("_fetch should not be called when exiftool is already on PATH")

    monkeypatch.setattr(exiftool, "_fetch", _boom)

    assert exiftool.ensure_exiftool() == Path("/usr/bin/exiftool")


def test_ensure_exiftool_downloads_and_extracts_with_correct_pin(monkeypatch, tmp_path):
    data_dir = _setup_download_env(monkeypatch, tmp_path)
    source_tarball = _build_tarball(tmp_path / "source.tar.gz")
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", _sha256_of(source_tarball))

    def _fake_fetch(url, dest):
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    result = exiftool.ensure_exiftool(background=False)

    expected = (
        data_dir
        / f"exiftool-{exiftool.EXIFTOOL_VERSION}"
        / f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}"
        / "exiftool"
    )
    assert result == expected
    assert result.is_file()
    assert os.access(result, os.X_OK)
    assert (expected.parent / "lib" / "Image" / "ExifTool" / "Fake.pm").is_file()


def test_ensure_exiftool_checksum_mismatch_returns_none_and_cleans_up(monkeypatch, tmp_path):
    data_dir = _setup_download_env(monkeypatch, tmp_path)
    source_tarball = _build_tarball(tmp_path / "source.tar.gz")
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", "0" * 64)

    def _fake_fetch(url, dest):
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    result = exiftool.ensure_exiftool(background=False)

    assert result is None
    assert not (data_dir / f"exiftool-{exiftool.EXIFTOOL_VERSION}").exists()


def test_ensure_exiftool_malicious_member_rejected(monkeypatch, tmp_path):
    data_dir = _setup_download_env(monkeypatch, tmp_path)
    source_tarball = _build_tarball(tmp_path / "evil.tar.gz", malicious=True)
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", _sha256_of(source_tarball))

    def _fake_fetch(url, dest):
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    result = exiftool.ensure_exiftool(background=False)

    assert result is None
    assert not (data_dir / f"exiftool-{exiftool.EXIFTOOL_VERSION}").exists()
    # the escaping member must never land outside the data dir either
    assert not (tmp_path / "evil").exists()


def test_ensure_exiftool_refuses_when_sha_unpinned_and_no_override(monkeypatch, tmp_path):
    _setup_download_env(monkeypatch, tmp_path)
    assert exiftool.EXIFTOOL_SHA256 is None

    def _boom(url, dest):
        raise AssertionError("_fetch should not be called without a pinned checksum")

    monkeypatch.setattr(exiftool, "_fetch", _boom)

    assert exiftool.ensure_exiftool(background=False) is None


def test_ensure_exiftool_unpinned_override_proceeds(monkeypatch, tmp_path):
    data_dir = _setup_download_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CULLER_ALLOW_UNPINNED_EXIFTOOL", "1")
    assert exiftool.EXIFTOOL_SHA256 is None
    source_tarball = _build_tarball(tmp_path / "source.tar.gz")

    def _fake_fetch(url, dest):
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    result = exiftool.ensure_exiftool(background=False)

    expected = (
        data_dir
        / f"exiftool-{exiftool.EXIFTOOL_VERSION}"
        / f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}"
        / "exiftool"
    )
    assert result == expected


def test_ensure_exiftool_background_returns_none_then_completes(monkeypatch, tmp_path):
    data_dir = _setup_download_env(monkeypatch, tmp_path)
    source_tarball = _build_tarball(tmp_path / "source.tar.gz")
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", _sha256_of(source_tarball))

    def _fake_fetch(url, dest):
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    result = exiftool.ensure_exiftool(background=True)
    assert result is None

    expected = (
        data_dir
        / f"exiftool-{exiftool.EXIFTOOL_VERSION}"
        / f"Image-ExifTool-{exiftool.EXIFTOOL_VERSION}"
        / "exiftool"
    )
    deadline = time.monotonic() + 10
    found = None
    while time.monotonic() < deadline:
        exiftool._reset_cache()
        found = exiftool.find_exiftool()
        if found is not None:
            break
        time.sleep(0.05)

    assert found == expected


def test_ensure_exiftool_single_flight(monkeypatch, tmp_path):
    _setup_download_env(monkeypatch, tmp_path)
    source_tarball = _build_tarball(tmp_path / "source.tar.gz")
    monkeypatch.setattr(exiftool, "EXIFTOOL_SHA256", _sha256_of(source_tarball))

    call_count = 0
    call_lock = threading.Lock()

    def _fake_fetch(url, dest):
        nonlocal call_count
        with call_lock:
            call_count += 1
        time.sleep(0.2)  # widen the window so both threads are in-flight
        dest.write_bytes(source_tarball.read_bytes())

    monkeypatch.setattr(exiftool, "_fetch", _fake_fetch)

    results = []

    def _call():
        results.append(exiftool.ensure_exiftool(background=False))

    threads = [threading.Thread(target=_call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert call_count == 1
    assert len(results) == 2
    assert all(r is not None and r.is_file() for r in results)

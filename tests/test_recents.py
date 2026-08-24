"""maier.recents deliberately avoids Django settings (it runs pre-boot),
so these are plain unit tests with the config dir monkeypatched to tmp_path
via MAIER_CONFIG_DIR (the same override the module itself consults)."""

import json

import pytest

from maier import recents


@pytest.fixture(autouse=True)
def _config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("MAIER_CONFIG_DIR", str(config_dir))
    return config_dir


def _make_folder(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_load_recents_empty_when_no_file():
    assert recents.load_recents() == []


def test_record_and_load_single_entry(tmp_path):
    folder = _make_folder(tmp_path, "a")
    recents.record_recent(folder)

    loaded = recents.load_recents()
    assert len(loaded) == 1
    assert loaded[0]["path"] == str(folder.resolve())
    assert "last_opened" in loaded[0]


def test_reorder_on_reopen(tmp_path):
    a = _make_folder(tmp_path, "a")
    b = _make_folder(tmp_path, "b")

    recents.record_recent(a)
    recents.record_recent(b)
    recents.record_recent(a)  # re-open a -> should move back to front

    loaded = recents.load_recents()
    paths = [entry["path"] for entry in loaded]
    assert paths == [str(a.resolve()), str(b.resolve())]


def test_cap_at_ten(tmp_path):
    for i in range(12):
        recents.record_recent(_make_folder(tmp_path, f"folder-{i}"))

    loaded = recents.load_recents()
    assert len(loaded) == 10
    # most recently opened (folder-11) first, oldest two (0, 1) evicted
    assert loaded[0]["path"] == str((tmp_path / "folder-11").resolve())
    paths = {entry["path"] for entry in loaded}
    assert str((tmp_path / "folder-0").resolve()) not in paths
    assert str((tmp_path / "folder-1").resolve()) not in paths


def test_corrupt_file_tolerated(_config_dir):
    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / recents.RECENTS_FILENAME).write_text("not json{{{")

    assert recents.load_recents() == []


def test_non_list_json_tolerated(_config_dir):
    _config_dir.mkdir(parents=True, exist_ok=True)
    (_config_dir / recents.RECENTS_FILENAME).write_text(json.dumps({"oops": True}))

    assert recents.load_recents() == []


def test_vanished_folder_dropped_on_load(tmp_path):
    folder = _make_folder(tmp_path, "gone")
    recents.record_recent(folder)
    assert len(recents.load_recents()) == 1

    import shutil

    shutil.rmtree(folder)

    assert recents.load_recents() == []


def test_record_recent_writes_atomically_no_tmp_leftover(tmp_path, _config_dir):
    folder = _make_folder(tmp_path, "a")
    recents.record_recent(folder)

    leftovers = list(_config_dir.glob("*.tmp"))
    assert leftovers == []
    assert (_config_dir / recents.RECENTS_FILENAME).exists()

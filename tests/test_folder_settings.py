"""Durable per-folder export settings (PLAN T25): round-trip, defaults,
corrupt-file quarantine. No Django DB needed -- this module is pure
filesystem/JSON, same pattern as test_remote_state.py.
"""

import json

from maier.core.folder_settings import (
    FolderSettings,
    load_settings,
    save_settings,
)


def test_load_settings_missing_file_returns_defaults(tmp_path):
    settings = load_settings(tmp_path)

    assert settings.export_destination == ""
    assert settings.export_mode == "manual"
    assert settings.export_date_prefix is False


def test_save_then_load_round_trips(tmp_path):
    settings = FolderSettings(
        export_destination="/Volumes/Backup/exports",
        export_mode="automatic",
        export_date_prefix=True,
    )

    save_settings(tmp_path, settings)
    loaded = load_settings(tmp_path)

    assert loaded.export_destination == "/Volumes/Backup/exports"
    assert loaded.export_mode == "automatic"
    assert loaded.export_date_prefix is True


def test_save_settings_writes_expected_json_schema(tmp_path):
    settings = FolderSettings(export_destination="/dest", export_mode="manual")

    save_settings(tmp_path, settings)

    path = tmp_path / "maier-settings.json"
    data = json.loads(path.read_text())
    assert data["export_destination"] == "/dest"
    assert data["export_mode"] == "manual"
    assert data["export_date_prefix"] is False
    assert data["version"] == 1


def test_save_settings_is_atomic_no_stray_tmp_files(tmp_path):
    save_settings(tmp_path, FolderSettings())

    names = {p.name for p in tmp_path.iterdir()}
    assert names == {"maier-settings.json"}


def test_save_settings_overwrites_existing_file(tmp_path):
    save_settings(tmp_path, FolderSettings(export_destination="/a"))
    save_settings(tmp_path, FolderSettings(export_destination="/b"))

    loaded = load_settings(tmp_path)
    assert loaded.export_destination == "/b"


def test_invalid_export_mode_falls_back_to_manual(tmp_path):
    path = tmp_path / "maier-settings.json"
    path.write_text(json.dumps({"export_mode": "bogus"}))

    loaded = load_settings(tmp_path)
    assert loaded.export_mode == "manual"


def test_corrupt_file_quarantined_and_defaults_returned(tmp_path):
    path = tmp_path / "maier-settings.json"
    path.write_text("{not json")

    loaded = load_settings(tmp_path)

    assert loaded.export_destination == ""
    assert loaded.export_mode == "manual"
    corrupt_files = list(tmp_path.glob("maier-settings.json.corrupt-*"))
    assert len(corrupt_files) == 1
    assert not path.exists()


def test_corrupt_file_not_a_json_object_quarantined(tmp_path):
    path = tmp_path / "maier-settings.json"
    path.write_text(json.dumps([1, 2, 3]))

    loaded = load_settings(tmp_path)

    assert loaded.export_destination == ""
    corrupt_files = list(tmp_path.glob("maier-settings.json.corrupt-*"))
    assert len(corrupt_files) == 1

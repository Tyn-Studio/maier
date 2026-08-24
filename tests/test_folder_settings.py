"""Durable per-folder export settings (PLAN T25): round-trip, defaults,
corrupt-file quarantine. No Django DB needed -- this module is pure
filesystem/JSON, same pattern as test_remote_state.py.
"""

import json
from datetime import date

from maier.core.folder_settings import (
    FolderSettings,
    load_settings,
    save_settings,
    working_range,
)


def test_load_settings_missing_file_returns_defaults(tmp_path):
    settings = load_settings(tmp_path)

    assert settings.export_destination == ""
    assert settings.export_mode == "manual"
    assert settings.export_date_prefix is False
    assert settings.working_from == ""
    assert settings.working_to == ""


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


# --- working date range (PLAN T29) -------------------------------------------


def test_working_from_to_round_trip(tmp_path):
    settings = FolderSettings(working_from="2026-02-01", working_to="2026-03-17")

    save_settings(tmp_path, settings)
    loaded = load_settings(tmp_path)

    assert loaded.working_from == "2026-02-01"
    assert loaded.working_to == "2026-03-17"


def test_working_range_unset_when_both_empty():
    assert working_range(FolderSettings()) is None
    assert working_range(FolderSettings(working_from="", working_to="")) is None


def test_working_range_parses_both_sides():
    settings = FolderSettings(working_from="2026-02-01", working_to="2026-03-17")

    assert working_range(settings) == (date(2026, 2, 1), date(2026, 3, 17))


def test_working_range_open_ended_on_one_side_is_not_unset():
    # A blank `working_to` with a real `working_from` is a real, deliberate
    # range (open on the upper end), not "never configured".
    settings = FolderSettings(working_from="2026-02-01", working_to="")

    result = working_range(settings)
    assert result is not None
    assert result == (date(2026, 2, 1), None)

    settings2 = FolderSettings(working_from="", working_to="2026-03-17")
    result2 = working_range(settings2)
    assert result2 is not None
    assert result2 == (None, date(2026, 3, 17))


def test_working_range_tolerant_parse_junk_becomes_none():
    settings = FolderSettings(working_from="not-a-date", working_to="2026-03-17")

    result = working_range(settings)
    assert result == (None, date(2026, 3, 17))


def test_working_range_everything_sentinel_distinct_from_unset():
    # "Everything" preset saves an explicit working_from rather than leaving
    # both blank -- distinguishable from "never set up".
    settings = FolderSettings(working_from="1970-01-01", working_to="")

    result = working_range(settings)
    assert result is not None
    assert result == (date(1970, 1, 1), None)

"""Durable per-account iCloud state (SPEC §18, PLAN T16): round-trip,
atomicity, corrupt-file quarantine, and account discovery. No Django DB
needed -- this module is pure filesystem/JSON.
"""

import json
from datetime import UTC, datetime

from maier.core.remote_state import (
    AccountState,
    list_accounts,
    load_state,
    save_state,
)


def test_load_state_missing_file_returns_fresh_empty_state(tmp_path):
    state = load_state(tmp_path, "luis@example.com")

    assert state.account == "luis@example.com"
    assert state.cursor is None
    assert state.decisions == {}
    assert state.downloaded == {}


def test_save_then_load_round_trips(tmp_path):
    cursor = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)
    state = AccountState(
        account="luis@example.com",
        cursor=cursor,
        decisions={"abc123": "rejected", "def456": "optional"},
        downloaded={"abc123": "selected/apple-luis/IMG_0001.jpg"},
    )

    save_state(tmp_path, state)
    loaded = load_state(tmp_path, "luis@example.com")

    assert loaded.account == "luis@example.com"
    assert loaded.cursor == cursor
    assert loaded.decisions == {"abc123": "rejected", "def456": "optional"}
    assert loaded.downloaded == {"abc123": "selected/apple-luis/IMG_0001.jpg"}


def test_save_state_writes_expected_json_schema(tmp_path):
    cursor = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)
    state = AccountState(account="luis@example.com", cursor=cursor)

    save_state(tmp_path, state)

    path = tmp_path / "icloud-state" / "luis-example-com.json"
    data = json.loads(path.read_text())
    assert data["account"] == "luis@example.com"
    assert data["cursor"] == cursor.isoformat()
    assert data["decisions"] == {}
    assert data["downloaded"] == {}
    assert data["version"] == 1


def test_save_state_no_cursor_writes_null(tmp_path):
    state = AccountState(account="luis@example.com")
    save_state(tmp_path, state)

    path = tmp_path / "icloud-state" / "luis-example-com.json"
    data = json.loads(path.read_text())
    assert data["cursor"] is None


def test_save_state_is_atomic_no_stray_tmp_files(tmp_path):
    state = AccountState(account="luis@example.com")
    save_state(tmp_path, state)

    state_dir = tmp_path / "icloud-state"
    names = {p.name for p in state_dir.iterdir()}
    assert names == {"luis-example-com.json"}


def test_save_state_overwrites_existing_file(tmp_path):
    state = AccountState(account="luis@example.com", decisions={"a": "rejected"})
    save_state(tmp_path, state)

    state.decisions["b"] = "optional"
    save_state(tmp_path, state)

    loaded = load_state(tmp_path, "luis@example.com")
    assert loaded.decisions == {"a": "rejected", "b": "optional"}


def test_corrupt_file_quarantined_and_fresh_state_returned(tmp_path):
    state_dir = tmp_path / "icloud-state"
    state_dir.mkdir(parents=True)
    bad_path = state_dir / "luis-example-com.json"
    bad_path.write_text("not valid json {{{")

    state = load_state(tmp_path, "luis@example.com")

    assert state.account == "luis@example.com"
    assert state.cursor is None
    assert state.decisions == {}
    assert state.downloaded == {}

    # Original corrupt file renamed aside, not overwritten/deleted.
    assert not bad_path.exists()
    quarantined = list(state_dir.glob("luis-example-com.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "not valid json {{{"


def test_corrupt_file_not_silently_overwritten_by_next_save(tmp_path):
    state_dir = tmp_path / "icloud-state"
    state_dir.mkdir(parents=True)
    bad_path = state_dir / "luis-example-com.json"
    bad_path.write_text("{not json")

    state = load_state(tmp_path, "luis@example.com")
    state.decisions["x"] = "rejected"
    save_state(tmp_path, state)

    quarantined = list(state_dir.glob("luis-example-com.json.corrupt-*"))
    assert len(quarantined) == 1
    # The fresh state was written normally at the canonical path.
    reloaded = load_state(tmp_path, "luis@example.com")
    assert reloaded.decisions == {"x": "rejected"}


def test_non_dict_json_treated_as_corrupt(tmp_path):
    state_dir = tmp_path / "icloud-state"
    state_dir.mkdir(parents=True)
    bad_path = state_dir / "luis-example-com.json"
    bad_path.write_text("[1, 2, 3]")

    state = load_state(tmp_path, "luis@example.com")

    assert state.decisions == {}
    assert not bad_path.exists()
    assert list(state_dir.glob("luis-example-com.json.corrupt-*"))


def test_list_accounts_empty_when_no_state_dir(tmp_path):
    assert list_accounts(tmp_path) == []


def test_list_accounts_lists_all_saved_accounts(tmp_path):
    save_state(tmp_path, AccountState(account="luis@example.com"))
    save_state(tmp_path, AccountState(account="maria@example.com"))

    accounts = list_accounts(tmp_path)

    assert set(accounts) == {"luis@example.com", "maria@example.com"}


def test_list_accounts_excludes_quarantined_corrupt_files(tmp_path):
    save_state(tmp_path, AccountState(account="luis@example.com"))

    state_dir = tmp_path / "icloud-state"
    bad_path = state_dir / "maria-example-com.json"
    bad_path.write_text("not json")
    load_state(tmp_path, "maria@example.com")  # triggers quarantine rename

    accounts = list_accounts(tmp_path)

    assert accounts == ["luis@example.com"]


def test_slug_is_filename_safe():
    from maier.core.remote_state import _slug

    assert _slug("Luis.Natera+test@Example.COM") == "luis-natera-test-example-com"
    assert _slug("") == "account"

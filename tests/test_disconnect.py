"""Disconnect an iCloud account (SPEC §18, PLAN M5 T21): deletes the
account's saved session + its remote (`source="icloud"`) DB rows + cached
previews, but MUST leave `icloud-state/{slug}.json`, everything in
`selected/`, and every other account completely untouched.
"""

from datetime import UTC, datetime

import pytest
from django.conf import settings

from maier.core import disconnect, pull, remote_state
from maier.core.models import Photo

_CAPTURED = datetime(2025, 6, 14, 18, 30, 12, tzinfo=UTC)
_ACCOUNT_A = "t_t21_a@example.com"
_ACCOUNT_B = "t_t21_b@example.com"


@pytest.fixture(autouse=True)
def _global_data_dir(tmp_path, monkeypatch):
    data_dir = tmp_path / "global-data"
    monkeypatch.setattr(settings, "GLOBAL_DATA_DIR", data_dir)
    return data_dir


@pytest.fixture(autouse=True)
def _reset_current_pulls():
    pull._current_pulls.clear()
    yield
    pull._current_pulls.clear()


def _remote_photo(account: str, remote_id: str, **overrides) -> Photo:
    kwargs = dict(
        source=Photo.SOURCE_ICLOUD,
        account=account,
        remote_id=remote_id,
        relative_path=f"@icloud/{account}/{remote_id}",
        status=Photo.STATUS_OPTIONAL,
        provenance=remote_state.account_slug(account),
        file_size=1000,
        file_mtime=0.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
        remote_filename=f"{remote_id}.jpg",
    )
    kwargs.update(overrides)
    return Photo.objects.create(**kwargs)


def _local_photo(relative_path: str, **overrides) -> Photo:
    kwargs = dict(
        source=Photo.SOURCE_LOCAL,
        relative_path=relative_path,
        status=Photo.STATUS_SELECTED,
        provenance="",
        file_size=1234,
        file_mtime=1_700_000_000.0,
        captured_at=_CAPTURED,
        captured_at_source="exif",
        media_type=Photo.MEDIA_IMAGE,
    )
    kwargs.update(overrides)
    return Photo.objects.create(**kwargs)


def _preview_file(folder, account: str, remote_id: str) -> None:
    d = folder / ".maier" / "previews"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"icloud-{remote_state.account_slug(account)}-{remote_id}.jpg").write_bytes(b"jpg")


def _session_dir(account: str):
    return settings.GLOBAL_DATA_DIR / "icloud-sessions" / remote_state.account_slug(account)


@pytest.mark.django_db
def test_disconnect_removes_only_that_accounts_remote_rows_and_previews(tmp_path):
    _remote_photo(_ACCOUNT_A, "r1")
    _remote_photo(_ACCOUNT_A, "r2")
    _remote_photo(_ACCOUNT_B, "r3")
    _preview_file(tmp_path, _ACCOUNT_A, "r1")
    _preview_file(tmp_path, _ACCOUNT_A, "r2")
    _preview_file(tmp_path, _ACCOUNT_B, "r3")

    result = disconnect.disconnect_account(tmp_path, _ACCOUNT_A)

    assert result.rows_removed == 2
    assert result.previews_removed == 2
    assert not Photo.objects.filter(account=_ACCOUNT_A).exists()
    assert Photo.objects.filter(account=_ACCOUNT_B, remote_id="r3").exists()

    previews_dir = tmp_path / ".maier" / "previews"
    remaining = {p.name for p in previews_dir.glob("*.jpg")}
    assert remaining == {f"icloud-{remote_state.account_slug(_ACCOUNT_B)}-r3.jpg"}


@pytest.mark.django_db
def test_disconnect_keeps_local_converted_row_with_account_set(tmp_path):
    # A downloaded-and-converted selection: source flips to "local" but
    # account/remote_id are deliberately left in place (downloads.py
    # _convert_to_local) -- disconnect must filter on source, not account.
    converted = _local_photo(
        f"selected/{remote_state.account_slug(_ACCOUNT_A)}/r1.jpg",
        account=_ACCOUNT_A,
        remote_id="r1",
    )
    _remote_photo(_ACCOUNT_A, "r2")

    result = disconnect.disconnect_account(tmp_path, _ACCOUNT_A)

    assert result.rows_removed == 1
    converted.refresh_from_db()
    assert converted.source == Photo.SOURCE_LOCAL
    assert converted.account == _ACCOUNT_A
    assert converted.remote_id == "r1"
    assert not Photo.objects.filter(source=Photo.SOURCE_ICLOUD, account=_ACCOUNT_A).exists()


@pytest.mark.django_db
def test_disconnect_leaves_state_file_untouched(tmp_path):
    state = remote_state.AccountState(
        account=_ACCOUNT_A,
        cursor=_CAPTURED,
        decisions={"r1": "rejected"},
        downloaded={"r2": "selected/foo/r2.jpg"},
    )
    remote_state.save_state(tmp_path, state)
    _remote_photo(_ACCOUNT_A, "r1")

    disconnect.disconnect_account(tmp_path, _ACCOUNT_A)

    reloaded = remote_state.load_state(tmp_path, _ACCOUNT_A)
    assert reloaded.decisions == {"r1": "rejected"}
    assert reloaded.downloaded == {"r2": "selected/foo/r2.jpg"}
    assert reloaded.cursor == _CAPTURED
    assert _ACCOUNT_A in remote_state.list_accounts(tmp_path)


@pytest.mark.django_db
def test_disconnect_removes_session_dir():
    session_dir = _session_dir(_ACCOUNT_A)
    session_dir.mkdir(parents=True)
    (session_dir / "session.json").write_text("{}")

    disconnect.disconnect_account(settings.WORKING_FOLDER, _ACCOUNT_A)

    assert not session_dir.exists()


@pytest.mark.django_db
def test_disconnect_unknown_account_is_noop(tmp_path):
    result = disconnect.disconnect_account(tmp_path, "t_t21_never_attached@example.com")

    assert result.rows_removed == 0
    assert result.previews_removed == 0


@pytest.mark.django_db
def test_disconnect_idempotent_second_call_returns_zeros(tmp_path):
    _remote_photo(_ACCOUNT_A, "r1")
    _preview_file(tmp_path, _ACCOUNT_A, "r1")

    first = disconnect.disconnect_account(tmp_path, _ACCOUNT_A)
    second = disconnect.disconnect_account(tmp_path, _ACCOUNT_A)

    assert first.rows_removed == 1
    assert second.rows_removed == 0
    assert second.previews_removed == 0


@pytest.mark.django_db
def test_disconnect_refuses_when_pull_in_flight(tmp_path, monkeypatch):
    _remote_photo(_ACCOUNT_A, "r1")
    monkeypatch.setattr(disconnect, "pull_in_flight", lambda account: True)

    with pytest.raises(disconnect.PullInFlight):
        disconnect.disconnect_account(tmp_path, _ACCOUNT_A)

    assert Photo.objects.filter(account=_ACCOUNT_A, source=Photo.SOURCE_ICLOUD).exists()


def test_pull_in_flight_reads_pull_module_state():
    assert disconnect.pull_in_flight(_ACCOUNT_A) is False

    progress = pull.PullProgress(account=_ACCOUNT_A, finished=False)
    pull._current_pulls[_ACCOUNT_A] = progress
    assert disconnect.pull_in_flight(_ACCOUNT_A) is True

    progress.finished = True
    assert disconnect.pull_in_flight(_ACCOUNT_A) is False

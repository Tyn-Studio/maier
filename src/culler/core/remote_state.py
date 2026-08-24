"""Durable per-account iCloud pull state (SPEC §18, CLAUDE.md hard rule 9):
JSON files at `{folder}/icloud-state/{account-slug}.json`. Deliberately NOT
under `.culler/` -- this is user state (sync cursor + per-item decisions),
not a rebuildable cache, so it must survive `.culler/` deletion and travel
with the folder on disk/between machines.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

STATE_VERSION = 1

DECISION_REJECTED = "rejected"
DECISION_OPTIONAL = "optional"

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see core/previews.py / recents.py for the same workaround).
_LOAD_ERRORS = (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError)
_LIST_ACCOUNTS_ERRORS = (json.JSONDecodeError, OSError, ValueError)


@dataclass
class AccountState:
    account: str
    cursor: datetime | None = None
    decisions: dict[str, str] = field(default_factory=dict)  # remote_id -> "rejected" | "optional"
    downloaded: dict[str, str] = field(default_factory=dict)  # remote_id -> relative_path


def account_slug(account: str) -> str:
    """Canonical filesystem-safe name for an Apple ID. THE single
    implementation: state filenames here, session dirs and preview keys
    elsewhere all import this one — a divergence would silently split an
    account's state across different filenames.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", account.strip().lower()).strip("-")
    return slug or "account"


_slug = account_slug


def _state_dir_for_read(folder: Path) -> Path:
    return Path(folder) / "icloud-state"


def _state_dir_for_write(folder: Path) -> Path:
    d = _state_dir_for_read(folder)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(folder: Path, account: str) -> Path:
    return _state_dir_for_read(folder) / f"{_slug(account)}.json"


def load_state(folder: Path, account: str) -> AccountState:
    """Missing file -> fresh empty state. Corrupt file -> also a fresh empty
    state, but the unreadable file is renamed aside to `<name>.corrupt-<ts>`
    first rather than silently discarded/overwritten on the next save --
    this is durable user state, not a cache.
    """
    path = _state_path(folder, account)
    try:
        raw = path.read_text()
    except OSError:
        return AccountState(account=account)

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state file does not contain a JSON object")
        cursor_raw = data.get("cursor")
        cursor = datetime.fromisoformat(cursor_raw) if cursor_raw else None
        decisions = {str(k): str(v) for k, v in dict(data.get("decisions") or {}).items()}
        downloaded = {str(k): str(v) for k, v in dict(data.get("downloaded") or {}).items()}
        stored_account = data.get("account") or account
    except _LOAD_ERRORS:
        _quarantine_corrupt_file(path)
        return AccountState(account=account)

    return AccountState(
        account=stored_account, cursor=cursor, decisions=decisions, downloaded=downloaded
    )


def _quarantine_corrupt_file(path: Path) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        os.replace(path, corrupt_path)
    except OSError:
        pass  # best-effort; a subsequent save_state will just overwrite in place


def save_state(folder: Path, state: AccountState) -> None:
    """Atomic write: tmp file in the same directory, then `os.replace`."""
    path = _state_path(folder, state.account)
    state_dir = _state_dir_for_write(folder)
    payload = {
        "account": state.account,
        "cursor": state.cursor.isoformat() if state.cursor else None,
        "decisions": state.decisions,
        "downloaded": state.downloaded,
        "version": STATE_VERSION,
    }

    fd, tmp_name = tempfile.mkstemp(dir=state_dir, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def list_accounts(folder: Path) -> list[str]:
    """Account emails for every valid state file present in
    `{folder}/icloud-state/`. `glob("*.json")` naturally excludes files
    already renamed aside as `*.json.corrupt-<ts>`.
    """
    state_dir = _state_dir_for_read(folder)
    if not state_dir.is_dir():
        return []

    accounts = []
    for path in sorted(state_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except _LIST_ACCOUNTS_ERRORS:
            continue
        if not isinstance(data, dict):
            continue
        account = data.get("account")
        if account:
            accounts.append(account)
    return accounts

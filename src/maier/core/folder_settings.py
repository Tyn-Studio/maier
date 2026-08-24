"""Durable per-folder app settings (PLAN T25): a single JSON file at
`{folder}/maier-settings.json`. Deliberately NOT under `.maier/` -- like
`remote_state.py`'s per-account state, this is user configuration (where to
export, and how), not a rebuildable cache, so it must survive `.maier/`
deletion and travel with the folder on disk/between machines. Pattern-
matches `remote_state.py`: dataclass, atomic writes (tmp file + os.replace),
corrupt-file quarantine rather than silent overwrite.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

SETTINGS_VERSION = 1

MODE_MANUAL = "manual"
MODE_AUTOMATIC = "automatic"
_VALID_MODES = {MODE_MANUAL, MODE_AUTOMATIC}

# Named tuple (not an inline `except (A, B, C):` literal) -- ruff 0.16.4
# formatter bug, see remote_state.py/previews.py for the same workaround.
_LOAD_ERRORS = (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError)


@dataclass
class FolderSettings:
    export_destination: str = ""  # absolute path outside the working folder
    export_mode: str = MODE_MANUAL  # "manual" | "automatic"
    export_date_prefix: bool = False
    # T29: the user-chosen scope for heavy background work (iCloud thumb
    # downloads, pHash sweeps) at large-library scale. ISO date strings
    # ("YYYY-MM-DD"), not `date` objects -- matches this module's plain-JSON
    # storage pattern. Both empty = never configured (setup wizard gate);
    # one side empty = deliberately open-ended on that end (see
    # `working_range` below). "Everything" is saved as an explicit sentinel
    # (working_from="1970-01-01", working_to="") rather than leaving both
    # blank, so it's distinguishable from "never set up".
    working_from: str = ""
    working_to: str = ""


def _settings_path(folder: Path) -> Path:
    return Path(folder) / "maier-settings.json"


def load_settings(folder: Path) -> FolderSettings:
    """Missing file -> defaults. Corrupt file -> also defaults, but the
    unreadable file is renamed aside to `<name>.corrupt-<ts>` first rather
    than being silently discarded/overwritten on the next save -- this is
    durable user state, not a cache.
    """
    path = _settings_path(folder)
    try:
        raw = path.read_text()
    except OSError:
        return FolderSettings()

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("settings file does not contain a JSON object")
        export_destination = str(data.get("export_destination") or "")
        export_mode = str(data.get("export_mode") or MODE_MANUAL)
        if export_mode not in _VALID_MODES:
            export_mode = MODE_MANUAL
        export_date_prefix = bool(data.get("export_date_prefix", False))
        working_from = str(data.get("working_from") or "")
        working_to = str(data.get("working_to") or "")
    except _LOAD_ERRORS:
        _quarantine_corrupt_file(path)
        return FolderSettings()

    return FolderSettings(
        export_destination=export_destination,
        export_mode=export_mode,
        export_date_prefix=export_date_prefix,
        working_from=working_from,
        working_to=working_to,
    )


def _quarantine_corrupt_file(path: Path) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        os.replace(path, corrupt_path)
    except OSError:
        pass  # best-effort; a subsequent save_settings will just overwrite in place


def save_settings(folder: Path, settings: FolderSettings) -> None:
    """Atomic write: tmp file in the same directory, then `os.replace`."""
    folder = Path(folder)
    path = _settings_path(folder)
    payload = asdict(settings)
    payload["version"] = SETTINGS_VERSION

    fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=f".{path.name}-", suffix=".tmp")
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


def _parse_iso_date(value: str) -> date | None:
    """Tolerant `date.fromisoformat` -- unparseable/junk values degrade to
    `None` (open-ended on that side) rather than raising, since this is
    user-editable durable state (T29 brief: "parse tolerantly; junk ->
    None").
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def working_range(settings: FolderSettings) -> tuple[date | None, date | None] | None:
    """T29: the user's chosen scope for heavy background work (iCloud thumb
    downloads, pHash sweeps at large-library scale) -- metadata enumeration
    itself stays whole-library regardless.

    Returns `None` when unset (both `working_from`/`working_to` are the
    empty string -- the setup-wizard gate's signal that it has never been
    configured). Otherwise a `(from_date, to_date)` tuple, either side
    possibly `None` meaning "open" on that end (an explicitly blank side, or
    a value that failed to parse). Note this is deliberately distinct from
    the "unset" case: a range with one side set is a real, user-chosen
    range, not a signal to show the setup wizard again.
    """
    if not settings.working_from and not settings.working_to:
        return None
    return (_parse_iso_date(settings.working_from), _parse_iso_date(settings.working_to))

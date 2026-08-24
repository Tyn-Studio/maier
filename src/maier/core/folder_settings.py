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
from datetime import UTC, datetime
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
    except _LOAD_ERRORS:
        _quarantine_corrupt_file(path)
        return FolderSettings()

    return FolderSettings(
        export_destination=export_destination,
        export_mode=export_mode,
        export_date_prefix=export_date_prefix,
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

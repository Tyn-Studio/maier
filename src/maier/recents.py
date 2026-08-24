"""Persistent recent-folders list for the desktop home screen (SPEC §11).

This lives at the top level (not `maier.core`) and deliberately does NOT
import `maier.settings` / Django: the native folder picker and home screen
(`maier.window`) run *before* Django is booted -- `cli.py` only sets
`MAIER_FOLDER` and calls `django.setup()` once a folder has actually been
chosen. Computing the config directory here duplicates one line of
`settings.py` (`platformdirs.user_config_dir("Maier")` ->
`GLOBAL_CONFIG_DIR`); if the app name or that computation ever changes,
update both places.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import platformdirs

APP_NAME = "Maier"
MAX_RECENTS = 10
RECENTS_FILENAME = "recent_folders.json"

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see core/previews.py for the same workaround).
_LOAD_ERRORS = (json.JSONDecodeError, ValueError)


def _config_dir() -> Path:
    # MAIER_CONFIG_DIR lets subprocess-based CLI tests redirect the global
    # config dir hermetically (platformdirs.user_config_dir honors
    # XDG_CONFIG_HOME on Linux but not macOS, where it's always
    # ~/Library/Application Support/Maier). Not used by the app otherwise.
    override = os.environ.get("MAIER_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_config_dir(APP_NAME))


def _recents_path() -> Path:
    return _config_dir() / RECENTS_FILENAME


def load_recents() -> list[dict]:
    """Return recent folders, most-recent first. Tolerates a missing or
    corrupt file (returns []) and silently drops entries whose folder no
    longer exists."""
    try:
        raw = _recents_path().read_text()
    except OSError:
        return []

    try:
        data = json.loads(raw)
    except _LOAD_ERRORS:
        return []

    if not isinstance(data, list):
        return []

    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        last_opened = entry.get("last_opened")
        if not path or not last_opened:
            continue
        if not Path(path).is_dir():
            continue
        result.append({"path": str(path), "last_opened": str(last_opened)})
    return result


def record_recent(path: Path) -> None:
    """Upsert `path` to the front of the recents list, capped at
    MAX_RECENTS, written atomically (tmp file + os.replace)."""
    resolved = str(Path(path).resolve())

    recents = [r for r in load_recents() if r["path"] != resolved]
    recents.insert(0, {"path": resolved, "last_opened": datetime.now(UTC).isoformat()})
    recents = recents[:MAX_RECENTS]

    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    dest = config_dir / RECENTS_FILENAME

    fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=".recent_folders-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(recents, f, indent=2)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

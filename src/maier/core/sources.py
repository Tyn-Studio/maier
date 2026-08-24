"""Source registry + per-source durable decisions (SPEC §19, M6 first wave).

Local sources are indexed in place (never moved/modified/deleted -- CLAUDE.md
hard rule 1-2, extended to sources by §19). Durable culling decisions for a
local source live in a sidecar `{source.path}/maier-state.json` -- same
pattern as `core/remote_state.py`'s `{folder}/icloud-state/{account}.json`
(read/quarantine/atomic-write helpers below are copied and adapted from that
module, not imported from it -- remote_state.py is untouched per brief).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from django.db.models import QuerySet

from .models import Source

STATE_VERSION = 1

DECISION_SELECTED = "selected"
DECISION_REJECTED = "rejected"
DECISION_OPTIONAL = "optional"

STATE_FILENAME = "maier-state.json"

# named tuple (not an inline literal) to sidestep a ruff 0.16.4 formatter bug
# that strips the parens from `except (A, B, C):` when it fits on one line
# (see core/remote_state.py / core/previews.py for the same workaround).
_LOAD_ERRORS = (json.JSONDecodeError, ValueError, TypeError, KeyError, AttributeError)


@dataclass
class SourceState:
    decisions: dict[str, str] = field(default_factory=dict)  # rel path -> "selected" | "rejected"
    version: int = STATE_VERSION


def _is_inside(path: Path, other: Path) -> bool:
    """True if `path` is `other` itself or somewhere underneath it."""
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def add_local_source(library: Path, path: Path, name: str | None = None) -> Source:
    """Register a local folder as a source.

    Refuses (ValueError): `path` isn't a directory; `path` is inside the
    library root (or is the library root); `path` is inside (or is) an
    already-registered source's path. Name defaults to the folder basename,
    de-duped with " (1)", " (2)", ... against existing Source names.
    """
    library = Path(library).resolve()
    path = Path(path).resolve()

    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")

    if _is_inside(path, library):
        raise ValueError(f"path is inside the library root: {path}")

    for existing in Source.objects.filter(kind=Source.KIND_LOCAL):
        existing_path = Path(existing.path)
        if _is_inside(path, existing_path):
            raise ValueError(f"path is inside an already-registered source: {existing.name!r}")

    base_name = name or path.name or str(path)
    final_name = base_name
    suffix = 1
    while Source.objects.filter(name=final_name).exists():
        final_name = f"{base_name} ({suffix})"
        suffix += 1

    return Source.objects.create(kind=Source.KIND_LOCAL, name=final_name, path=str(path))


def list_sources() -> QuerySet[Source]:
    return Source.objects.all().order_by("added_at", "pk")


def get_or_create_icloud_source(account: str) -> Source:
    source, _created = Source.objects.get_or_create(
        kind=Source.KIND_ICLOUD,
        account=account,
        defaults={"name": account, "path": ""},
    )
    return source


# --- sidecar state: {source.path}/maier-state.json --------------------------


def _state_path(source: Source) -> Path:
    return Path(source.path) / STATE_FILENAME


def load_source_state(source: Source) -> SourceState:
    """Missing file -> fresh empty state. Corrupt file -> also a fresh empty
    state, but the unreadable file is renamed aside to `<name>.corrupt-<ts>`
    first rather than silently discarded/overwritten on the next save --
    this is durable user state, not a cache (mirrors remote_state.load_state).
    """
    path = _state_path(source)
    try:
        raw = path.read_text()
    except OSError:
        return SourceState()

    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state file does not contain a JSON object")
        decisions = {str(k): str(v) for k, v in dict(data.get("decisions") or {}).items()}
        version = int(data.get("version") or STATE_VERSION)
    except _LOAD_ERRORS:
        _quarantine_corrupt_file(path)
        return SourceState()

    return SourceState(decisions=decisions, version=version)


def _quarantine_corrupt_file(path: Path) -> None:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    corrupt_path = path.with_name(f"{path.name}.corrupt-{ts}")
    try:
        os.replace(path, corrupt_path)
    except OSError:
        pass  # best-effort; a subsequent save_source_state will just overwrite in place


def save_source_state(source: Source, state: SourceState) -> None:
    """Atomic write: tmp file in the same directory (the source root itself),
    then `os.replace`.
    """
    path = _state_path(source)
    payload = {"decisions": state.decisions, "version": STATE_VERSION}

    fd, tmp_name = tempfile.mkstemp(
        dir=str(Path(source.path)), prefix=f".{path.name}-", suffix=".tmp"
    )
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


def record_decision(source: Source, rel_path: str, status: str) -> None:
    """Apply a single decision to `source`'s sidecar (load, mutate, save).
    `status="optional"` removes the key entirely (the sidecar only ever
    carries non-default decisions, matching remote_state's convention).
    """
    state = load_source_state(source)
    if status == DECISION_OPTIONAL:
        state.decisions.pop(rel_path, None)
    else:
        state.decisions[rel_path] = status
    save_source_state(source, state)

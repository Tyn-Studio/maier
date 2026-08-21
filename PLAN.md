# Implementation plan

Roles: Luis (CTO) · Claude lead engineer (task briefs, review, integration, commits) · developer agents (implementation).
Process per task: lead writes brief → dev agent implements + tests → lead reviews diff, runs suite, fixes/bounces → commit. Statuses: `[ ]` todo · `[~]` in progress · `[x]` done.

## M1 — Core loop

- [x] **T1 Skeleton** — `pyproject.toml` (uv, src layout, deps: django~=6.0, waitress, pillow, pillow-heif, imagehash, platformdirs, htmx vendored as static file; dev: pytest, pytest-django, ruff), `src/culler/{settings,urls,cli}.py`, `core` app with models + initial migration, folder bootstrap (`.culler/` dir, WAL, auto-migrate), waitress serve + `/healthz`, base template + `culler.css` shell, pytest config. CLI: `culler open PATH [--browser] [--port N]`, `culler status PATH`.
- [ ] **T2 Move engine** — `core/moves.py` per interface below + unit tests (mirrored substructure, unflag restore, collision suffix, Live Photo companion param, DB row update).
- [ ] **T3 Indexing Phase A** — `core/scan.py` + `core/metadata.py`: tree walk (skip `.culler/`), extension filter, status-from-location, provenance, (path,size,mtime) diff, capture-date fallback chain (Pillow EXIF → filename → mtime; exiftool used when detected on PATH), `missing` marking, simple move reconciliation (size+mtime match), progress state object polled by UI, background-thread runner. Unit tests with generated fixtures.
- [ ] **T4 Previews** — `core/previews.py`: content-keyed cache under `.culler/previews/`, on-demand generation (2048px, q82, EXIF orientation), HEIC via pillow-heif, placeholder for RAW/videos in M1. View `preview/<photo_id>` with far-future cache headers.
- [ ] **T5 Web UI** — urls/views/templates/static: timeline grid (day headers, infinite scroll via htmx sentinel, filter bar: status/provenance/date-range, status badges), review view (large preview, filmstrip ±10, metadata sidebar, auto-advance), status endpoints calling `moves.apply_status`, inline keyboard `<script>` (P/X/U/arrows/Space/Esc/I/?), dark theme CSS.
- [ ] **T6 Integration** — fixture-folder builder (tiny JPEGs with EXIF dates via Pillow), end-to-end tests: index → DB matches filesystem; cull via views → files physically moved; re-open → state persists; `.culler/` deleted → state rebuilt from locations.

## M2 — Full indexing

- [ ] **T7** SHA-256 background queue + exact-dupe grouping (×N badge, group cull, auto-reject policy)
- [ ] **T8** pHash + `DuplicatePair` + side-by-side dupes review screen
- [ ] **T9** Live Photo pairing + video support (range-request streaming view, `<video>` cards)
- [ ] **T10** RAW embedded previews (exiftool), hash-confirmed move reconciliation, missing-file UX

## M3 — Desktop feel

- [ ] **T11** pywebview window mode (default), native folder picker, recent-folders home (global config)
- [ ] **T12** exiftool detect/auto-download (pinned, checksum-verified)
- [ ] **T13** Polish: indexing banner, summary screen, shortcut overlay, low-trust date glyphs, empty states

## M4 — Distribution

- [ ] **T14** PyPI packaging + publish workflow; PyInstaller specs (macOS/Windows/Linux) + GitHub Actions release matrix + bundle smoke test; README install docs. Resolve open questions: final name, license.

## Interfaces (agents code against these — deviations must be flagged)

```python
# core/models.py
class Photo(models.Model):
    relative_path: str   # unique, POSIX-style, current location incl. status prefix
    status: str          # "optional" | "selected" | "rejected"  (cache of location)
    provenance: str      # first non-status path segment, "" for root files
    file_size: int; file_mtime: float
    sha256: str | None; phash: str | None
    captured_at: datetime; captured_at_source: str  # "exif" | "filename" | "file_mtime"
    media_type: str      # "image" | "video"
    live_photo_video_path: str | None
    missing: bool; status_changed_at: datetime | None; indexed_at: datetime

# core/moves.py
def dest_for(photo: Photo, new_status: str) -> PurePosixPath: ...
def apply_status(folder: Path, photo: Photo, new_status: str) -> Photo:
    """Atomic os.rename (collision -> ' (n)' suffix), moves live-photo companion,
    updates relative_path/status/status_changed_at, saves. Never overwrites/deletes."""

# core/scan.py
@dataclass
class ScanProgress: total: int; done: int; errors: list[str]; finished: bool
def scan(folder: Path, progress: ScanProgress) -> None: ...        # idempotent, upserts
def start_background_scan(folder: Path) -> ScanProgress: ...

# core/metadata.py
def capture_datetime(path: Path) -> tuple[datetime, str]: ...      # value + source flag

# core/previews.py
def preview_path(folder: Path, photo: Photo) -> Path: ...          # generates if absent

# URL names (templates depend on these)
# "grid" (GET, params: status, provenance, page, from, to), "review" <int:pk>,
# "set-status" <int:pk> (POST, param status), "preview" <int:pk>, "healthz", "home"
```

## Decisions log

- 2026-08-21: repo created; commits authored by Luis (global git identity); Python pinned 3.14 via uv; exiftool absent on dev machine → M1 relies on Pillow path (per SPEC §12).
- 2026-08-21: T1 reviewed & accepted. Deviation kept: `whitenoise` dep added (serves app static dirs directly via `WHITENOISE_USE_FINDERS`, no collectstatic for users). `*.md` excluded from ruff (0.16 formats fenced code blocks in markdown, mangled PLAN.md's interface sketch). Tests bootstrap `CULLER_FOLDER` via `-p _bootstrap` plugin since settings read the env var at import time.

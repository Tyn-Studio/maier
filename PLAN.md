# Implementation plan

Roles: Luis (CTO) · Claude lead engineer (task briefs, review, integration, commits) · developer agents (implementation).
Process per task: lead writes brief → dev agent implements + tests → lead reviews diff, runs suite, fixes/bounces → commit. Statuses: `[ ]` todo · `[~]` in progress · `[x]` done.

## M1 — Core loop

- [x] **T1 Skeleton** — `pyproject.toml` (uv, src layout, deps: django~=6.0, waitress, pillow, pillow-heif, imagehash, platformdirs, htmx vendored as static file; dev: pytest, pytest-django, ruff), `src/culler/{settings,urls,cli}.py`, `core` app with models + initial migration, folder bootstrap (`.culler/` dir, WAL, auto-migrate), waitress serve + `/healthz`, base template + `culler.css` shell, pytest config. CLI: `culler open PATH [--browser] [--port N]`, `culler status PATH`.
- [x] **T2 Move engine** — `core/moves.py` per interface below + unit tests (mirrored substructure, unflag restore, collision suffix, Live Photo companion param, DB row update).
- [x] **T3 Indexing Phase A** — `core/scan.py` + `core/metadata.py`: tree walk (skip `.culler/`), extension filter, status-from-location, provenance, (path,size,mtime) diff, capture-date fallback chain (Pillow EXIF → filename → mtime; exiftool used when detected on PATH), `missing` marking, simple move reconciliation (size+mtime match), progress state object polled by UI, background-thread runner. Unit tests with generated fixtures.
- [x] **T4 Previews** — `core/previews.py`: content-keyed cache under `.culler/previews/`, on-demand generation (2048px, q82, EXIF orientation), HEIC via pillow-heif, placeholder for RAW/videos in M1. View `preview/<photo_id>` with far-future cache headers.
- [x] **T5 Web UI** — urls/views/templates/static: timeline grid (day headers, infinite scroll via htmx sentinel, filter bar: status/provenance/date-range, status badges), review view (large preview, filmstrip ±10, metadata sidebar, auto-advance), status endpoints calling `moves.apply_status`, inline keyboard `<script>` (P/X/U/arrows/Space/Esc/I/?), dark theme CSS.
- [x] **T6 Integration** — fixture-folder builder (tiny JPEGs with EXIF dates via Pillow), end-to-end tests: index → DB matches filesystem; cull via views → files physically moved; re-open → state persists; `.culler/` deleted → state rebuilt from locations.

## M2 — Full indexing

- [x] **T7** SHA-256 background queue + exact-dupe grouping (×N badge, group cull, auto-reject policy)
- [x] **T8** pHash + `DuplicatePair` + side-by-side dupes review screen
- [x] **T9** Live Photo pairing + video support (range-request streaming view, `<video>` cards)
- [x] **T10** RAW embedded previews (exiftool), hash-confirmed move reconciliation, missing-file UX

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
- 2026-08-21: T2/T3/T4 reviewed & accepted. Lead fixed a T3 reconciliation bug (a re-linked path candidate wasn't consumed, letting a second vanished row with equal size+mtime delete the re-linked row; regression test added). M1 preview cache keys on sha1(first 64KiB)+size until Phase B populates sha256. Known ruff 0.16.4 formatter bug: inline `except (A, B, C):` gets rewritten to invalid syntax — use a named tuple constant (see previews.py).
- 2026-08-21: T5 reviewed & accepted with two lead fixes: filter bar `hx-include` scoped to its own inputs (was `[name]`, dragging every cell's hidden input into filter requests), and CSRF enabled (`CsrfViewMiddleware` + `hx-headers` on `<body>`) since set-status moves files on POST — verified 403 without token. HX-Request → partial applies to all htmx grid requests, not just page>1 (filter swaps need it). Video cards show the JPEG placeholder until T9 streaming.
- 2026-08-21: **M1 accepted.** T6 green (84 tests). Acceptance run on a scratch fixture: boot → index → review → P/X culling over HTTP → mirrored moves verified on disk → unflag restored originals; CSRF-less POST rejected 403. Browser-extension UI pass not possible this session (extension disconnected) — keyboard JS verified by review; revisit in M3 polish. M2 runs sequentially (T7→T10): every task after T7 touches views/urls/templates, so parallel agents would collide.
- 2026-08-21: T7 reviewed & accepted. Exact-dupe groups derived by query, no new model. Group cull auto-rejects redundant copies on ANY status action incl. undecide (copies never linger undecided; unflag never auto-restores them) — §17.3 policy kept as specced, revisit with real data. Phase B kicks off automatically at the end of every Phase A scan.
- 2026-08-21: T8 reviewed & accepted. pHashing previews has the intended side effect of pre-generating all previews in the background (SPEC §6 B1 sweep). Defer = pk-ordered skip with wraparound, no DB write. Known gap: keep-left/right does two sequential moves, not atomic (retry-safe). Dupes zoom (SPEC "zoomable") deferred to T13 polish.
- 2026-08-21: T9 reviewed & accepted. Live Photo pairing is fallback-only (same dir + stem + ±1s; exiftool ContentIdentifier arrives with T12) and self-heals dangling pointers. Streaming honors single-range requests without loading files in memory. Lead fix in moves.py: culling a Live Photo now updates the companion's own Photo row too (was stale until next scan, transiently reappearing in grid; regression test added).
- 2026-08-21: T10 reviewed & accepted; **M2 complete** (186 tests). Reconciliation is hash-confirmed when sha256 exists (per-path candidate consumption disambiguates simultaneous moves); RAW previews extract via exiftool when detected (PATH or data dir), tested against fake-exiftool scripts; missing files hidden by default, browsable via `?show=missing` with actions disabled. Known pre-existing artifact for T13: scan()'s auto Phase B thread can log a harmless unhandled-thread warning in isolated test runs at teardown.

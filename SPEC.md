# Culler — Specification

Working name: **Culler** (final name TBD — see Open Questions; note an existing commercial app is called "PhotoCuller", avoid that name).

## 1. Purpose

A local-first, folder-centric photo culling app where **the folder structure is the state**. The user opens a working folder of photos (typically a consolidation of exports from multiple sources: Apple Photos accounts, Lightroom, camera cards). The app indexes it, presents everything in one timeline sorted by capture date, and the user culls with keyboard shortcuts. Culling actions are **file moves**:

- **Select** → file moves to `{folder}/selected/…`
- **Reject** → file moves to `{folder}/rejected/…`
- **Undecided/optional** → file stays in (or moves back to) the folder root structure

There is no export step and no data duplication: `selected/` always contains exactly the current selection, readable in any file browser without the app. The app's own data (`.culler/`) is a rebuildable cache — deleting it loses nothing about which photos are selected.

Distributed as a normal desktop app: technical users install with one command; non-technical users get a double-clickable app.

## 2. Core workflow

1. **Consolidate** (user, outside the app) — drop each source export as a subfolder of one working folder: `apple-luis/`, `apple-maria/`, `lightroom/`. The top-level subfolder name is the photo's **provenance**, filterable in the UI.
2. **Open** — launch the app → recent-folders list or native folder picker.
3. **Index** — fast progressive scan (§6) over the root and the `selected/` / `rejected/` status folders: the date-sorted grid appears and is cullable within seconds; heavy work continues in the background. Re-opening a known folder is near-instant.
4. **Cull** — arrow keys through the timeline, P/X/U mark photos; each keystroke is an atomic file move. Review near-duplicates side by side.
5. **Done** — `selected/` is the deliverable. Cull again next month: newly selected photos join it. The root progressively empties down to the genuinely undecided.

## 3. Status = location

| Location | Status in UI |
|---|---|
| `{folder}/…` (anywhere except the two status folders and `.culler/`) | `optional` (undecided) |
| `{folder}/selected/…` | `selected` |
| `{folder}/rejected/…` | `rejected` |

Rules:

- **Moves mirror the source substructure**: selecting `apple-luis/IMG_001.jpg` moves it to `selected/apple-luis/IMG_001.jpg`. No filename collisions between sources; provenance survives the move; unflagging strips the status prefix and restores the original location.
- Moves are `os.rename` on the same volume — atomic and instant. If a destination path unexpectedly exists (e.g. user manually placed a different file there), the move appends a numeric suffix rather than overwriting; overwriting never happens.
- **Live Photos**: the paired `.mov` moves together with its image, always.
- **The filesystem is the source of truth.** The DB caches status for query speed but is reconciled on every scan; files moved externally (Finder) are simply picked up — culling outside the app is supported behavior, not corruption.
- Reserved names: top-level `selected/` and `rejected/` are status folders. If the working folder already contains them at first open, their contents are indexed with the corresponding status (that's a feature: pre-sorted folders import cleanly).

## 4. Non-goals (v1)

- No photo editing of any kind.
- No cloud *sync* and no multi-user support. One person, one machine, one folder at a time. (One-way iCloud *import* is in scope as of v2 — see §18. Write-back to any cloud account remains a non-goal.)
- No direct reading of Apple Photos `.photoslibrary` bundles or Lightroom catalogs — users export originals to folders first (documented in README), or pull from iCloud per §18.
- No AI scene detection / face recognition.
- The app never modifies file *contents*, never deletes files, and only ever moves them between the working folder's own subtrees.

## 5. Tech stack (locked)

| Concern | Choice |
|---|---|
| Language | Python ≥ 3.14 |
| Web framework | Django 6.x |
| Interactivity | HTMX only. No JS frameworks, no build step, no npm. Vanilla JS permitted only in small inline `<script>` blocks (e.g. keyboard shortcuts) |
| Database | SQLite (cache role), one DB per working folder |
| Embedded server | Waitress |
| Image handling | Pillow + pillow-heif |
| Near-duplicate hashing | `imagehash` (pHash) |
| Metadata extraction | `exiftool` (bundled/auto-downloaded, see §12) |
| Desktop window | pywebview |
| Global config dir | `platformdirs` |
| Packaging (lib/CLI) | `pyproject.toml`, published to PyPI, installable via `uv tool install` / `uvx` / `pipx` |
| Packaging (desktop) | PyInstaller, built per-platform via GitHub Actions |

CSS: a single hand-written stylesheet. Dark UI.

## 6. Indexing (progressive, two phases)

Triggered on folder open and on demand (rescan button). Covers the whole working folder; skips `.culler/`. Extension filter: images `.jpg .jpeg .png .heic .heif .tif .tiff .dng .cr2 .cr3 .nef .arw .raf .orf .rw2`, videos `.mov .mp4 .m4v .avi`.

**Phase A — fast index (foreground, progressive).** Walk the tree; derive status from location (§3) and provenance from the path. For new/changed files, read capture datetime + dimensions + orientation via exiftool (batched, `-stay_open`). The grid renders immediately, photos slot in as indexed, banner shows "indexing 12,400 / 20,000"; culling is allowed from the first second. Target: a 20–50k-photo year fully date-indexed in minutes; re-opens are near-instant via the (path, size, mtime) cache.

**Move reconciliation**: a file that disappeared from one indexed path while an identical (size, mtime — hash-confirmed when available) file appeared at another is re-linked to its existing DB row, keeping its preview, capture date, and dupe history. Genuinely missing files are marked `missing` (hidden by default, state retained).

**Phase B — heavy work (background queue, interruptible, resumes on next open):**
1. **Previews**: max 2048 px long-edge JPEG (quality 82, orientation baked in) into `.culler/previews/`, keyed by content so they survive moves. Also generated **on demand** with priority when the UI requests one that isn't ready — the viewport always beats the background sweep. HEIC via pillow-heif; RAW via exiftool embedded-preview extraction (`-b -JpgFromRaw` / `-PreviewImage`) with placeholder fallback; videos use `<video preload="metadata">` as their own thumbnail.
2. **SHA-256** per file → exact-dupe grouping (§8) and robust move reconciliation.
3. **pHash** of the preview (images only) → near-dupe scan against photos within ±8 s of `captured_at`; Hamming ≤ 8 creates a `DuplicatePair`. (Time-windowed: O(burst), not O(folder).)
4. **Live Photo pairing**: a `.mov` whose QuickTime `ContentIdentifier` matches an image's (or same basename + capture time within 1 s as fallback) is attached to the image and hidden as a standalone item.

Indexing is idempotent and crash-safe: every step is a DB upsert; errors accumulate in a visible per-run error list and never abort the run.

## 7. Data model (cache role)

Per-folder SQLite DB at `{folder}/.culler/culler.sqlite3`. Everything here is derivable from the filesystem + file contents except `DuplicatePair.resolved` — the DB is a cache plus a small amount of review bookkeeping.

### Photo

| Field | Notes |
|---|---|
| `relative_path` | unique; current path under the working folder root (includes `selected/`/`rejected/` prefix when applicable) |
| `status` | derived from location on every scan/move; cached for queries |
| `provenance` | first non-status path segment ("apple-luis", …); filterable |
| `file_size`, `file_mtime` | scan cache — unchanged (path, size, mtime) skips re-processing |
| `sha256` | nullable until computed (Phase B) |
| `phash` | nullable; images only |
| `captured_at`, `captured_at_source` | fallback chain in §9; source enum `exif` / `filename` / `file_mtime`, low-trust flagged in UI |
| `media_type` | `image` / `video` |
| `live_photo_video_path` | nullable; paired `.mov`, moves with the image |
| `missing` | bool; not found at last scan |
| `status_changed_at`, `indexed_at` | |

### DuplicatePair

`photo_a`, `photo_b`, `hamming_distance`, `resolved` (bool). Exact duplicates (same sha256) are handled as groups, not pairs — §8.

## 8. Duplicate handling

- **Exact (same sha256 — e.g. the same shot present in two source exports)**: auto-grouped. The timeline shows one representative with a ×2 badge. A cull action moves the representative accordingly; the redundant copies are auto-moved to `rejected/` so they don't linger as undecided. (Policy open question §17.)
- **Near (DuplicatePair)**: dedicated review screen, pairs side by side (zoomable) with distance + metadata. One-key actions: keep left / keep right ("keep" selects one, rejects the other — both are file moves), keep both, defer. Nav badge shows unresolved count.

## 9. Capture date fallback chain

1. EXIF `DateTimeOriginal` / QuickTime `CreationDate` (timezone if present; else local) → flag `exif`.
2. Parseable filename timestamp (`IMG_20250614_183012`, `2025-06-14 18.30.12`) → flag `filename`.
3. File modification time → flag `file_mtime`.

Low-trust dates (2, 3) get a warning glyph and a filter.

## 10. UI specification

Server-rendered Django templates + HTMX partial swaps. Screens:

### Home
Recent folders (name, counts by status, last opened) + "Open folder" (native picker via pywebview; path input in browser mode).

### Timeline grid
- Thumbnails ordered by `captured_at` ascending across **all** statuses, day headers ("Sat 14 Jun 2025").
- Infinite scroll: sentinel `div` with `hx-get` appends the next page (200/page).
- Filter bar (HTMX swaps grid): status, provenance, date range, media type, low-confidence dates, unresolved dupes.
- Status = colored border/badge: green selected, red rejected, neutral undecided; ×N badge on exact-dupe groups. Indexing banner during Phase A.
- Status keys act on the focused cell (move the file, swap the badge via HTMX); click/Space opens review.

### Review (single photo)
- Large preview — the hot loop; < 100 ms perceived (local previews, far-future cache headers keyed by content).
- Filmstrip of ±10 neighbours; auto-advance to the next photo in the current filter after each status action.
- Collapsible metadata sidebar: capture date + trust flag, provenance, dimensions, size, current path, dupe-group info.
- Videos: `<video controls>` streaming the original via a Django range-request view. Live Photos: badge toggles the paired video.

### Duplicates review — per §8.

### Summary
Counts by status and provenance; total size of `selected/`; recent activity; link to the folder in Finder/Explorer. (Django admin stays enabled as a free debug UI.)

### Keyboard shortcuts
One small inline vanilla-JS `keydown` listener triggering HTMX-wired buttons via `htmx.trigger()`. `?` overlay lists them.

| Key | Action |
|---|---|
| `P` | Select — move to `selected/` |
| `X` | Reject — move to `rejected/` |
| `U` | Undecide — move back to original root location |
| `←` `→` | Previous / next |
| `Space` | Open review (grid) / toggle zoom (review) |
| `L` | Play video / Live Photo |
| `I` | Metadata sidebar |
| `?` | Shortcut overlay |
| `Esc` | Back to grid |

Bindings mirror Lightroom so muscle memory transfers.

## 11. Application shell

### CLI (`culler`)

```
culler                 # launch app: native window, home screen (recent folders / picker)
culler open PATH       # launch directly into a folder
culler --browser       # serve + open system browser instead of the pywebview window
culler status PATH     # headless folder stats: counts by status/provenance
```

`culler` boots Django programmatically (no manage.py for users), points the DB at the opened folder's `.culler/culler.sqlite3`, runs `migrate` automatically (also on every folder open — how folder DBs upgrade across app versions), then serves via Waitress on 127.0.0.1:8347 (falls back to a free port). Binds localhost only. One folder open at a time (v1).

### State locations

- **Per folder** — `{folder}/.culler/`: `culler.sqlite3`, `previews/`, `logs/`. Pure cache + dupe-review bookkeeping: deleting it loses no culling state (that's in the folder structure); the folder is portable across disks/machines (relative paths only).
- **Global** (platformdirs: `~/Library/Application Support/Culler/`, `%LOCALAPPDATA%\Culler\`, `~/.local/share/culler/`): recent-folders list, window geometry, generated `SECRET_KEY`, auto-downloaded exiftool.

## 12. exiftool strategy

exiftool is the only non-Python dependency.

- **Tier 1/2 (pip install)**: detect on PATH; if absent, download a pinned, checksum-verified copy into the global data dir. Offline + no system exiftool → JPEG/HEIC dates still work via Pillow EXIF; RAW previews and video dates degrade gracefully with a visible notice.
- **Tier 3 (bundled app)**: per-platform exiftool binary ships inside the bundle.

## 13. Distribution

### Tier 1 — PyPI
`uvx culler` / `uv tool install culler` / `pipx install culler`. README one-liner: "install uv, run `uvx culler`". Also the dev workflow.

### Tier 2 — native window
Default mode: pywebview (WKWebView / WebView2 / WebKitGTK) with native folder pickers. `--browser` mode falls back to a path text input.

### Tier 3 — double-click apps
- PyInstaller one-dir builds: `Culler.app` (macOS), `Culler-Setup.exe` (Windows, Inno Setup or zipped exe), AppImage/tarball (Linux). Entry point = the window launcher.
- PyInstaller spec declares Django templates, static files, migrations, stylesheet, and the platform exiftool binary as data files. `multiprocessing.freeze_support()` and pywebview hooks included.
- **CI**: GitHub Actions release workflow, matrix `[macos-14, windows-latest, ubuntu-latest]`; on tag push: tests → wheel + sdist → PyPI → PyInstaller bundles → GitHub Release assets.
- **Signing**: v1 ships unsigned; README documents macOS right-click→Open and Windows SmartScreen "More info → Run anyway". Developer-ID signing/notarization is a fast-follow once there are external users — CI slots stubbed.
- CI smoke test launches each built bundle headless, hits `http://127.0.0.1:PORT/healthz`, asserts 200 — catches PyInstaller missing-data-file breakage.

## 14. Performance targets

- Open a known folder of 100k photos: grid interactive < 2 s (index diff only).
- First index of a 50k-photo folder: cullable from the first seconds; Phase A complete in minutes; Phase B fully background.
- Cull action (file move + badge swap) < 50 ms perceived; review-view photo swap < 100 ms.
- SQLite WAL mode; indexes on `captured_at`, `status`, `sha256`, `phash`, `relative_path`.

## 15. Testing

- Unit: status-from-location derivation, move path mapping (incl. collision suffixes, Live Photo companions, unflag restore), capture-date fallback chain, dedup grouping, move reconciliation (fixtures incl. HEIC, DNG, timezone-less EXIF).
- Integration: index a fixture folder end-to-end → assert DB matches filesystem; cull via views → assert files physically moved and status re-derived; move files externally → rescan → state converges; move the whole folder → re-open → intact.
- UI: Django test client on views/partials (HTMX responses are plain HTML).
- CI bundle smoke test per §13.

## 16. Milestones

1. **M1 — Core loop (usable by Luis)**: package skeleton, CLI + browser mode, per-folder DB, Phase A indexing (images, EXIF dates, status-from-location), on-demand previews, timeline grid + review, P/X/U culling as atomic moves with mirrored substructure + unflag restore. *Success: open a real folder, cull it in the app, verify the result in Finder.*
2. **M2 — Full indexing**: Phase B background queue (hashes, exact-dupe groups + auto-reject policy, pHash + near-dupe screen), Live Photos, RAW previews, videos, move reconciliation, missing-file handling, provenance filter.
3. **M3 — Desktop feel**: pywebview window + native pickers as default, recent-folders home, exiftool auto-download, polish (day headers, badges, shortcut overlay, dark theme, indexing banner, summary screen).
4. **M4 — Distribution**: PyPI publish, PyInstaller specs, GitHub Actions release pipeline, smoke tests, README install docs for all three tiers.
5. **M5 — iCloud sources (§18)**: multi-account attach with 2FA, incremental thumbnail-first pulls, select-downloads-original culling, accounts screen. *Success: attach two real accounts, pull, cull remote photos alongside local ones, verify only selected originals land in `selected/{account}/` and nothing changes in either iCloud account.*

## 17. Open questions

1. **Name** — "Culler" is a placeholder. Needs a PyPI-available, trademark-safe name before M4.
2. **License** — MIT/Apache-2.0 if open-sourcing; decide before the repo goes public (M4 at the latest).
3. **Exact-dupe policy** — redundant copies auto-move to `rejected/` when their group is culled (current spec), or stay in place with a badge? Decide during M2 with real data.
4. **"New since last time" tracking** — the old incremental-export requirement dissolved with the move model (`selected/` is always the current selection). If a need emerges to know *which selected photos are new since the last upload/handoff*, add a lightweight "mark sync point" feature (DB timestamp + "added since" filter). Deferred until actually needed.
5. **RAW+JPEG pairs** — v1 treats them as two photos (near-dupe screen surfaces them). Auto-stacking as one unit that moves together is a possible M2+ refinement.
6. **macOS Intel support** — universal2 or Apple-Silicon-only? Decide at M4.

## 18. iCloud sources (v2, decided 2026-08-24)

Multiple Apple accounts can be attached as **read-only import sources**. Their photos appear in the same capture-date timeline as local files and are culled with the same keys.

### Hard rules

1. **Never modify anything in an iCloud account.** No deletes, no album changes, no writes of any kind — the web API is used exclusively to read metadata and download image data. (Extends §4's non-goals: rejecting a photo locally never touches iCloud.)
2. **Originals are downloaded only on selection.** Culling browses thumbnails/medium previews; `P` (select) enqueues download of the original into `selected/{account}/…`. Rejected and undecided remote photos never materialize as local files.
3. Once an original is downloaded it becomes a normal local file: further status changes are ordinary file moves (unflag moves it to `{account}/…` in the root — it is never deleted).

### Access path

- Unofficial iCloud web API via the maintained `pyicloud` package (2.x), wrapped behind `core/icloud.py` so the dependency is swappable. Known constraints accepted: per-account Apple-ID login with interactive 2FA, session tokens that expire periodically (re-auth prompt), breakage risk on Apple-side changes, and no accounts with Advanced Data Protection unless "Access iCloud Data on the Web" is enabled.
- Passwords are used transiently for login and **never persisted**; only pyicloud's session/trust tokens are stored, under the global data dir (`§11`), per account.

### State model

- Remote items get DB rows (`Photo.source = "icloud"`, `account`, `remote_id`, remote capture date/dimensions from API metadata) but **the DB stays a cache**: durable remote state lives in per-account JSON files in the working folder at `{folder}/icloud-state/{account}.json` — portable with the folder, survives `.culler/` deletion. Each records: the incremental sync cursor (last pull watermark), and the per-remote-id decision map (`rejected` / `undecided`; `selected` is derivable from the downloaded file in `selected/{account}/` and is recorded only as a download-completed marker).
- **Pulls are incremental**: first pull optionally bounded by a date range; subsequent pulls fetch only items newer than the cursor. Re-pulls are idempotent (keyed by `remote_id`).
- Thumbnails/medium previews cache under `.culler/previews/` keyed by `remote_id` (cache role, regenerable by re-fetch).
- Cross-account exact duplicates: remote items join §8 grouping once their selected original is downloaded and hashed; pre-download, near-identical remote items are surfaced via capture-time + filename + size heuristics (best effort).

### UI

- **Accounts screen**: list attached accounts (email, session status, last pull, item counts), add account (email + password + 2FA code prompt), re-auth when a session expires, "Pull now" with progress (mirrors the indexing banner pattern).
- Remote photos show a cloud badge in grid/review; selection shows download progress until the original lands.


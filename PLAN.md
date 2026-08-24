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

- [x] **T11** pywebview window mode (default), native folder picker, recent-folders home (global config)
- [x] **T12** exiftool detect/auto-download (pinned, checksum-verified)
- [x] **T13** Polish: indexing banner, summary screen, shortcut overlay, low-trust date glyphs, empty states

## M4 — Distribution

- [~] **T14** *(mechanics done + MIT licensed; blocked on final name only)* PyPI packaging + publish workflow; PyInstaller specs (macOS/Windows/Linux) + GitHub Actions release matrix + bundle smoke test; README install docs. Resolve open questions: final name, license.

## M5 — iCloud sources (SPEC §18)

- [x] **T15 iCloud client** — `core/icloud.py`: thin wrapper over `pyicloud` 2.x (new dep). Session/trust-token store per account under global data dir; `login` (raises `TwoFactorRequired`), `submit_2fa`, `from_session`, `list_assets(since)` iterator, `download(remote_id, version, dest)` for thumb/medium/original. All tests against fakes — no network, no real accounts.
- [x] **T16 Remote model + pull pipeline** — migration: `Photo.source` ("local"/"icloud", default local), `account`, `remote_id`; `core/remote_state.py` (per-account JSON in `{folder}/icloud-state/`, cursor + decisions + downloaded map, atomic writes); `core/pull.py`: `pull_account` = list→upsert remote rows→prefetch medium previews into `.culler/previews/`; scan/phaseb/queries updated to EXCLUDE `source="icloud"` rows from file walks, missing-marking, hashing, pHash, Live-Photo pairing.
- [x] **T17 Remote-aware culling** — set-status branches on source: remote reject/undecide = state-file + DB write (no file move); remote select = enqueue original download → lands at `selected/{account}/…` (collision-suffixed) → row converts to local (`source="local"`, relative_path set, sha256 via Phase B); download queue with progress + failure retry; unflag after download = normal local move to `{account}/…`.
- [x] **T18 Accounts UI** — accounts screen (list, add w/ email+password+2FA forms, re-auth, "Pull now" + progress banner), cloud badge on remote cells/review, download-pending indicator on selected-but-not-yet-downloaded, nav link.
- [x] **T20 HEIC + Live Photo selects** — iCloud HEIC selects convert to full-res JPG/PNG (EXIF preserved); Live Photo selects fetch the video complement. Agent in flight.
- [x] **T21 Disconnect accounts** (Luis, 2026-08-24) — accounts screen "Disconnect": delete session tokens + that account's remote DB rows + cached previews. KEEP `icloud-state/{account}.json` (durable decisions — re-attaching restores rejections) and KEEP all downloaded files in `selected/` (they're ordinary local photos now). Two-step confirm in-page (no JS confirm dialogs). Runs after T20 (both touch icloud.py/views).
- [x] **T23 Review redesign** (Luis, 2026-08-24) — image maximal, filmstrip pinned bottom, metadata = `I`-toggled minimal overlay, NO auto-advance on status change (keyboard-only navigation), adjustable grid cell size (slider + localStorage).
- [x] **T24 Flat selected/** (Luis, 2026-08-24) — `selected/` holds all exports directly, no subfolders; `rejected/` stays mirrored; unflag restores via new `Photo.original_path` with provenance-dir → root fallback on cache loss; iCloud downloads land flat.
- [x] **T22 Sync-tier polish**
- [~] **T25 Export** (Luis, 2026-08-24) — per-folder setting `{folder}/maier-settings.json`: export destination + mode (manual default / automatic). Export = COPY selected/ contents to destination, additive-only (never delete there), skip identical existing (size+mtime); auto mode copies on each select + on iCloud original landing. Optional date-prefix renaming on export. Native picker in window mode, path field in browser.
- [x] **T27 Update notification** (Luis, 2026-08-24) — repo pushed to github.com/Tyn-Studio/maier (private); version single-sourced in `maier/__init__.py`; boot-time background check of GitHub releases/latest (24h cached, fail-silent while repo is private), grid banner with download link. No self-modification.
- [x] **T28 M6 wave 1** — Source model + multi-root scan + per-source sidecars, built alongside the current model (no flip yet).
- [x] **T30 UI consolidation + ship prep** (Luis, 2026-08-24) — one Settings page (accounts merged in, "Connect iCloud" button, native folder picker via pywebview js_api, working-range section), shared app header on all pages but review, slimmed grid filter bar, /accounts → /settings redirect. Latent bug fixed: export-settings save was wiping the working range. CI flake fixed (phaseb thread drain), actions bumped (checkout@v5, setup-uv@v6), v0.1.0 release scoped to macOS.
- [x] **T29 Working date range** (Luis, 2026-08-24) — setup wizard on open (iCloud step first when no account, skippable for local-only; then required date range, presets + custom, applies to all accounts). Metadata sync stays whole-library; thumb downloads + pHash/preview sweeps scoped to the range; grid defaults to it; range changes trigger backlog fetch.
- [ ] **T26 Session persistence + snappiness** (Luis, 2026-08-24) — restore filters/grid size/scroll via localStorage on open; memoize duplicate_counts/non-representative/companion-path sets with invalidation on scan/pull/cull completion (grid paging at 41k). — on-demand "medium" fetch when the review screen opens a remote photo (thumb tier is grid-only quality); consider progress-ordered backlog (newest first) instead of remote_id order.
- [x] **T19 M5 integration** *(code-complete; real-account acceptance pending — needs Luis for 2FA)* — end-to-end tests with a fake client: attach→pull→cull mixed local+remote timeline→only selected originals on disk→state survives `.culler/` deletion→incremental re-pull idempotent; README/§18 docs; acceptance per SPEC §16.5.

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

# --- M5 interfaces ---

# core/icloud.py (T15)
class TwoFactorRequired(Exception): ...   # carries a pending client to submit the code on
class ICloudError(Exception): ...
@dataclass
class RemoteAsset:
    remote_id: str; filename: str; captured_at: datetime  # aware UTC
    size: int; media_type: str  # "image" | "video"
class ICloudClient:
    account: str  # the Apple-ID email
    @classmethod
    def login(cls, email: str, password: str) -> "ICloudClient": ...  # may raise TwoFactorRequired(pending)
    @classmethod
    def from_session(cls, email: str) -> "ICloudClient | None": ...   # stored session or None
    def submit_2fa(self, code: str) -> bool: ...
    def list_assets(self, since: datetime | None) -> Iterator[RemoteAsset]: ...
    def download(self, remote_id: str, version: str, dest: Path) -> None: ...  # "thumb"|"medium"|"original"

# core/remote_state.py (T16) — {folder}/icloud-state/{account-slug}.json, atomic writes
@dataclass
class AccountState:
    account: str; cursor: datetime | None
    decisions: dict[str, str]    # remote_id -> "rejected" | "optional"
    downloaded: dict[str, str]   # remote_id -> relative_path of the downloaded original
def load_state(folder: Path, account: str) -> AccountState: ...
def save_state(folder: Path, state: AccountState) -> None: ...
def list_accounts(folder: Path) -> list[str]: ...

# core/pull.py (T16)
def pull_account(folder: Path, client: ICloudClient, progress: PullProgress) -> None: ...
def start_background_pull(folder: Path, client: ICloudClient) -> PullProgress: ...

# Photo model additions (T16): source ("local"|"icloud"), account (str, ""),
# remote_id (str|None, unique together with account). Remote rows:
# relative_path = f"@icloud/{account}/{remote_id}" sentinel (never a real path).
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
- 2026-08-21: T11+T12 reviewed & accepted. exiftool pinned to 13.59 via SourceForge (exiftool.org's direct URL 404s once superseded; SourceForge keeps versions) — sha256 cross-checked against exiftool.org/checksums.txt by lead; download wired into folder-open (background, non-blocking) and live-verified on this machine (`exiftool -ver` → 13.59). Desktop shell: window mode default, `CULLER_FORCE_NO_WINDOW=1` for headless/CI, recents in global config (`CULLER_CONFIG_DIR` test override), bare `culler --browser` prints usage instead of a placeholder-folder home (deviation from SPEC §10's path-input home — revisit if requested). GUI paths (window/picker/home) not automatable — need a manual pass from Luis. `culler status` no longer records recents (only real opens do).
- 2026-08-21: T13 reviewed & accepted; **M3 complete** (248 tests). Summary screen, low-confidence date filter+glyphs, exiftool capture dates for RAW/video (one process per file — batch via -stay_open if indexing large RAW sets feels slow), dupes zoom, badge corners, three-way empty states. Lead redesigned the scan banner polling: agent's 286-based approach would request-storm in a real browser (`load` re-fires on every self-swap); replaced with recursive load-polling — in-flight responses carry the next `load delay:2s` trigger, idle responses are inert, rescan swaps a live poller back in.
- 2026-08-21: T14 mechanics reviewed & accepted (250 tests). Wheel verified to ship templates/static/migrations. Lead fixed the PyInstaller spec (Django's string-imports need `collect_submodules` — first build died on `culler.settings`) and verified the real macOS bundle end-to-end: builds, boots, serves grid/static/summary, smoke test PASS. Release workflow inert by design (publish `if: false`, draft releases). **Blocked on CTO decisions:** final name ("culler" is taken on PyPI; free: cullkit, lightcull, cullfolder; photo-culler free but collides with commercial "PhotoCuller") and license (MIT vs Apache-2.0). All rename points are greppable via TODO(name)/TODO(license).
- 2026-08-24: **M5 scoped (Luis):** multi-account iCloud import — thumbnails-first culling, originals download ONLY on select into `selected/{account}/`, incremental pulls, strictly one-way/read-only (never modify iCloud), unofficial web API accepted (`pyicloud` 2.6.5, active as of 2026-06). SPEC §18 added; CLAUDE.md hard rules 8–9 added. Remote decisions are durable state in `{folder}/icloud-state/` (NOT `.culler/`) since location-derived status is impossible for undownloaded items.
- 2026-08-24: T15+T16 reviewed & accepted (328 tests). pyicloud 2.6.5's API differs from classic docs (CloudKit photos, `validate_2fa_code` auto-trusts, `download()` returns full bytes in memory — watch RAM on huge originals); session reuse via `password=""` to skip keyring. **Lead rework of pull semantics:** incrementality keys on remote_id, NOT a capture-date cursor — iCloud libraries gain OLD photos later (device syncs/imports) which a date cursor would hide forever; listing is a full metadata enumeration anyway (no server-side filter). Pulls also repair missing previews each run. Slug helper consolidated into remote_state.account_slug (was duplicated with a strip() divergence). Both T15/T16 agents were killed mid-run by API connection drops and resumed from transcripts — worth knowing this works.
- 2026-08-24: T17 reviewed & accepted (357 tests). Culling dispatcher: local→group engine unchanged; remote reject/undecide = durable state write only; remote select = instant status flip + async original download → row converts to local. Provenance for remote rows changed to the account SLUG (stable across download conversion; was raw email in T16). DB-derived download queue, retries on next enqueue; session-expiry surfaces via worker errors, not a blocking check. Lead fix: worker now caches one client per account per run (was constructing a pyicloud session per photo). Known small gap: crash window between row conversion and state save (self-heals via state.downloaded/known-ids checks on next pull).
- 2026-08-24: T18 reviewed & accepted (374 tests). Accounts page renders with zero network calls; passwords request-scoped only (lead-verified); pending-2FA clients held in a module dict (single-user app); account-pull takes the email as a POST field, not a path segment. "Remove account" deliberately deferred (product question: what happens to already-pulled rows). Django template gotcha discovered: multi-line `{# #}` comments render literally — use `{% comment %}`.
- 2026-08-24: T19 reviewed & accepted; **M5 code-complete** (380 tests, 20+ consecutive flake-free runs). Integration proves the §18 hard rules end-to-end with filesystem-diff guards: reject/undecide create nothing on disk but the state file; only selected originals land under `selected/{slug}/`; state survives cache deletion; re-pull incremental + idempotent; expired sessions degrade to a re-auth prompt. Remaining for M5 acceptance (SPEC §16.5): attach two REAL accounts with Luis at the keyboard (2FA), pull, cull, verify in Finder + both iCloud accounts untouched. Product name still undecided (blocks first release, not development).
- 2026-08-24: **Name decided: Maier** (Luis; after photographer Vivian Maier — her posthumously-culled archive is the product's story). Full clean rename: package/CLI `maier`, env vars `MAIER_*`, cache `.maier/`, DB `maier.sqlite3`, app "Maier". PyPI `maier` verified free; no competing "Maier" photo software found in a web search (formal trademark clearance still open before wide distribution). Old `.culler/` caches orphaned (rebuildable); global dir change requires one iCloud re-auth.
- 2026-08-24: **Incident + hardening.** Luis hit a 500 on /accounts while the rename agent was rewriting the tree under his running server (template dir moved mid-serve) — process failure: lead must explicitly say "don't run the app" during repo-wide refactors. Hardening exposed a REAL pre-existing bug: PyInstaller bundles were missing fido2's package data (pyicloud dep, since M5) — every M5-era bundle crashed at boot. Fixed via collect_data_files in maier.spec. smoke_test.py upgraded from healthz-only to all pages incl. /accounts with a seeded account-state file.
- 2026-08-24: First real-account contact (Luis, natera@hey.com): session + CloudKit listing work live. UX trap fixed: authenticating didn't start a pull → "no photos" confusion; login/2FA success now auto-starts the first background pull (tested contract). Known real-data quirk: some iCloud assets report epoch-adjacent capture dates (e.g. 1969-12-31) which currently carry the trusted "exif" source flag and sort at timeline start — candidate fix: treat pre-1990 API dates as low-trust (matches metadata.py's plausibility window).
- 2026-08-24: **Pull restructured for real-library scale** (lead, live-debugged against Luis's account: ~30 assets/s enumeration, 10k+ items). Old design materialized the full listing before writing anything → "Pulling 0 / 0" for many minutes, then serial preview downloads for hours before the grid filled. Now: phase 1 streams metadata (rows upsert during enumeration, grid fills progressively, banner shows "Discovering library… N found"); phase 2 fetches the preview backlog ("Fetching previews N / M") — the old repair pass is now the sole preview path, so interrupted pulls resume cleanly. Remaining perf candidates for a follow-up: parallel preview fetches, on-demand preview fetch in review view, asset-cache memory on very large libraries.
- 2026-08-24: Live-testing round 2 (41,811 remote rows — streaming pull confirmed at scale). Three fixes: (1) **placeholder previews were served with immutable 1-year caching** — browsers pinned gray thumbnails forever even after real previews landed; placeholders now `no-store` (regression-tested). (2) Remote videos rendered a `<video src=/stream/…>` that 404'd (original not local) — review now shows the preview still + "plays after select" note. (3) Preview backlog fetch parallelized (4 workers; serial was hours at this scale) and waitress bumped to 12 threads (grid fires dozens of concurrent preview requests; queue-depth warnings). Still open: on-demand preview fetch in review, asset-cache memory footprint at 40k+.
- 2026-08-24: Round 3. Pull now runs previews CONCURRENTLY with discovery (backlog first — a re-pull shows thumbnails immediately) with honest counters (scanned vs previews done/total; re-pull previously showed "0 photos found" while skipping 41k known items). Real-library probe: 82% HEIC; **video "medium" rendition is MP4** — previews saved as .jpg were broken video bytes; videos now fetch the "medium_image" JPEG poster (never falls back to original), plus a cache-repair pass that discards non-JPEG video previews. **T20 scoped (Luis requirement):** iCloud HEIC selects must save a full-res JPG (PNG if alpha) with EXIF preserved — HEIC originals expose no full-res JPEG rendition (medium is only 1536px), so convert locally via pillow-heif; remote Live Photos (`PhotoAsset.is_live_photo` exists) should fetch their video complement on select. Local files are NEVER converted (hard rule 2 — moves only).
- 2026-08-24: T20+T21 reviewed & accepted (413 tests). T20: HEIC selects convert to full-res JPEG/PNG (EXIF kept, orientation tag preserved not baked, staged atomically under `.maier/tmp/`, HEIC fallback on conversion failure); Live Photo video complement via pyicloud's `original_video` rendition (`is_live_photo` is free at listing time — reads an already-fetched CloudKit field); known gap: no auto-retry for a failed video fetch. T21: disconnect = forget session + delete remote rows (source-filtered — converted local rows survive) + purge preview cache; keeps icloud-state (re-attach restores rejections) and selected/; refuses while that account's pull is in flight (best-effort guard, not a lock); two-step in-page confirm. Sync tier: bulk previews now "thumb" (~60KB) at 8 workers — full 41k grid browsable in well under an hour; T22 will fetch "medium" on demand for review quality.
- 2026-08-24 (evening): T22/T25/T27/T28 reviewed & accepted (558 tests). Repo live + PUBLIC at github.com/Tyn-Studio/maier (visibility flipped at Luis's request so the update feed works; secrets sweep clean; note natera@hey.com appears in this log, now public). Update flow: bump `maier/__init__.__version__` → tag `v*` → push tag → CI publishes a VISIBLE release (draft:false now — tagging is the ship act) → running apps show the banner within 24h. M6 wave 1 landed alongside the old model: Source registry, `@src/{pk}/` sentinel rows, per-root-scoped reconciliation, sidecar decisions, `absolute_path_for` as the single path resolver. Known drift case: sidecar keys aren't rewritten on in-source renames (flagged for culling integration). Remaining M6 waves: copy/un-copy culling engine, onboarding + source-management UI, adoption, the flip.
- 2026-08-24: T29 reviewed & accepted (588 tests). Working date range: setup wizard gates /grid only (accounts step skippable for local-only, then required range w/ presets; "Everything" = explicit 1970-01-01 sentinel, distinct from unset); metadata sync stays whole-library, thumb backlog + new-asset previews + pHash sweep scoped to the range; sha256 deliberately unscoped; range change kicks background pulls for live sessions; grid defaults to the range with explicit params (even empty) winning. Smoke now seeds a range and checks /setup. Known follow-ups: review() ordering doesn't inherit the grid's default range; custom range saved blank re-gates.
- 2026-08-24: T23+T24 reviewed & accepted (435 tests). T23: review = maximal image, filmstrip pinned bottom, slim top bar, `I`-toggled metadata overlay, NO auto-advance (status pill swaps in place; SPEC §10 amended — auto-advance was specced behavior, CTO reversed it); grid cell-size slider (localStorage). T24: `selected/` FLAT with `Photo.original_path` (migration 0004) driving unflag restore (cache-loss fallback: provenance dir → root); `rejected/` stays mirrored; reject-from-selected mirrors the ORIGIN, not the flat path; iCloud downloads land flat; `flatten_selected()` converges legacy mirrored trees at the start of every scan (records origins, rewrites companion + state.downloaded paths, prunes empty dirs). SPEC §3/§10/§18 + CLAUDE.md reconciled by lead. Known degradations documented: external move into mirrored selected/ between scans re-links without original_path; flat select's origin unrecoverable after cache loss.
- 2026-08-21: **License decided: MIT** (Luis) — LICENSE file added, pyproject license table set, README updated. **Name deferred** (Luis): working name "culler" stays; PyPI publish remains gated until the name lands. Remaining before first release: pick name → grep TODO(name) → tag v0.1.0 → iterate release.yml on the real run.

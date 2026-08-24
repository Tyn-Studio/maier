# CLAUDE.md — project conventions

Read SPEC.md before implementing anything. PLAN.md holds the task breakdown and status.

## Commands

- `uv sync` — install deps (uv manages Python 3.14)
- `uv run pytest` — run tests (always run before declaring a task done)
- `uv run maier open <folder> --browser` — run the app against a photo folder
- `uv run django-admin ...` — only via `DJANGO_SETTINGS_MODULE=maier.settings MAIER_FOLDER=<path>`

## Architecture

- `src/maier/` — Django project package. `settings.py` reads the working folder from the `MAIER_FOLDER` env var / runtime bootstrap; DB is `{folder}/.maier/maier.sqlite3`.
- `src/maier/cli.py` — the only entry point users touch (`maier`). Boots Django programmatically, auto-migrates, serves via waitress on 127.0.0.1.
- `src/maier/core/` — the single Django app: models, scanner, moves, metadata, previews, views, templates, static.
- `tests/` — pytest + pytest-django; fixtures are generated tiny images, never committed binaries.

## Hard rules (from SPEC)

1. **Filesystem is the source of truth.** Status is derived from location (root = optional, `selected/` = selected, `rejected/` = rejected). The DB is a cache; every code path must survive `.maier/` being deleted.
2. **Never modify file contents, never delete files, never overwrite** (collisions get ` (1)` style numeric suffixes). Only moves within the working folder are allowed, implemented as same-volume `os.rename`.
3. `selected/` is FLAT (T24: exports land directly as `selected/{filename}`, origins recorded in `Photo.original_path` for unflag restore); `rejected/` mirrors the source substructure. Live Photo `.mov` companions move with their image.
4. **No JS frameworks, no build step, no npm.** HTMX for interactivity; vanilla JS only in small inline `<script>` blocks. One hand-written CSS file (`core/static/maier.css`).
5. Views return full pages or HTMX partials (plain HTML fragments). Keep views thin; logic lives in `core/` modules with unit tests.
6. exiftool may be absent: metadata extraction must degrade gracefully to Pillow (JPEG/HEIC/PNG/TIFF) per SPEC §12. Never make exiftool a hard dependency.
7. SQLite in WAL mode; long work (scan, previews) runs in background threads — keep DB writes short and idempotent (upserts).
8. **iCloud accounts are strictly read-only** (SPEC §18): the web API is only ever used to read metadata and download image data — never delete, never modify, never write. Passwords are used transiently for login and never persisted (session tokens only, global data dir).
9. Remote (iCloud) photos have no local file until selected: their durable state lives in `{folder}/icloud-state/{account}.json` (NOT in `.maier/` — it must survive cache deletion and travel with the folder). Originals download only into flat `selected/` (T24); once downloaded they are ordinary local files.

## Style

- Python ≥ 3.14, ruff for lint/format (`uv run ruff check`, `uv run ruff format`).
- Type hints on public functions. No docstring boilerplate — comment only non-obvious constraints.
- Commit style: short imperative subject lines ("Add move engine"), no trailers.

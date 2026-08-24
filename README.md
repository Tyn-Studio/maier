# Maier

Local-first photo culling app where **the folder structure is the state**.

Point it at a working folder (e.g. exports from two Apple Photos accounts
and a Lightroom catalog, dropped in as subfolders). It indexes everything
into one timeline sorted by capture date, and you cull with keyboard
shortcuts. Every cull action is an atomic file move — no export step, no
duplication, no lock-in:

- Select (`P`) moves the file into `selected/`
- Reject (`X`) moves it into `rejected/`
- Undecide (`U`) moves it back to where it came from

`selected/` always *is* your current selection — readable in any file
browser, without the app. The app's own cache (`.maier/`) is fully
rebuildable from the filesystem; deleting it loses no culling state.

Built with Python / Django 6 / HTMX. No JS frameworks, no build step, no npm.

## Install

Requires [uv](https://docs.astral.sh/uv/) (installs Python 3.14 for you if
you don't have it).

```sh
# Not published to PyPI yet -- these commands are aspirational until then.
# Use the "Development" section below for now.
uvx maier                        # run without installing
uv tool install maier            # install a `maier` command permanently
pipx install maier                # or via pipx, if you prefer
```

### Double-click app (no Python required)

Prebuilt, unsigned desktop bundles (macOS, Windows, Linux) will be attached
to [GitHub Releases](../../releases) once M4 packaging ships — see
`.github/workflows/release.yml`. Not available yet; this section is a
placeholder for the download links.

Because these builds are unsigned (SPEC §13 — signing is a fast-follow):

- **macOS**: Gatekeeper will refuse a plain double-click ("Maier is
  damaged" / "cannot be opened"). Right-click the app → **Open** → **Open**
  in the confirmation dialog. Only needs doing once.
- **Windows**: SmartScreen will show "Windows protected your PC". Click
  **More info** → **Run anyway**.

## Quickstart

```sh
uv run maier open ~/Photos/2025-inbox
```

Opens a native window on the given folder (falls back to your system
browser with `--browser`, or automatically if no windowing backend is
available). First launch on a folder runs Phase A indexing in the
background — the grid is cullable within seconds even on large folders;
thumbnails and duplicate detection fill in progressively.

### Consolidating sources first

Maier doesn't read Apple Photos `.photoslibrary` bundles or Lightroom
catalogs directly (not a goal — see SPEC §4). Export originals from each
source into subfolders of one working folder before opening it:

```
2025-inbox/
  apple-luis/       <- exported from Luis's Photos library
  apple-maria/      <- exported from Maria's Photos library
  lightroom/        <- exported from a Lightroom catalog
```

The top-level subfolder name becomes each photo's **provenance**, filterable
in the UI. Moves mirror this substructure: selecting `apple-luis/IMG_1.jpg`
moves it to `selected/apple-luis/IMG_1.jpg`, so provenance and directory
layout survive culling.

## iCloud accounts

Maier can also pull directly from one or more iCloud (Apple ID) accounts
and cull those photos in the same timeline as your local files — no export
step needed for that source. Open the **Accounts** screen, add an account
(email + password + 2FA code if prompted), then **Pull now**. Remote photos
show up alongside local ones, sorted by capture date, with a cloud badge.

A few things worth knowing before you connect an account:

- **Thumbnail-first, originals only on select.** Pulling an account fetches
  metadata and a medium-quality preview for every photo — enough to browse
  and cull with the usual keys. The full-resolution original is only
  downloaded when you select (`P`) a photo, landing at
  `selected/<account>/…`. Rejected and undecided remote photos never touch
  your disk.
- **Nothing in iCloud is ever modified.** Maier only ever reads metadata
  and downloads image data from your account — no deletes, no album
  changes, no writes of any kind. Rejecting a photo locally never touches
  iCloud; it only records a local decision.
- **Your password is never stored.** It's used once, in memory, to sign in;
  only the resulting session/trust token is kept (in your user data
  directory, alongside the exiftool download), and it expires after roughly
  a month. When that happens you'll see a re-authenticate prompt on the
  Accounts screen — no photos are lost, you just sign in again.
- **Advanced Data Protection.** If an account has ADP enabled, Apple's web
  API can't see its library unless "Access iCloud Data on the Web" is
  turned on for that account (Settings → Apple ID → iCloud → Advanced Data
  Protection).
- **Where state lives.** Per-account pull progress and cull decisions for
  not-yet-downloaded photos live in `icloud-state/` at the top of your
  working folder — deliberately *not* inside `.maier/`, so they survive
  cache deletion and travel with the folder. Once a photo is downloaded, it
  becomes an ordinary local file and behaves exactly like the rest of your
  library.
- **Unofficial API.** This feature is built on Apple's undocumented iCloud
  web API (via the `pyicloud` project), the same approach used by tools
  like `icloudpd`. It isn't officially supported by Apple and can break if
  Apple changes that API — treat it as best-effort, and keep your own
  Photos app / iCloud.com as the source of truth for anything you can't
  afford to lose track of.

## Keyboard shortcuts

| Key | Action |
|---|---|
| `P` | Select — move to `selected/` |
| `X` | Reject — move to `rejected/` |
| `U` | Undecide — move back to original location |
| `←` `→` | Previous / next photo |
| `Space` | Open review (grid) / toggle zoom (review) |
| `L` | Play video / Live Photo |
| `I` | Toggle metadata sidebar |
| `?` | Shortcut overlay |
| `Esc` | Back to grid |

Duplicates review screen:

| Key | Action |
|---|---|
| `1` | Keep left, reject right |
| `2` | Keep right, reject left |
| `B` | Keep both |
| `D` | Defer (skip for now) |

Bindings mirror Lightroom so muscle memory transfers.

## The `.maier/` cache

Each working folder gets a `.maier/` directory: a SQLite index, generated
previews, and logs. It's a cache, not a source of truth — the filesystem
(file locations + contents) is. Deleting `.maier/` loses no culling
decisions; the next open just re-derives status from where files currently
live (root = undecided, `selected/` = selected, `rejected/` = rejected) and
rebuilds previews/hashes in the background. Safe to `.gitignore`, safe to
delete, not meant to be synced or backed up separately from the photos
themselves.

## exiftool

Maier uses `exiftool` for accurate capture dates, RAW previews, and video
metadata. It's auto-detected on `PATH`, or downloaded automatically (pinned
version, checksum-verified) into your user data directory the first time
it's needed. If it's unavailable (offline, download blocked, unsupported
platform), Maier degrades gracefully: JPEG/HEIC/PNG/TIFF dates still come
from Pillow's EXIF reader, and RAW previews / video metadata show a
placeholder with a visible notice instead of failing.

## Development

```sh
uv sync                                      # install deps
uv run maier open ~/Photos/2025 --browser    # run against a real folder
uv run pytest                                # tests
uv run ruff check                            # lint
uv run ruff format                           # format
```

See [SPEC.md](SPEC.md) for the full specification and [PLAN.md](PLAN.md) for
implementation status.

## License

MIT — see [LICENSE](LICENSE).

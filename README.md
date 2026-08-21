# Culler

Local-first photo culling app where **the folder structure is the state**.

Point it at a folder of photos (e.g. exports from two Apple Photos accounts + Lightroom dropped in as subfolders). It shows everything in one timeline sorted by capture date; you cull with keyboard shortcuts (`P` select, `X` reject, `U` undecide). Every action is an atomic file move into `selected/` or `rejected/` inside your folder — no duplication, no export step, no lock-in. `selected/` always *is* your current selection, readable in any file browser. The app's cache (`.culler/`) is fully rebuildable; deleting it loses nothing.

Built with Python / Django 6 / HTMX. No JS frameworks, no build step.

**Status: work in progress** — see [SPEC.md](SPEC.md) for the full specification and [PLAN.md](PLAN.md) for implementation progress.

## Development

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.14 for you).

```sh
uv sync
uv run culler open ~/Photos/2025 --browser   # run against a folder
uv run pytest                                # tests
```

## Install (once published — M4)

```sh
uvx culler
```

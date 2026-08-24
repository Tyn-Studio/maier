"""The `maier` CLI — the only entry point users touch.

Boots Django programmatically against a chosen working folder (no
manage.py), auto-migrates that folder's `.maier/maier.sqlite3`, and
serves via Waitress on 127.0.0.1.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8347

USAGE_HINT = """\
maier — local-first photo culling

Usage:
  maier                                    launch the app (native window, recent folders)
  maier open PATH [--browser] [--port N]   open a working folder
  maier --browser                          browser mode (see note below)
  maier status PATH                        print status counts, no server

Run 'maier open PATH' to get started.
"""

# `maier --browser` (bare, no folder) has nowhere useful to serve from --
# SPEC's browser-mode "home" (path text input) would need a server bound to
# a placeholder folder, which isn't worth it yet. Point at `open --browser`.
BROWSER_USAGE_HINT = """\
maier — local-first photo culling

Browser mode needs a folder to open:
  maier open PATH --browser [--port N]

Run 'maier open PATH --browser' to get started.
"""


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _pick_port(preferred: int) -> int:
    if _port_available(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _bootstrap_django(folder: Path) -> None:
    os.environ["MAIER_FOLDER"] = str(folder)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "maier.settings")
    import django

    django.setup()


def _window_forced_off() -> bool:
    # Set by headless CI/smoke-test invocations that can't create a native
    # window (also what tests use, since pywebview can't run in a test
    # process). `maier open` still works everywhere: it falls back to
    # browser-mode serving.
    return os.environ.get("MAIER_FORCE_NO_WINDOW") == "1"


def _open_folder(folder: Path, *, browser: bool, port: int) -> int:
    """Shared body of `maier open PATH` and the post-picker bare-command
    flow: bootstrap Django, migrate, start indexing, then serve either in
    a pywebview window (default) or a plain browser tab (--browser /
    MAIER_FORCE_NO_WINDOW / window unavailable)."""
    _bootstrap_django(folder)

    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)

    from django.core.wsgi import get_wsgi_application

    application = get_wsgi_application()

    from maier.core.scan import start_background_scan

    start_background_scan(folder)

    from maier.recents import record_recent

    record_recent(folder)

    # SPEC §12: exiftool absent on PATH -> fetch a pinned, checksum-verified
    # copy into the global data dir, without blocking startup. No-op when
    # already present; offline failures degrade silently (Pillow fallback).
    from maier.core.exiftool import ensure_exiftool

    ensure_exiftool(background=True)

    # PLAN T27: notify of newer releases without ever self-modifying the
    # app. Best-effort only -- the update check must never stop the app
    # from booting (it fails silently anyway; this try/except is belt and
    # braces around the thread-spawn itself).
    try:
        from maier.core import updates

        updates.start_background_check()
    except Exception:
        pass

    port = _pick_port(port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Maier serving {folder} at {url}")

    use_window = not browser and not _window_forced_off()
    window_module = None
    if use_window:
        try:
            from maier import window as window_module
        except Exception:
            use_window = False

    import waitress

    if use_window:
        server_thread = threading.Thread(
            target=lambda: waitress.serve(application, host="127.0.0.1", port=port, threads=12),
            daemon=True,
        )
        server_thread.start()
        try:
            window_module.launch_window(url)
        except Exception:
            print(f"note: could not launch the desktop window; open {url} in a browser instead")
            print("(Ctrl-C to stop)")
            try:
                server_thread.join()
            except KeyboardInterrupt:
                print("\nstopping")
        return 0

    print("(Ctrl-C to stop)")
    if browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    try:
        waitress.serve(application, host="127.0.0.1", port=port, threads=12)
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    folder = Path(args.path).expanduser()
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 1
    folder = folder.resolve()

    return _open_folder(folder, browser=args.browser, port=args.port)


def cmd_status(args: argparse.Namespace) -> int:
    folder = Path(args.path).expanduser()
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 1
    folder = folder.resolve()

    _bootstrap_django(folder)

    from django.db.utils import OperationalError

    from maier.core.models import Photo

    counts = {"optional": 0, "selected": 0, "rejected": 0}
    try:
        for status in counts:
            counts[status] = Photo.objects.filter(status=status).count()
    except OperationalError:
        pass  # no DB / no migrations applied yet -> all zero

    total = sum(counts.values())
    print(f"Folder: {folder}")
    for status, count in counts.items():
        print(f"  {status}: {count}")
    print(f"  total: {total}")
    return 0


def cmd_home(args: argparse.Namespace) -> int:
    """Bare `maier`: native-window home screen (recent folders / picker).
    A chosen folder proceeds exactly like `maier open <choice>`."""
    if args.browser:
        print(BROWSER_USAGE_HINT)
        return 0

    if _window_forced_off():
        print(USAGE_HINT)
        return 0

    try:
        from maier import window
        from maier.recents import load_recents
    except Exception:
        print(USAGE_HINT)
        return 0

    try:
        chosen = window.show_home(load_recents())
    except Exception:
        chosen = None

    if chosen is None:
        print(USAGE_HINT)
        return 0

    return _open_folder(chosen.resolve(), browser=False, port=DEFAULT_PORT)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="maier", add_help=True)
    parser.add_argument("--browser", action="store_true")
    parser.set_defaults(func=cmd_home)
    subparsers = parser.add_subparsers(dest="command")

    open_parser = subparsers.add_parser("open", help="open a working folder")
    open_parser.add_argument("path")
    open_parser.add_argument("--browser", action="store_true")
    open_parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    open_parser.set_defaults(func=cmd_open)

    status_parser = subparsers.add_parser("status", help="print status counts")
    status_parser.add_argument("path")
    status_parser.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        return cmd_home(args)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

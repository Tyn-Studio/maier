"""The `culler` CLI — the only entry point users touch.

Boots Django programmatically against a chosen working folder (no
manage.py), auto-migrates that folder's `.culler/culler.sqlite3`, and
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
culler — local-first photo culling

Usage:
  culler open PATH [--browser] [--port N]   open a working folder
  culler status PATH                        print status counts, no server

Run 'culler open PATH' to get started.
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
    os.environ["CULLER_FOLDER"] = str(folder)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "culler.settings")
    import django

    django.setup()


def cmd_open(args: argparse.Namespace) -> int:
    folder = Path(args.path).expanduser()
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 1
    folder = folder.resolve()

    _bootstrap_django(folder)

    from django.core.management import call_command

    call_command("migrate", verbosity=0, interactive=False)

    from django.core.wsgi import get_wsgi_application

    application = get_wsgi_application()

    from culler.core.scan import start_background_scan

    start_background_scan(folder)

    port = _pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Culler serving {folder} at {url}  (Ctrl-C to stop)")

    if args.browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    import waitress

    try:
        waitress.serve(application, host="127.0.0.1", port=port)
    except KeyboardInterrupt:
        print("\nstopping")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    folder = Path(args.path).expanduser()
    if not folder.is_dir():
        print(f"error: not a directory: {folder}", file=sys.stderr)
        return 1
    folder = folder.resolve()

    _bootstrap_django(folder)

    from django.db.utils import OperationalError

    from culler.core.models import Photo

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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="culler", add_help=True)
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
        print(USAGE_HINT)
        return 0

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

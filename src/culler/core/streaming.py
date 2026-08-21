"""HTTP Range support for streaming original video files (SPEC §10: "videos:
`<video controls>` streaming the original via a Django range-request
view"). Kept as its own module per PLAN T9 brief -- range parsing deserves
isolated unit tests and views.py stays thin.

Never reads a whole file into memory: full-file responses stream via a
plain file object (Django's `FileResponse`/`FileWrapper` reads it in
chunks); partial responses stream via `_BoundedReader`, a thin file-like
wrapper that caps how many bytes get read off an already-`seek`ed file
object.
"""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from django.http import FileResponse, Http404, HttpRequest, HttpResponse

_CONTENT_TYPES = {
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".avi": "video/x-msvideo",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


class RangeNotSatisfiable(Exception):
    """Raised for any unparseable/out-of-bounds/multi-range `Range` header
    -- callers respond 416.
    """


def content_type_for(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), _DEFAULT_CONTENT_TYPE)


def parse_range(range_header: str | None, file_size: int) -> tuple[int, int] | None:
    """Parse a single-range `Range: bytes=start-end` header against a file
    of `file_size` bytes.

    Returns `None` when there's no header at all (caller serves the whole
    file, 200). Returns an inclusive `(start, end)` byte range for a
    satisfiable single range (caller responds 206). Raises
    `RangeNotSatisfiable` for anything unparseable, out of bounds, or a
    multi-range request (we only support a single range, per brief).
    """
    if not range_header:
        return None
    if not range_header.startswith("bytes="):
        raise RangeNotSatisfiable(range_header)
    if file_size <= 0:
        raise RangeNotSatisfiable(range_header)

    spec = range_header[len("bytes=") :]
    if "," in spec:
        raise RangeNotSatisfiable(range_header)  # multi-range: unsupported
    if "-" not in spec:
        raise RangeNotSatisfiable(range_header)

    start_s, _, end_s = spec.partition("-")

    if start_s == "":
        # Suffix range ("last N bytes"): bytes=-500
        if end_s == "":
            raise RangeNotSatisfiable(range_header)
        try:
            n = int(end_s)
        except ValueError:
            raise RangeNotSatisfiable(range_header) from None
        if n <= 0:
            raise RangeNotSatisfiable(range_header)
        start = max(0, file_size - n)
        return start, file_size - 1

    try:
        start = int(start_s)
    except ValueError:
        raise RangeNotSatisfiable(range_header) from None

    if end_s == "":
        end = file_size - 1
    else:
        try:
            end = int(end_s)
        except ValueError:
            raise RangeNotSatisfiable(range_header) from None

    if start < 0 or start > end or start >= file_size:
        raise RangeNotSatisfiable(range_header)

    return start, min(end, file_size - 1)


class _BoundedReader:
    """File-like wrapper around an already-`seek`ed file object that reads
    at most `length` bytes total -- lets `FileResponse` stream an HTTP
    Range slice in chunks without reading the whole file into memory.
    """

    def __init__(self, fileobj: BinaryIO, length: int) -> None:
        self._file = fileobj
        self._remaining = length

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._file.read(want)
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._file.close()


def serve_file_range(request: HttpRequest, path: Path) -> HttpResponse:
    """Serve `path` honouring a `Range` header (SPEC §10). 200 with the
    full file when there's no `Range` header, 206 with `Content-Range` for
    a satisfiable single range, 416 for anything else.
    """
    if not path.exists():
        raise Http404(f"file not found: {path}")

    file_size = path.stat().st_size
    content_type = content_type_for(path)

    try:
        byte_range = parse_range(request.headers.get("Range"), file_size)
    except RangeNotSatisfiable:
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{file_size}"
        response["Accept-Ranges"] = "bytes"
        return response

    if byte_range is None:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = str(file_size)
        response["Accept-Ranges"] = "bytes"
        return response

    start, end = byte_range
    length = end - start + 1
    f = path.open("rb")
    f.seek(start)
    response = FileResponse(_BoundedReader(f, length), status=206, content_type=content_type)
    response["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    response["Content-Length"] = str(length)
    response["Accept-Ranges"] = "bytes"
    return response

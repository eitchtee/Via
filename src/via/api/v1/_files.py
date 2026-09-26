"""Shared helpers for file uploads and (range) downloads."""

from __future__ import annotations

import base64
import re
from collections.abc import Iterator
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import StreamingResponse

from via.context import Context
from via.crypto import iter_plaintext
from via.db.models import Push
from via.errors import APIError
from via.services import pushes

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single-range ``Range`` header. Returns ``None`` to serve the whole file."""
    m = _RANGE_RE.match(header.strip())
    if not m or not (m[1] or m[2]):
        return None
    unsatisfiable = APIError(
        416,
        "range_not_satisfiable",
        "Requested range is outside the file",
        headers={"Content-Range": f"bytes */{size}"},
    )
    if m[1]:
        start = int(m[1])
        end = min(int(m[2]), size - 1) if m[2] else size - 1
    else:
        suffix = int(m[2])
        if suffix == 0:
            raise unsatisfiable
        start, end = max(0, size - suffix), size - 1
    if start > end or start >= size:
        raise unsatisfiable
    return start, end


def _content_disposition(filename: str) -> str:
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", filename) or "file"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"


def file_response(ctx: Context, push: Push, range_header: str | None) -> StreamingResponse:
    """Stream a push's decrypted file, honoring a single-range ``Range`` header."""
    if not push.file_stored or not ctx.blobs.exists(push.id):
        raise APIError(410, "gone", "This file is no longer stored")
    dek, meta = pushes.decrypt(ctx, push)
    size = push.file_size or 0
    byte_range = parse_range(range_header, size) if range_header else None
    start, end = byte_range or (0, size - 1)
    sha256 = meta.get("sha256", "")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition(meta.get("filename") or push.id),
        "Content-Length": str(end - start + 1),
        "ETag": f'"{push.id}"',
        # Digest of the whole file, also on range responses (RFC 9530).
        "Repr-Digest": f"sha-256=:{base64.b64encode(bytes.fromhex(sha256)).decode()}:",
        "X-Via-SHA256": sha256,
    }
    if byte_range:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    path = ctx.blobs.path(push.id)

    def body() -> Iterator[bytes]:
        with path.open("rb") as fh:
            yield from iter_plaintext(fh, dek, size, start, end)

    return StreamingResponse(
        body(),
        status_code=206 if byte_range else 200,
        media_type=meta.get("mime") or "application/octet-stream",
        headers=headers,
    )


def upload_mime(request: Request) -> str:
    """The uploaded file's MIME type, from ``Content-Type``."""
    mime = request.headers.get("content-type", "").split(";")[0].strip()
    if not mime or mime in ("application/x-www-form-urlencoded", "multipart/form-data"):
        return "application/octet-stream"
    return mime


def declared_size(request: Request) -> int | None:
    value = request.headers.get("content-length", "")
    return int(value) if value.isdigit() else None

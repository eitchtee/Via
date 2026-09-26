"""``/v1/send``: a curl-friendly shortcut in the spirit of ntfy.

    curl -H "Authorization: Bearer $TOKEN" -d "https://example.com" $VIA/v1/send
    curl -H "Authorization: Bearer $TOKEN" -d "remember the milk" $VIA/v1/send?to=@bob
    curl -H "Authorization: Bearer $TOKEN" -T report.pdf $VIA/v1/send/

A body that is a single URL becomes a link, other text becomes a note, and a filename (in the
path or the ``filename`` query parameter) makes it a file.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Query, Request

from via.api.v1._files import declared_size, upload_mime
from via.api.v1._params import Title, To, Ttl
from via.deps import DB, AnyAuth, Ctx
from via.errors import APIError
from via.schemas import PushStatus
from via.services import pushes

router = APIRouter(prefix="/send", tags=["send"])

_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")


async def _send(
    request: Request,
    principal: AnyAuth,
    ctx: Ctx,
    db: DB,
    *,
    to: str,
    title: str | None,
    ttl: str | None,
    filename: str | None,
) -> PushStatus:
    targets = pushes.parse_targets(to)
    if filename:
        return await pushes.send_file(
            ctx,
            db,
            principal,
            request.stream(),
            to=targets,
            filename=filename,
            mime=upload_mime(request),
            declared_size=declared_size(request),
            title=title,
            ttl=ttl,
        )

    max_bytes = ctx.settings.max_text_length * 4
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            raise APIError(413, "text_too_long", "Text body too long; send it as a file")
    try:
        text = raw.decode().strip()
    except UnicodeDecodeError as e:
        raise APIError(
            400, "not_text", "Body is not UTF-8 text; pass a filename to send a file"
        ) from e
    if not text:
        raise APIError(422, "empty_body", "Nothing to send")
    if _URL_RE.match(text):
        return await pushes.send_text(
            ctx, db, principal, kind="link", to=targets, title=title, url=text, ttl=ttl
        )
    return await pushes.send_text(
        ctx, db, principal, kind="note", to=targets, title=title, body=text, ttl=ttl
    )


@router.post("", status_code=201)
@router.put("", status_code=201)
async def send(
    request: Request,
    principal: AnyAuth,
    ctx: Ctx,
    db: DB,
    to: To = "all",
    title: Title = None,
    ttl: Ttl = None,
    filename: Annotated[str | None, Query(max_length=255)] = None,
) -> PushStatus:
    """Send the request body as a link, a note or (with `filename`) a file."""
    return await _send(request, principal, ctx, db, to=to, title=title, ttl=ttl, filename=filename)


@router.post("/{filename}", status_code=201)
@router.put("/{filename}", status_code=201)
async def send_file(
    filename: str,
    request: Request,
    principal: AnyAuth,
    ctx: Ctx,
    db: DB,
    to: To = "all",
    title: Title = None,
    ttl: Ttl = None,
) -> PushStatus:
    """Send the request body as a file named `filename` (works with `curl -T file URL/`)."""
    return await _send(request, principal, ctx, db, to=to, title=title, ttl=ttl, filename=filename)

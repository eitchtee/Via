from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from via.api.v1._files import declared_size, file_response, upload_mime
from via.api.v1._params import Filename, Message, Title, To, Ttl
from via.deps import DB, AnyAuth, Ctx, DeviceAuth
from via.errors import APIError
from via.schemas import HistoryItem, PushIn, PushStatus
from via.services import pushes
from via.services.common import commit

router = APIRouter(prefix="/pushes", tags=["pushes"])

_RAW_BODY = {
    "requestBody": {
        "required": True,
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


@router.post("", status_code=201)
async def create_push(body: PushIn, principal: AnyAuth, ctx: Ctx, db: DB) -> PushStatus:
    """Send a link or a note to your devices and/or contacts."""
    return await pushes.send_text(
        ctx,
        db,
        principal,
        kind=body.kind,
        to=body.to,
        title=body.title,
        body=body.body,
        url=body.url,
        ttl=body.ttl,
    )


@router.post("/file", status_code=201, openapi_extra=_RAW_BODY)
async def create_file_push(
    request: Request,
    principal: AnyAuth,
    ctx: Ctx,
    db: DB,
    filename: Filename,
    to: To = "all",
    title: Title = None,
    body: Message = None,
    ttl: Ttl = None,
) -> PushStatus:
    """Send a file. The request body is the raw file content (streamed, not multipart);
    `Content-Type` is stored as the file's MIME type."""
    return await pushes.send_file(
        ctx,
        db,
        principal,
        request.stream(),
        to=pushes.parse_targets(to),
        filename=filename,
        mime=upload_mime(request),
        declared_size=declared_size(request),
        title=title,
        body=body,
        ttl=ttl,
    )


@router.get("/sent")
async def list_sent(
    principal: AnyAuth,
    db: DB,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[PushStatus]:
    """Sent pushes, newest first, with per-device delivery status. Sessions and devices see the
    whole account's; an app token sees only the pushes it sent."""
    return await pushes.list_sent(db, principal, before=before, limit=limit)


def _require_history(ctx: Ctx) -> None:
    if ctx.settings.history_days <= 0:
        raise APIError(404, "history_disabled", "Sent history is disabled on this server")


@router.get("/history")
async def list_history(
    principal: DeviceAuth,
    ctx: Ctx,
    db: DB,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[HistoryItem]:
    """Content of pushes this device sent: still pending, or kept after delivery when the server
    keeps sent history. Device tokens only: sessions and app tokens never read content."""
    _require_history(ctx)
    return await pushes.list_history(ctx, db, principal.this_device, before=before, limit=limit)


@router.get("/history/{push_id}/file")
async def download_history_file(
    push_id: str,
    principal: DeviceAuth,
    ctx: Ctx,
    db: DB,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    _require_history(ctx)
    push = await pushes.get_history_push(db, principal.this_device, push_id)
    return file_response(ctx, push, range_header)


@router.delete("/history/{push_id}", status_code=204)
async def delete_history_entry(push_id: str, principal: DeviceAuth, ctx: Ctx, db: DB) -> Response:
    _require_history(ctx)
    push = await pushes.get_history_push(db, principal.this_device, push_id)
    if push.purged_at is None:
        raise APIError(409, "still_pending", "Recall the push instead: DELETE /v1/pushes/{id}")
    pushes.drop_content(db, push)
    await commit(ctx, db)
    return Response(status_code=204)


@router.get("/{push_id}")
async def get_push_status(push_id: str, principal: AnyAuth, db: DB) -> PushStatus:
    push = await pushes.get_sent_push(db, principal, push_id)
    return (await pushes.statuses(db, [push]))[0]


@router.delete("/{push_id}", status_code=204)
async def recall_push(push_id: str, principal: AnyAuth, ctx: Ctx, db: DB) -> Response:
    """Recall a push: devices that haven't acked it won't get it, and its content is deleted.
    App tokens can only recall pushes they sent."""
    push = await pushes.get_sent_push(db, principal, push_id)
    await pushes.recall(ctx, db, push)
    return Response(status_code=204)

"""The receiving side. Every endpoint here needs a device token and only sees that device's
own inbox."""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse

from via.api.v1._files import file_response
from via.deps import DB, Ctx, DeviceAuth
from via.errors import APIError, not_found
from via.notify.hub import Event, sse_events
from via.schemas import AckIn, AckOut, InboxItem, InboxPage
from via.services import pushes

router = APIRouter(prefix="/inbox", tags=["inbox"])


async def _wait_for_push(queue: asyncio.Queue[Event]) -> None:
    while (await queue.get())["type"] not in ("push", "revoked", "shutdown"):
        pass


@router.get("")
async def list_inbox(
    principal: DeviceAuth,
    ctx: Ctx,
    db: DB,
    after: Annotated[str | None, Query(description="Cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    wait: Annotated[
        int, Query(ge=0, le=60, description="Long-poll: seconds to wait if the inbox is empty")
    ] = 0,
) -> InboxPage:
    """Items waiting for this device, oldest first. Items stay here until acked."""
    device = principal.this_device
    with ctx.hub.subscribe(device.id) as queue:
        items = await pushes.list_inbox(ctx, db, device, after=after, limit=limit)
        if not items and wait:
            try:
                await asyncio.wait_for(_wait_for_push(queue), wait)
            except TimeoutError:
                pass
            else:
                items = await pushes.list_inbox(ctx, db, device, after=after, limit=limit)
    return InboxPage(items=items, cursor=items[-1].id if items else None)


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Event stream"}},
)
async def events(principal: DeviceAuth, ctx: Ctx) -> StreamingResponse:
    """Server-sent events. `ready` on connect (fetch your inbox then), `push` when something
    arrives (`{"id", "kind"}`), `recalled` when a sender takes a push back, `revoked` when this
    device is removed. A `: ping` comment is sent periodically to keep the connection alive."""
    return StreamingResponse(
        sse_events(ctx.hub, principal.this_device.id, ctx.settings.sse_heartbeat),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/ack")
async def ack_many(body: AckIn, principal: DeviceAuth, ctx: Ctx, db: DB) -> AckOut:
    """Confirm receipt of several items. Unknown ids are ignored."""
    return AckOut(acked=await pushes.ack(ctx, db, principal.this_device, body.ids))


@router.get("/{push_id}")
async def get_item(push_id: str, principal: DeviceAuth, ctx: Ctx, db: DB) -> InboxItem:
    push = await pushes.get_inbox_push(db, principal.this_device, push_id)
    return (await pushes.to_items(ctx, db, principal.this_device, [push]))[0]


@router.get("/{push_id}/file")
async def download_file(
    push_id: str,
    principal: DeviceAuth,
    ctx: Ctx,
    db: DB,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> StreamingResponse:
    """Download the file of a `file` item. Supports `Range` to resume. Verify the content
    against the `X-Via-SHA256` header, then ack."""
    push = await pushes.get_inbox_push(db, principal.this_device, push_id)
    if push.kind != "file":
        raise APIError(404, "not_a_file", "This item has no file")
    await db.commit()  # don't hold the read transaction while streaming
    return file_response(ctx, push, range_header)


@router.post("/{push_id}/ack")
async def ack(push_id: str, principal: DeviceAuth, ctx: Ctx, db: DB) -> AckOut:
    """Confirm receipt. Once every target device has acked, the content is deleted.
    Acking again is harmless."""
    acked = await pushes.ack(ctx, db, principal.this_device, [push_id])
    if not acked:
        raise not_found("Delivery")
    return AckOut(acked=acked)

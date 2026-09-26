import asyncio
import json

import httpx
from httpx import AsyncClient

from via.db.models import Device as DeviceRow
from via.notify.hub import EventHub, sse_events

from .conftest import Api, auth, expect


async def test_long_poll_wakes_up_on_push(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")

    async def send_later() -> None:
        await asyncio.sleep(0.2)
        await api.send(phone.headers, kind="note", body="wake up")

    loop = asyncio.get_running_loop()
    started = loop.time()
    poll, _ = await asyncio.gather(
        client.get("/v1/inbox?wait=10", headers=laptop.headers), send_later()
    )
    assert loop.time() - started < 5
    assert [i["body"] for i in expect(poll, 200)["items"]] == ["wake up"]


async def test_long_poll_times_out_empty(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    laptop = await api.device(session, "laptop")
    r = await client.get("/v1/inbox?wait=1", headers=laptop.headers)
    assert expect(r, 200) == {"items": [], "cursor": None}


async def test_sse_stream() -> None:
    hub = EventHub()
    stream = sse_events(hub, "dev1", heartbeat=0.05)
    assert "event: ready" in await anext(stream)
    assert await anext(stream) == ": ping\n\n"
    hub.publish("dev1", {"type": "push", "id": "p1", "kind": "note"})
    hub.publish("dev2", {"type": "push", "id": "other", "kind": "note"})
    event = await anext(stream)
    assert event.startswith("event: push\n")
    assert json.loads(event.split("data: ")[1]) == {"type": "push", "id": "p1", "kind": "note"}
    hub.close()
    assert [e async for e in stream] == []
    assert not hub.is_connected("dev1")


async def test_sse_endpoint_requires_device_token(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    expect(await client.get("/v1/inbox/events", headers=auth(session)), 403)


async def test_push_providers(api: Api, client: AsyncClient) -> None:
    requests: list[httpx.Request] = []
    gone = {"https://up.example/gone"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(410 if str(request.url) in gone else 200)

    dispatcher = api.ctx.dispatcher
    dispatcher.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher.relay_url = "https://relay.example"
    dispatcher.relay_headers = {"X-Via-Instance-Key": "instance-key"}

    session = await api.user()
    phone = await api.device(session, "phone")
    up = await api.device(session, "up")
    fcm = await api.device(session, "fcm")
    stale = await api.device(session, "stale")
    for device, body in [
        (up, {"provider": "unifiedpush", "endpoint": "https://up.example/abc"}),
        (fcm, {"provider": "fcm_relay", "token": "fcm-token-1"}),
        (stale, {"provider": "unifiedpush", "endpoint": "https://up.example/gone"}),
    ]:
        expect(await client.put("/v1/devices/me/push", json=body, headers=device.headers), 200)

    push = await api.send(phone.headers, kind="note", body="secret content")
    await dispatcher.drain()

    by_url = {str(r.url): json.loads(r.content) for r in requests}
    wake = {"t": "wake", "id": push["id"]}
    assert by_url == {
        "https://up.example/abc": wake,
        "https://relay.example/v1/wake": {"token": "fcm-token-1", "id": push["id"]},
        "https://up.example/gone": wake,
    }
    assert all(b"secret" not in r.content for r in requests)
    relay_request = next(r for r in requests if r.url.host == "relay.example")
    assert relay_request.headers["x-via-instance-key"] == "instance-key"
    assert all("x-via-instance-key" not in r.headers for r in requests if r is not relay_request)

    # A 410 from the endpoint clears that device's registration.
    async with api.ctx.sessionmaker() as db:
        row = await db.get(DeviceRow, stale.id)
        assert row is not None and row.push_provider == "none" and row.push_target is None

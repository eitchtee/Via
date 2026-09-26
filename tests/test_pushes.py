from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select

from via import timeutil
from via.config import Settings
from via.db.models import Push
from via.services import janitor

from .conftest import Api, auth, expect


async def _push_row(api: Api, push_id: str) -> Push | None:
    async with api.ctx.sessionmaker() as db:
        return await db.scalar(select(Push).where(Push.id == push_id))


async def test_link_lifecycle(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    tablet = await api.device(session, "tablet")

    push = await api.send(phone.headers, kind="link", url="https://example.com", title="Example")
    assert {d["device_id"] for d in push["deliveries"]} == {laptop.id, tablet.id}

    # The sender doesn't get its own push when sending to "all".
    assert await api.inbox(phone) == []
    items = await api.inbox(laptop)
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "link"
    assert item["url"] == "https://example.com"
    assert item["title"] == "Example"
    assert item["source_device_id"] == phone.id

    # Content is encrypted at rest.
    row = await _push_row(api, push["id"])
    assert row is not None and row.meta is not None
    assert b"example.com" not in row.meta

    # Fetching doesn't remove it; acking does.
    assert len(await api.inbox(laptop)) == 1
    assert (await api.ack(laptop, push["id"]))["acked"] == [push["id"]]
    assert (await api.ack(laptop, push["id"]))["acked"] == [push["id"]]  # idempotent
    assert await api.inbox(laptop) == []

    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=phone.headers), 200)
    states = {d["device_id"]: d["state"] for d in status["deliveries"]}
    assert states == {laptop.id: "acked", tablet.id: "delivered"} or states == {
        laptop.id: "acked",
        tablet.id: "pending",
    }
    assert status["purged_at"] is None

    # Once the last device acks, content is purged; a tombstone keeps the status.
    await api.ack(tablet, push["id"])
    row = await _push_row(api, push["id"])
    assert row is not None and row.meta is None and row.wrapped_key is None
    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=phone.headers), 200)
    assert status["purged_at"] is not None


async def test_note_to_specific_device(api: Api) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    tablet = await api.device(session, "tablet")
    await api.send(phone.headers, kind="note", body="hello", to=[tablet.id])
    assert await api.inbox(laptop) == []
    assert [i["body"] for i in await api.inbox(tablet)] == ["hello"]


async def test_sending_errors(api: Api) -> None:
    session = await api.user()
    phone = await api.device(session)
    other = await api.device(await api.user("bob"))

    # No other devices.
    r = await api.send(phone.headers, 422, kind="note", body="x")
    assert r["error"]["code"] == "no_targets"
    # Another account's device.
    r = await api.send(phone.headers, 422, kind="note", body="x", to=[other.id])
    assert r["error"]["code"] == "unknown_device"
    r = await api.send(phone.headers, 422, kind="link", title="no url", to=[phone.id])
    assert r["error"]["code"] == "validation_error"
    r = await api.send(phone.headers, 422, kind="note", body="x", to=[phone.id], ttl="soon")
    assert r["error"]["code"] == "invalid_ttl"


async def test_text_length_limit(api: Api, settings: Settings) -> None:
    settings.max_text_length = 10
    session = await api.user()
    phone = await api.device(session)
    r = await api.send(phone.headers, 413, kind="note", body="x" * 11, to=[phone.id])
    assert r["error"]["code"] == "text_too_long"


async def test_who_can_read_what(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    app_token = expect(
        await client.post("/v1/app-tokens", json={"name": "script"}, headers=auth(session)), 201
    )["token"]

    # App tokens and sessions can send...
    push = await api.send(auth(app_token), kind="note", body="from a script", to=[laptop.id])
    await api.send(auth(session), kind="note", body="from the web", to=[laptop.id])
    # ...but only the device itself can read its inbox.
    for token in (app_token, session):
        expect(await client.get("/v1/inbox", headers=auth(token)), 403)
        expect(await client.get(f"/v1/inbox/{push['id']}", headers=auth(token)), 403)
    expect(await client.get(f"/v1/inbox/{push['id']}", headers=phone.headers), 404)
    expect(await client.post(f"/v1/inbox/{push['id']}/ack", headers=phone.headers), 404)
    item = expect(await client.get(f"/v1/inbox/{push['id']}", headers=laptop.headers), 200)
    assert item["body"] == "from a script"
    assert item["source_device_id"] is None

    # Another account sees nothing.
    bob = await api.user("bob")
    expect(await client.get(f"/v1/pushes/{push['id']}", headers=auth(bob)), 404)
    expect(await client.delete(f"/v1/pushes/{push['id']}", headers=auth(bob)), 404)


async def test_batch_ack_and_pagination(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    ids = [(await api.send(phone.headers, kind="note", body=str(i)))["id"] for i in range(5)]

    first = expect(await client.get("/v1/inbox?limit=2", headers=laptop.headers), 200)
    assert [i["id"] for i in first["items"]] == ids[:2]
    second = expect(
        await client.get(f"/v1/inbox?limit=2&after={first['cursor']}", headers=laptop.headers),
        200,
    )
    assert [i["id"] for i in second["items"]] == ids[2:4]

    r = await client.post(
        "/v1/inbox/ack", json={"ids": [*ids[:3], "bogus"]}, headers=laptop.headers
    )
    assert expect(r, 200)["acked"] == ids[:3]
    assert [i["id"] for i in await api.inbox(laptop)] == ids[3:]

    sent = expect(await client.get("/v1/pushes/sent?limit=3", headers=phone.headers), 200)
    assert [p["id"] for p in sent] == ids[::-1][:3]


async def test_recall(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    push = await api.send(phone.headers, kind="note", body="oops")
    with api.ctx.hub.subscribe(laptop.id) as queue:
        expect(await client.delete(f"/v1/pushes/{push['id']}", headers=phone.headers), 204)
        assert queue.get_nowait() == {"type": "recalled", "id": push["id"]}
    assert await api.inbox(laptop) == []
    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=phone.headers), 200)
    assert status["deliveries"][0]["state"] == "expired"


async def test_ttl_expiry_and_tombstones(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    push = await api.send(phone.headers, kind="note", body="short-lived", ttl="1h")
    assert push["expires_at"]

    now = timeutil.utcnow()
    await janitor.run_once(api.ctx, now + timedelta(minutes=30))
    assert len(await api.inbox(laptop)) == 1

    await janitor.run_once(api.ctx, now + timedelta(hours=2))
    assert await api.inbox(laptop) == []
    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=phone.headers), 200)
    assert status["deliveries"][0]["state"] == "expired"

    # Tombstones go away after VIA_TOMBSTONE_DAYS.
    await janitor.run_once(api.ctx, now + timedelta(days=8))
    assert await _push_row(api, push["id"]) is None


async def test_ttl_is_capped(api: Api, settings: Settings) -> None:
    session = await api.user()
    phone = await api.device(session)
    push = await api.send(phone.headers, kind="note", body="x", to=[phone.id], ttl="365d")
    row = await _push_row(api, push["id"])
    assert row is not None
    assert row.expires_at - row.created_at == timedelta(seconds=settings.max_ttl)


async def test_history(api: Api, client: AsyncClient, settings: Settings) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    expect(await client.get("/v1/pushes/history", headers=phone.headers), 404)

    settings.history_days = 3
    push = await api.send(phone.headers, kind="link", url="https://kept.example")
    await api.inbox(laptop)
    await api.ack(laptop, push["id"])

    history = expect(await client.get("/v1/pushes/history", headers=phone.headers), 200)
    assert [(h["id"], h["url"]) for h in history] == [(push["id"], "https://kept.example")]
    assert history[0]["purged_at"] is not None
    # History belongs to the sending device only.
    assert expect(await client.get("/v1/pushes/history", headers=laptop.headers), 200) == []
    expect(await client.get("/v1/pushes/history", headers=auth(session)), 403)

    # It expires...
    await janitor.run_once(api.ctx, timeutil.utcnow() + timedelta(days=4))
    assert expect(await client.get("/v1/pushes/history", headers=phone.headers), 200) == []

    # ...or can be deleted.
    push = await api.send(phone.headers, kind="note", body="delete me")
    r = await client.delete(f"/v1/pushes/history/{push['id']}", headers=phone.headers)
    assert expect(r, 409)["error"]["code"] == "still_pending"
    await api.ack(laptop, push["id"])
    expect(await client.delete(f"/v1/pushes/history/{push['id']}", headers=phone.headers), 204)
    assert expect(await client.get("/v1/pushes/history", headers=phone.headers), 200) == []


async def test_app_tokens_only_see_their_own_pushes(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    await api.device(session, "laptop")

    async def app_token(name: str) -> dict[str, str]:
        r = await client.post("/v1/app-tokens", json={"name": name}, headers=auth(session))
        return auth(expect(r, 201)["token"])

    script, other_script = await app_token("script"), await app_token("other")
    mine = await api.send(script, kind="note", body="from the script")
    theirs = await api.send(other_script, kind="note", body="from another script")
    from_phone = await api.send(phone.headers, kind="note", body="from the phone")

    sent = expect(await client.get("/v1/pushes/sent", headers=script), 200)
    assert [p["id"] for p in sent] == [mine["id"]]
    expect(await client.get(f"/v1/pushes/{mine['id']}", headers=script), 200)
    for push in (theirs, from_phone):
        expect(await client.get(f"/v1/pushes/{push['id']}", headers=script), 404)
        expect(await client.delete(f"/v1/pushes/{push['id']}", headers=script), 404)

    # Sessions and devices see (and may recall) everything sent from the account.
    for headers in (auth(session), phone.headers):
        sent = expect(await client.get("/v1/pushes/sent", headers=headers), 200)
        assert {p["id"] for p in sent} == {mine["id"], theirs["id"], from_phone["id"]}
    expect(await client.delete(f"/v1/pushes/{theirs['id']}", headers=phone.headers), 204)

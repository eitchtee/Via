from httpx import AsyncClient

from .conftest import Api, auth, expect


async def test_register_list_rename(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop", "desktop")
    assert phone.token.startswith("via_d_")

    devices = expect(await client.get("/v1/devices", headers=phone.headers), 200)
    assert [(d["name"], d["current"]) for d in devices] == [("phone", True), ("laptop", False)]

    me = expect(await client.get("/v1/devices/me", headers=laptop.headers), 200)
    assert me["id"] == laptop.id and me["type"] == "desktop"

    r = await client.patch(
        f"/v1/devices/{laptop.id}", json={"name": "work laptop"}, headers=auth(session)
    )
    assert expect(r, 200)["name"] == "work laptop"


async def test_only_the_owner_session_manages_devices(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    for headers in (phone.headers, laptop.headers):
        expect(
            await client.patch(f"/v1/devices/{phone.id}", json={"name": "x"}, headers=headers), 403
        )
        expect(await client.delete(f"/v1/devices/{laptop.id}", headers=headers), 403)
        expect(await client.post("/v1/devices", json={"name": "x"}, headers=headers), 403)

    # Another account can't see or touch them.
    mallory = await api.user("mallory")
    expect(await client.delete(f"/v1/devices/{phone.id}", headers=auth(mallory)), 404)
    assert expect(await client.get("/v1/devices", headers=auth(mallory)), 200) == []


async def test_revoke_device(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone")
    laptop = await api.device(session, "laptop")
    push = await api.send(auth(session), kind="note", body="hi", to=[laptop.id])

    expect(await client.delete(f"/v1/devices/{laptop.id}", headers=auth(session)), 204)
    expect(await client.get("/v1/inbox", headers=laptop.headers), 401)
    devices = expect(await client.get("/v1/devices", headers=phone.headers), 200)
    assert [d["id"] for d in devices] == [phone.id]

    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=phone.headers), 200)
    assert status["deliveries"][0]["state"] == "expired"
    assert status["purged_at"] is not None


async def test_rotate_token(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session)
    new = expect(await client.post("/v1/devices/me/rotate-token", headers=phone.headers), 200)
    expect(await client.get("/v1/inbox", headers=phone.headers), 401)
    expect(await client.get("/v1/inbox", headers=auth(new["token"])), 200)


async def test_push_registration(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session)
    url = "/v1/devices/me/push"
    r = await client.put(url, json={"provider": "fcm_relay"}, headers=phone.headers)
    expect(r, 422)
    r = await client.put(
        url, json={"provider": "unifiedpush", "endpoint": "ftp://x"}, headers=phone.headers
    )
    expect(r, 422)
    r = await client.put(url, json={"provider": "fcm_relay", "token": "abc"}, headers=phone.headers)
    assert expect(r, 200)["push_provider"] == "fcm_relay"
    r = await client.put(url, json={"provider": "none"}, headers=phone.headers)
    assert expect(r, 200)["push_provider"] == "none"
    # Sessions can't set a device's push registration.
    r = await client.put(url, json={"provider": "none"}, headers=auth(session))
    expect(r, 403)


async def test_device_updates_itself(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session, "phone", "android")
    laptop = await api.device(session, "laptop")

    r = await client.patch(
        "/v1/devices/me", json={"name": "Pixel", "accepts_shares": False}, headers=phone.headers
    )
    me = expect(r, 200)
    assert (me["id"], me["name"], me["accepts_shares"], me["current"]) == (
        phone.id,
        "Pixel",
        False,
        True,
    )
    # Only itself: other devices still need a session.
    r = await client.patch(f"/v1/devices/{laptop.id}", json={"name": "x"}, headers=phone.headers)
    expect(r, 403)
    expect(await client.patch("/v1/devices/me", json={"name": "x"}, headers=auth(session)), 403)
    r = await client.patch("/v1/devices/me", json={"name": ""}, headers=phone.headers)
    expect(r, 422)

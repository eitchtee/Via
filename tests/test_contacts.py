from httpx import AsyncClient

from .conftest import Api, Device, auth, expect


async def _befriend(api: Api, a: str, b: str) -> None:
    """Make users with sessions ``a`` and ``b`` contacts."""
    me = expect(await api.client.get("/v1/account", headers=auth(b)), 200)["username"]
    request = expect(
        await api.client.post("/v1/contacts", json={"username": me}, headers=auth(a)), 201
    )
    expect(await api.client.post(f"/v1/contacts/{request['id']}/accept", headers=auth(b)), 200)


async def test_contact_requests(api: Api, client: AsyncClient) -> None:
    alice = await api.user("alice")
    bob = await api.user("bob")

    r = await client.post("/v1/contacts", json={"username": "@BOB"[1:]}, headers=auth(alice))
    request = expect(r, 201)
    assert request["status"] == "pending" and request["direction"] == "outgoing"
    assert request["username"] == "bob"

    r = await client.post("/v1/contacts", json={"username": "bob"}, headers=auth(alice))
    assert expect(r, 409)["error"]["code"] == "already_requested"
    r = await client.post("/v1/contacts", json={"username": "alice"}, headers=auth(alice))
    assert expect(r, 422)["error"]["code"] == "cannot_add_self"
    r = await client.post("/v1/contacts", json={"username": "nobody"}, headers=auth(alice))
    assert expect(r, 404)["error"]["code"] == "user_not_found"

    [incoming] = expect(await client.get("/v1/contacts", headers=auth(bob)), 200)
    assert incoming["direction"] == "incoming" and incoming["username"] == "alice"
    # Only the addressee can accept.
    expect(await client.post(f"/v1/contacts/{request['id']}/accept", headers=auth(alice)), 409)
    accepted = expect(
        await client.post(f"/v1/contacts/{request['id']}/accept", headers=auth(bob)), 200
    )
    assert accepted["status"] == "accepted" and accepted["accepted_at"]

    r = await client.post("/v1/contacts", json={"username": "alice"}, headers=auth(bob))
    assert expect(r, 409)["error"]["code"] == "already_contacts"

    # Either side can remove it; a stranger can't see it.
    carol = await api.user("carol")
    expect(await client.delete(f"/v1/contacts/{request['id']}", headers=auth(carol)), 404)
    expect(await client.delete(f"/v1/contacts/{request['id']}", headers=auth(bob)), 204)
    assert expect(await client.get("/v1/contacts", headers=auth(alice)), 200) == []


async def test_mutual_requests_accept_each_other(api: Api, client: AsyncClient) -> None:
    alice = await api.user("alice")
    bob = await api.user("bob")
    expect(await client.post("/v1/contacts", json={"username": "bob"}, headers=auth(alice)), 201)
    r = await client.post("/v1/contacts", json={"username": "alice"}, headers=auth(bob))
    assert expect(r, 201)["status"] == "accepted"


async def test_contact_management_needs_a_session(api: Api, client: AsyncClient) -> None:
    alice = await api.user("alice")
    phone = await api.device(alice)
    r = await client.post("/v1/contacts", json={"username": "x"}, headers=phone.headers)
    expect(r, 403)
    # Devices can list contacts (to offer them as targets).
    expect(await client.get("/v1/contacts", headers=phone.headers), 200)


async def _setup_sharing(api: Api) -> tuple[str, Device, str, Device, Device, Device]:
    alice = await api.user("alice")
    alice_phone = await api.device(alice, "alice phone")
    bob = await api.user("bob")
    bob_phone = await api.device(bob, "bob phone", "android")
    bob_laptop = await api.device(bob, "bob laptop", "desktop")
    bob_cli = await api.device(bob, "bob cli", "cli")  # doesn't accept shares by default
    return alice, alice_phone, bob, bob_phone, bob_laptop, bob_cli


async def test_sharing(api: Api, client: AsyncClient) -> None:
    alice, alice_phone, bob, bob_phone, bob_laptop, bob_cli = await _setup_sharing(api)

    r = await api.send(alice_phone.headers, 422, kind="note", body="hi", to=["@bob"])
    assert r["error"]["code"] == "unknown_contact"
    await _befriend(api, alice, bob)

    push = await api.send(
        alice_phone.headers, kind="link", url="https://shared.example", to=["@bob"]
    )
    # The sender sees the contact's username, never their device ids.
    assert [(d["device_id"], d["recipient"]) for d in push["deliveries"]] == [
        (None, "bob"),
        (None, "bob"),
    ]
    assert all(bob_phone.id not in str(d) for d in push["deliveries"])

    for device in (bob_phone, bob_laptop):
        [item] = await api.inbox(device)
        assert item["url"] == "https://shared.example"
        assert item["sender"] == "alice"
        assert item["source_device_id"] is None
    assert await api.inbox(bob_cli) == []
    assert await api.inbox(alice_phone) == []

    # Shared items are acked and purged like any other.
    await api.ack(bob_phone, push["id"])
    await api.ack(bob_laptop, push["id"])
    status = expect(await client.get(f"/v1/pushes/{push['id']}", headers=alice_phone.headers), 200)
    assert status["purged_at"] is not None
    # Bob can't see or recall Alice's push.
    expect(await client.get(f"/v1/pushes/{push['id']}", headers=bob_phone.headers), 404)

    # Own devices and contacts can be mixed; bob's side works the same way.
    bob_to_alice = await api.send(
        bob_phone.headers, kind="note", body="hey", to=["@alice", bob_laptop.id]
    )
    assert {(d["device_id"], d["recipient"]) for d in bob_to_alice["deliveries"]} == {
        (None, "alice"),
        (bob_laptop.id, None),
    }
    [item] = await api.inbox(alice_phone)
    assert item["sender"] == "bob"
    [own] = await api.inbox(bob_laptop)
    assert own["sender"] is None and own["source_device_id"] == bob_phone.id


async def test_share_opt_out_and_removal(api: Api, client: AsyncClient) -> None:
    alice, alice_phone, bob, bob_phone, bob_laptop, _ = await _setup_sharing(api)
    await _befriend(api, alice, bob)

    for device in (bob_phone, bob_laptop):
        r = await client.patch(
            f"/v1/devices/{device.id}", json={"accepts_shares": False}, headers=auth(bob)
        )
        assert expect(r, 200)["accepts_shares"] is False
    r = await api.send(alice_phone.headers, 422, kind="note", body="hi", to=["@bob"])
    assert r["error"]["code"] == "contact_unreachable"

    expect(
        await client.patch(
            f"/v1/devices/{bob_phone.id}", json={"accepts_shares": True}, headers=auth(bob)
        ),
        200,
    )
    push = await api.send(alice_phone.headers, kind="note", body="hi", to=["@bob"])
    assert len(push["deliveries"]) == 1

    # Removing the contact stops new shares; items already sent stay.
    [contact] = expect(await client.get("/v1/contacts", headers=auth(bob)), 200)
    expect(await client.delete(f"/v1/contacts/{contact['id']}", headers=auth(bob)), 204)
    r = await api.send(alice_phone.headers, 422, kind="note", body="again", to=["@bob"])
    assert r["error"]["code"] == "unknown_contact"
    assert len(await api.inbox(bob_phone)) == 1


async def test_send_shortcut_to_contact(api: Api, client: AsyncClient) -> None:
    alice, alice_phone, bob, bob_phone, _, _ = await _setup_sharing(api)
    await _befriend(api, alice, bob)
    r = await client.post(
        "/v1/send?to=@bob", content=b"https://x.example", headers=alice_phone.headers
    )
    expect(r, 201)
    assert [i["url"] for i in await api.inbox(bob_phone)] == ["https://x.example"]


async def test_disabled_contacts_are_unreachable(api: Api, client: AsyncClient) -> None:
    alice, alice_phone, bob, _, _, _ = await _setup_sharing(api)
    await _befriend(api, alice, bob)
    root = await api.user("root", admin=True)
    users = expect(await client.get("/v1/admin/users", headers=auth(root)), 200)
    bob_id = next(u["id"] for u in users if u["username"] == "bob")
    expect(
        await client.patch(
            f"/v1/admin/users/{bob_id}", json={"disabled": True}, headers=auth(root)
        ),
        200,
    )
    r = await api.send(alice_phone.headers, 422, kind="note", body="hi", to=["@bob"])
    assert r["error"]["code"] == "unknown_contact"

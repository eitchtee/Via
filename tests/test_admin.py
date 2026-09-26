from httpx import AsyncClient

from .conftest import PASSWORD, Api, auth, expect


async def _user_id(client: AsyncClient, admin: str, username: str) -> str:
    users = expect(await client.get("/v1/admin/users", headers=auth(admin)), 200)
    return str(next(u["id"] for u in users if u["username"] == username))


async def test_admin_only(api: Api, client: AsyncClient) -> None:
    user = await api.user("bob")
    phone = await api.device(user)
    for headers in (auth(user), phone.headers):
        expect(await client.get("/v1/admin/users", headers=headers), 403)
        expect(await client.get("/v1/admin/stats", headers=headers), 403)


async def test_list_users_with_usage(api: Api, client: AsyncClient) -> None:
    root = await api.user("root", admin=True)
    bob = await api.user("bob")
    phone = await api.device(bob, "phone")
    await api.device(bob, "laptop")
    await client.get("/v1/inbox", headers=phone.headers)  # marks the phone as seen
    r = await client.post(
        "/v1/pushes/file?filename=a.bin", content=b"x" * 1000, headers=phone.headers
    )
    expect(r, 201)

    users = {
        u["username"]: u
        for u in expect(await client.get("/v1/admin/users", headers=auth(root)), 200)
    }
    assert users["bob"]["devices"] == 2
    assert users["bob"]["stored_bytes"] == 1000
    assert users["bob"]["last_seen_at"] is not None
    assert users["root"]["is_admin"] and users["root"]["devices"] == 0

    stats = expect(await client.get("/v1/admin/stats", headers=auth(root)), 200)
    assert stats == {
        "users": 2,
        "devices": 2,
        "pushes": 1,
        "pending_deliveries": 1,
        "stored_bytes": 1000,
    }


async def test_create_disable_promote(api: Api, client: AsyncClient) -> None:
    root = await api.user("root", admin=True)
    r = await client.post(
        "/v1/admin/users",
        json={"username": "Carol", "password": "carol password"},
        headers=auth(root),
    )
    carol = expect(r, 201)
    assert carol["username"] == "carol" and not carol["is_admin"]
    session = await api.login("carol", "carol password")

    r = await client.patch(
        f"/v1/admin/users/{carol['id']}", json={"disabled": True}, headers=auth(root)
    )
    assert expect(r, 200)["disabled_at"] is not None
    expect(await client.get("/v1/account", headers=auth(session)), 401)
    r = await client.post(
        "/v1/auth/login", json={"username": "carol", "password": "carol password"}
    )
    expect(r, 401)

    r = await client.patch(
        f"/v1/admin/users/{carol['id']}",
        json={"disabled": False, "is_admin": True},
        headers=auth(root),
    )
    enabled = expect(r, 200)
    assert enabled["disabled_at"] is None and enabled["is_admin"]
    expect(await client.get("/v1/admin/users", headers=auth(session)), 200)


async def test_admins_cannot_lock_themselves_out(api: Api, client: AsyncClient) -> None:
    root = await api.user("root", admin=True)
    me = await _user_id(client, root, "root")
    for body in ({"disabled": True}, {"is_admin": False}):
        r = await client.patch(f"/v1/admin/users/{me}", json=body, headers=auth(root))
        assert expect(r, 409)["error"]["code"] == "cannot_modify_self"
    expect(await client.delete(f"/v1/admin/users/{me}", headers=auth(root)), 409)
    # A no-op change is fine.
    expect(
        await client.patch(f"/v1/admin/users/{me}", json={"is_admin": True}, headers=auth(root)),
        200,
    )


async def test_reset_password(api: Api, client: AsyncClient) -> None:
    root = await api.user("root", admin=True)
    bob = await api.user("bob")
    bob_id = await _user_id(client, root, "bob")
    r = await client.post(
        f"/v1/admin/users/{bob_id}/password", json={"password": "brand new pw"}, headers=auth(root)
    )
    expect(r, 204)
    expect(await client.get("/v1/account", headers=auth(bob)), 401)
    await api.login("bob", "brand new pw")
    r = await client.post("/v1/auth/login", json={"username": "bob", "password": PASSWORD})
    expect(r, 401)


async def test_delete_user(api: Api, client: AsyncClient) -> None:
    root = await api.user("root", admin=True)
    alice = await api.user("alice")
    alice_phone = await api.device(alice, "phone")
    bob = await api.user("bob")
    bob_phone = await api.device(bob, "phone")
    bob_laptop = await api.device(bob, "laptop")

    # Contacts, so alice can share with bob.
    request = expect(
        await client.post("/v1/contacts", json={"username": "bob"}, headers=auth(alice)), 201
    )
    expect(await client.post(f"/v1/contacts/{request['id']}/accept", headers=auth(bob)), 200)

    own_file = expect(
        await client.post(
            f"/v1/pushes/file?filename=f.bin&to={bob_laptop.id}",
            content=b"x" * 10,
            headers=bob_phone.headers,
        ),
        201,
    )
    shared = await api.send(alice_phone.headers, kind="note", body="for bob", to=["@bob"])
    assert api.ctx.blobs.path(own_file["id"]).exists()

    bob_id = await _user_id(client, root, "bob")
    expect(await client.delete(f"/v1/admin/users/{bob_id}", headers=auth(root)), 204)

    expect(await client.get("/v1/inbox", headers=bob_phone.headers), 401)
    assert not api.ctx.blobs.path(own_file["id"]).exists()
    # Alice's push to bob is purged right away instead of waiting to expire.
    status = expect(
        await client.get(f"/v1/pushes/{shared['id']}", headers=alice_phone.headers), 200
    )
    assert status["purged_at"] is not None
    assert expect(await client.get("/v1/contacts", headers=auth(alice)), 200) == []
    assert [
        u["username"] for u in expect(await client.get("/v1/admin/users", headers=auth(root)), 200)
    ] == [
        "alice",
        "root",
    ]

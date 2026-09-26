from httpx import AsyncClient

from via.config import Settings
from via.ratelimit import RateLimiter

from .conftest import PASSWORD, Api, auth, expect


async def test_health_and_info(client: AsyncClient) -> None:
    assert expect(await client.get("/v1/health"), 200) == {"status": "ok"}
    info = expect(await client.get("/v1/info"), 200)
    assert info["api_version"] == 1
    assert info["encryption"] == "server-v1"
    assert info["features"]["history"] is False
    assert info["limits"]["max_file_size"] == 100 * 1024**2


async def test_login(api: Api, client: AsyncClient) -> None:
    session = await api.user("Alice")
    account = expect(await client.get("/v1/account", headers=auth(session)), 200)
    assert account["username"] == "alice"
    assert account["usage"] == {"pending_bytes": 0, "quota": 1024**3}

    # Usernames are case-insensitive.
    await api.login("ALICE")
    for username, password in [("alice", "wrong password"), ("nobody", PASSWORD)]:
        r = await client.post("/v1/auth/login", json={"username": username, "password": password})
        assert expect(r, 401)["error"]["code"] == "invalid_credentials"


async def test_missing_and_bad_tokens(client: AsyncClient) -> None:
    assert expect(await client.get("/v1/account"), 401)["error"]["code"] == "unauthorized"
    r = await client.get("/v1/account", headers=auth("via_s_nope"))
    assert expect(r, 401)["error"]["code"] == "invalid_token"
    r = await client.get("/v1/account", headers=auth("garbage"))
    assert expect(r, 401)["error"]["code"] == "invalid_token"


async def test_logout(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    expect(await client.post("/v1/auth/logout", headers=auth(session)), 204)
    expect(await client.get("/v1/account", headers=auth(session)), 401)


async def test_change_password_signs_out_other_sessions(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    other = await api.login("alice")
    r = await client.patch(
        "/v1/account/password",
        json={"current_password": "wrong password", "new_password": "new password!"},
        headers=auth(session),
    )
    expect(r, 403)
    r = await client.patch(
        "/v1/account/password",
        json={"current_password": PASSWORD, "new_password": "new password!"},
        headers=auth(session),
    )
    expect(r, 204)
    expect(await client.get("/v1/account", headers=auth(session)), 200)
    expect(await client.get("/v1/account", headers=auth(other)), 401)
    await api.login("alice", "new password!")


async def test_signup_invite_mode(api: Api, client: AsyncClient) -> None:
    admin = await api.user("root", admin=True)
    user = await api.user("bob")
    body = {"username": "carol", "password": "carol's password"}

    r = await client.post("/v1/auth/register", json=body)
    assert expect(r, 403)["error"]["code"] == "invite_required"
    expect(await client.post("/v1/admin/invites", json={}, headers=auth(user)), 403)

    invite = expect(await client.post("/v1/admin/invites", json={}, headers=auth(admin)), 201)
    listed = expect(await client.get("/v1/admin/invites", headers=auth(admin)), 200)
    assert [i["id"] for i in listed] == [invite["id"]]

    r = await client.post("/v1/auth/register", json={**body, "invite": invite["code"]})
    session = expect(r, 201)
    assert session["user"]["username"] == "carol"

    # Invites are single use.
    r = await client.post(
        "/v1/auth/register", json={**body, "username": "dave", "invite": invite["code"]}
    )
    assert expect(r, 403)["error"]["code"] == "invalid_invite"
    assert expect(await client.get("/v1/admin/invites", headers=auth(admin)), 200) == []


async def test_signup_open_and_closed(api: Api, client: AsyncClient, settings: Settings) -> None:
    body = {"username": "carol", "password": "carol's password"}
    settings.signup = "closed"
    assert expect(await client.post("/v1/auth/register", json=body), 403)["error"]["code"] == (
        "signup_closed"
    )
    settings.signup = "open"
    expect(await client.post("/v1/auth/register", json=body), 201)
    r = await client.post("/v1/auth/register", json={**body, "username": "CAROL"})
    assert expect(r, 409)["error"]["code"] == "username_taken"
    r = await client.post("/v1/auth/register", json={**body, "username": "no spaces"})
    assert expect(r, 422)["error"]["code"] == "invalid_username"


async def test_login_rate_limit(api: Api, client: AsyncClient) -> None:
    api.ctx.login_limiter = RateLimiter(per_minute=3)
    for _ in range(3):
        await client.post("/v1/auth/login", json={"username": "x", "password": "y"})
    r = await client.post("/v1/auth/login", json={"username": "x", "password": "y"})
    assert expect(r, 429)["error"]["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1


async def test_app_tokens(api: Api, client: AsyncClient) -> None:
    session = await api.user()
    phone = await api.device(session)
    created = expect(
        await client.post("/v1/app-tokens", json={"name": "ci"}, headers=auth(session)), 201
    )
    assert created["token"].startswith("via_a_")
    listed = expect(await client.get("/v1/app-tokens", headers=auth(session)), 200)
    assert [t["name"] for t in listed] == ["ci"]
    # Only sessions manage app tokens.
    expect(await client.get("/v1/app-tokens", headers=phone.headers), 403)

    expect(
        await client.delete(f"/v1/app-tokens/{created['app_token']['id']}", headers=auth(session)),
        204,
    )
    expect(await client.get("/v1/devices", headers=auth(created["token"])), 401)

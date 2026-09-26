"""Authorization matrix: every API endpoint against every kind of caller.

Each case gets fresh credentials, so destructive endpoints (logout, token rotation…) can't
affect the others. Allowed callers must get past authorization (any status but 401/403);
others get 403, and requests without a token get 401. Ids point at nothing, so allowed calls
end in 404 instead of changing data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from via import security
from via.context import Context
from via.db.models import AppToken, User
from via.services import accounts, devices

from .conftest import PASSWORD, auth

NONE = "none"
SESSION = "session"
ADMIN = "admin"
DEVICE = "device"
APP = "app"
CALLERS = (NONE, SESSION, ADMIN, DEVICE, APP)

ANY = {SESSION, ADMIN, DEVICE, APP}
OWNER = {SESSION, ADMIN}  # password sessions
ADMINS = {ADMIN}
DEVICES = {DEVICE}
PUBLIC = {NONE, *ANY}

MISSING = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"


@dataclass
class Case:
    allowed: set[str]
    path: str | None = None  # concrete path, if the route has parameters
    kwargs: dict[str, Any] = field(default_factory=dict)
    streams: bool = False  # endless response (SSE): only check rejected callers


NOTE = {"json": {"kind": "note", "body": "hi"}}

MATRIX: dict[tuple[str, str], Case] = {
    # meta & auth
    ("GET", "/v1/health"): Case(PUBLIC),
    ("GET", "/v1/info"): Case(PUBLIC),
    ("POST", "/v1/auth/login"): Case(
        PUBLIC, kwargs={"json": {"username": "alice", "password": PASSWORD}}
    ),
    ("POST", "/v1/auth/register"): Case(
        PUBLIC, kwargs={"json": {"username": "newbie", "password": "password1"}}
    ),
    ("POST", "/v1/auth/logout"): Case(OWNER),
    # account
    ("GET", "/v1/account"): Case(OWNER),
    ("PATCH", "/v1/account/password"): Case(
        OWNER, kwargs={"json": {"current_password": PASSWORD, "new_password": PASSWORD}}
    ),
    # admin
    ("GET", "/v1/admin/stats"): Case(ADMINS),
    ("GET", "/v1/admin/users"): Case(ADMINS),
    ("POST", "/v1/admin/users"): Case(
        ADMINS, kwargs={"json": {"username": "created", "password": "password1"}}
    ),
    ("PATCH", "/v1/admin/users/{user_id}"): Case(
        ADMINS, f"/v1/admin/users/{MISSING}", {"json": {"disabled": True}}
    ),
    ("POST", "/v1/admin/users/{user_id}/password"): Case(
        ADMINS, f"/v1/admin/users/{MISSING}/password", {"json": {"password": "password1"}}
    ),
    ("DELETE", "/v1/admin/users/{user_id}"): Case(ADMINS, f"/v1/admin/users/{MISSING}"),
    ("POST", "/v1/admin/invites"): Case(ADMINS, kwargs={"json": {}}),
    ("GET", "/v1/admin/invites"): Case(ADMINS),
    ("DELETE", "/v1/admin/invites/{invite_id}"): Case(ADMINS, f"/v1/admin/invites/{MISSING}"),
    # devices
    ("POST", "/v1/devices"): Case(OWNER, kwargs={"json": {"name": "new"}}),
    ("GET", "/v1/devices"): Case(ANY),
    ("GET", "/v1/devices/me"): Case(DEVICES),
    ("PATCH", "/v1/devices/me"): Case(DEVICES, kwargs={"json": {"name": "renamed"}}),
    ("PUT", "/v1/devices/me/push"): Case(DEVICES, kwargs={"json": {"provider": "none"}}),
    ("POST", "/v1/devices/me/rotate-token"): Case(DEVICES),
    ("PATCH", "/v1/devices/{device_id}"): Case(
        OWNER, f"/v1/devices/{MISSING}", {"json": {"name": "x"}}
    ),
    ("DELETE", "/v1/devices/{device_id}"): Case(OWNER, f"/v1/devices/{MISSING}"),
    # contacts
    ("GET", "/v1/contacts"): Case(ANY),
    ("POST", "/v1/contacts"): Case(OWNER, kwargs={"json": {"username": "nobody"}}),
    ("POST", "/v1/contacts/{contact_id}/accept"): Case(OWNER, f"/v1/contacts/{MISSING}/accept"),
    ("DELETE", "/v1/contacts/{contact_id}"): Case(OWNER, f"/v1/contacts/{MISSING}"),
    # app tokens
    ("GET", "/v1/app-tokens"): Case(OWNER),
    ("POST", "/v1/app-tokens"): Case(OWNER, kwargs={"json": {"name": "t"}}),
    ("DELETE", "/v1/app-tokens/{token_id}"): Case(OWNER, f"/v1/app-tokens/{MISSING}"),
    # sending
    ("POST", "/v1/pushes"): Case(ANY, kwargs=NOTE),
    ("POST", "/v1/pushes/file"): Case(ANY, "/v1/pushes/file?filename=a.txt", {"content": b"file"}),
    ("GET", "/v1/pushes/sent"): Case(ANY),
    ("GET", "/v1/pushes/{push_id}"): Case(ANY, f"/v1/pushes/{MISSING}"),
    ("DELETE", "/v1/pushes/{push_id}"): Case(ANY, f"/v1/pushes/{MISSING}"),
    ("GET", "/v1/pushes/history"): Case(DEVICES),
    ("GET", "/v1/pushes/history/{push_id}/file"): Case(
        DEVICES, f"/v1/pushes/history/{MISSING}/file"
    ),
    ("DELETE", "/v1/pushes/history/{push_id}"): Case(DEVICES, f"/v1/pushes/history/{MISSING}"),
    ("POST", "/v1/send"): Case(ANY, kwargs={"content": b"hello"}),
    ("PUT", "/v1/send"): Case(ANY, kwargs={"content": b"hello"}),
    ("POST", "/v1/send/{filename}"): Case(ANY, "/v1/send/a.txt", {"content": b"file"}),
    ("PUT", "/v1/send/{filename}"): Case(ANY, "/v1/send/a.txt", {"content": b"file"}),
    # receiving: device tokens only
    ("GET", "/v1/inbox"): Case(DEVICES),
    ("GET", "/v1/inbox/events"): Case(DEVICES, streams=True),
    ("POST", "/v1/inbox/ack"): Case(DEVICES, kwargs={"json": {"ids": [MISSING]}}),
    ("GET", "/v1/inbox/{push_id}"): Case(DEVICES, f"/v1/inbox/{MISSING}"),
    ("GET", "/v1/inbox/{push_id}/file"): Case(DEVICES, f"/v1/inbox/{MISSING}/file"),
    ("POST", "/v1/inbox/{push_id}/ack"): Case(DEVICES, f"/v1/inbox/{MISSING}/ack"),
}


def test_matrix_covers_every_endpoint(app: FastAPI) -> None:
    documented = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    assert documented == set(MATRIX), "add new endpoints to MATRIX (or remove stale ones)"


async def _credentials(ctx: Context, alice: User, root: User) -> dict[str, dict[str, str]]:
    """Fresh tokens of every kind (alice is a regular user, root an admin)."""
    async with ctx.sessionmaker() as db:
        session, _ = await accounts.create_session(db, alice, 3600, None)
        admin, _ = await accounts.create_session(db, root, 3600, None)
        device, _ = await devices.register_device(db, alice, "phone", "android")
        app_token = security.new_token(security.APP)
        db.add(AppToken(user_id=alice.id, name="t", token_hash=security.hash_token(app_token)))
        await db.commit()
    return {
        NONE: {},
        SESSION: auth(session),
        ADMIN: auth(admin),
        DEVICE: auth(device),
        APP: auth(app_token),
    }


@pytest.fixture
async def users(ctx: Context) -> tuple[User, User]:
    async with ctx.sessionmaker() as db:
        alice = await accounts.create_user(db, "alice", PASSWORD)
        root = await accounts.create_user(db, "root", PASSWORD, is_admin=True)
        await db.commit()
    return alice, root


async def test_authorization_matrix(
    ctx: Context, client: AsyncClient, users: tuple[User, User]
) -> None:
    ctx.settings.signup = "open"  # so /auth/register's outcome depends on auth alone
    failures = []
    for (method, route), case in MATRIX.items():
        for caller in CALLERS:
            allowed = caller in case.allowed
            if case.streams and allowed:
                continue
            headers = (await _credentials(ctx, *users))[caller]
            r = await client.request(method, case.path or route, headers=headers, **case.kwargs)
            if allowed:
                ok = r.status_code not in (401, 403)
            elif caller == NONE:
                ok = r.status_code == 401
            else:
                ok = r.status_code == 403
            if not ok:
                expected = "allowed" if allowed else ("401" if caller == NONE else "403")
                failures.append(f"{method} {route} as {caller}: {r.status_code}, want {expected}")
    assert not failures, "\n".join(failures)

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response

from via.config import Settings
from via.context import Context
from via.crypto import generate_key
from via.main import create_app
from via.services import accounts

PASSWORD = "correct horse battery"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        master_key=generate_key(),
        janitor_interval=0,
        login_rate_per_minute=0,
        push_rate_per_minute=0,
    )


@pytest.fixture
async def app(settings: Settings) -> AsyncIterator[FastAPI]:
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
def ctx(app: FastAPI) -> Context:
    ctx: Context = app.state.ctx
    return ctx


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://via") as c:
        yield c


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def expect(response: Response, status: int) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@dataclass
class Device:
    id: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        return auth(self.token)


class Api:
    """Test helpers for common flows."""

    def __init__(self, client: AsyncClient, ctx: Context) -> None:
        self.client = client
        self.ctx = ctx

    async def user(self, username: str = "alice", *, admin: bool = False) -> str:
        """Create a user and return a session token."""
        async with self.ctx.sessionmaker() as db:
            await accounts.create_user(db, username, PASSWORD, is_admin=admin)
            await db.commit()
        return await self.login(username)

    async def login(self, username: str, password: str = PASSWORD) -> str:
        body = expect(
            await self.client.post(
                "/v1/auth/login", json={"username": username, "password": password}
            ),
            200,
        )
        token: str = body["token"]
        return token

    async def device(self, session: str, name: str = "phone", type_: str = "android") -> Device:
        body = expect(
            await self.client.post(
                "/v1/devices", json={"name": name, "type": type_}, headers=auth(session)
            ),
            201,
        )
        return Device(body["device"]["id"], body["token"])

    async def send(self, sender: dict[str, str], status: int = 201, **payload: Any) -> Any:
        return expect(await self.client.post("/v1/pushes", json=payload, headers=sender), status)

    async def inbox(self, device: Device, **params: Any) -> list[dict[str, Any]]:
        body = expect(
            await self.client.get("/v1/inbox", params=params, headers=device.headers), 200
        )
        items: list[dict[str, Any]] = body["items"]
        return items

    async def ack(self, device: Device, push_id: str, status: int = 200) -> Any:
        return expect(
            await self.client.post(f"/v1/inbox/{push_id}/ack", headers=device.headers), status
        )


@pytest.fixture
def api(client: AsyncClient, ctx: Context) -> Api:
    return Api(client, ctx)

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from via.cli import main as cli
from via.config import Settings
from via.context import Context
from via.crypto import DataKey, DecryptionError, MasterKey
from via.db.models import Push
from via.lock import DataDirLock, DataDirLocked
from via.main import create_app

from .conftest import Api, expect


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # No VIA_MASTER_KEY: use the generated <data>/master.key, like a default install.
    monkeypatch.setenv("VIA_DATA_DIR", str(tmp_path))  # for the CLI
    return Settings(data_dir=tmp_path, janitor_interval=0, login_rate_per_minute=0)


@asynccontextmanager
async def running(settings: Settings) -> AsyncIterator[Api]:
    app: FastAPI = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://via") as client,
    ):
        ctx: Context = app.state.ctx
        yield Api(client, ctx)


def rotate() -> None:
    cli(["rotate-master-key"])


async def test_rotate_master_key(settings: Settings) -> None:
    async with running(settings) as api:
        session = await api.user()
        phone = await api.device(session, "phone")
        laptop = await api.device(session, "laptop")
        expect(
            await api.client.put(
                "/v1/devices/me/push",
                json={"provider": "fcm_relay", "token": "fcm-token"},
                headers=laptop.headers,
            ),
            200,
        )
        note = await api.send(phone.headers, kind="note", body="survives rotation")
        file = expect(
            await api.client.post(
                "/v1/pushes/file?filename=a.txt", content=b"file body", headers=phone.headers
            ),
            201,
        )
    old_key = (settings.data_dir / "master.key").read_text()

    # The CLI runs its own event loop, so run it in a thread.
    await asyncio.to_thread(rotate)

    new_key = (settings.data_dir / "master.key").read_text()
    assert new_key != old_key
    assert not (settings.data_dir / "master.key.new").exists()

    async with running(settings) as api:
        items = {i["id"]: i for i in await api.inbox(laptop)}
        assert items[note["id"]]["body"] == "survives rotation"
        r = await api.client.get(f"/v1/inbox/{file['id']}/file", headers=laptop.headers)
        assert r.content == b"file body"

        # The old key can't unwrap anything anymore.
        async with api.ctx.sessionmaker() as db:
            push = await db.get(Push, note["id"])
            assert push is not None and push.wrapped_key is not None
            with pytest.raises(DecryptionError):
                DataKey.unwrap(MasterKey.from_b64(old_key), push.wrapped_key, push.id)


async def test_rotation_is_refused_while_the_server_runs(settings: Settings) -> None:
    async with running(settings):
        key = (settings.data_dir / "master.key").read_text()
        with pytest.raises(SystemExit, match="server is running"):
            await asyncio.to_thread(rotate)
    assert (settings.data_dir / "master.key").read_text() == key
    assert not (settings.data_dir / "master.key.new").exists()


async def test_rotation_refuses_to_overwrite_staged_key(settings: Settings) -> None:
    MasterKey.load(None, None, settings.data_dir)
    (settings.data_dir / "master.key.new").write_text("leftover")
    with pytest.raises(SystemExit, match="interrupted rotation"):
        rotate()


async def test_one_server_per_data_dir(settings: Settings) -> None:
    async with running(settings):
        with pytest.raises(RuntimeError, match="in use by a running Via server"):
            async with running(settings):
                pass
    # Once the first one stops, the directory is free again.
    async with running(settings):
        pass


def test_lock(tmp_path: Path) -> None:
    with DataDirLock(tmp_path), pytest.raises(DataDirLocked):
        DataDirLock(tmp_path).acquire()
    with DataDirLock(tmp_path):
        pass

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from via import __version__
from via.api.v1 import router as v1_router
from via.config import Settings
from via.context import Context
from via.crypto import MasterKey
from via.db.engine import make_engine, make_sessionmaker, upgrade
from via.errors import install_error_handlers
from via.lock import DataDirLock, DataDirLocked
from via.notify.dispatcher import Dispatcher
from via.notify.hub import EventHub
from via.ratelimit import RateLimiter
from via.services import accounts, janitor
from via.storage import BlobStore
from via.web import SecurityHeaders, mount_web_ui

log = logging.getLogger("via")

DESCRIPTION = """\
Send links, notes and files between your devices through your own server.

Authenticate with `Authorization: Bearer <token>`. Tokens are prefixed by type:
`via_s_…` (session: account and device management), `via_d_…` (device: its own inbox, sending)
and `via_a_…` (app: send only).
"""


@asynccontextmanager
async def _running(app: FastAPI, settings: Settings) -> AsyncIterator[None]:
    await asyncio.to_thread(upgrade, settings.db_path)
    engine = make_engine(settings.db_path)
    sessionmaker = make_sessionmaker(engine)
    master = MasterKey.load(settings.master_key, settings.master_key_file, settings.data_dir)
    hub = EventHub()
    ctx = Context(
        settings=settings,
        master=master,
        blobs=BlobStore(settings.data_dir / "blobs"),
        sessionmaker=sessionmaker,
        hub=hub,
        dispatcher=Dispatcher(
            hub, master, sessionmaker, settings.push_relay_url, settings.push_relay_key
        ),
        login_limiter=RateLimiter(settings.login_rate_per_minute),
        push_limiter=RateLimiter(settings.push_rate_per_minute),
    )
    app.state.ctx = ctx
    await accounts.bootstrap_admin(ctx)
    janitor_task = (
        asyncio.create_task(janitor.run_forever(ctx)) if settings.janitor_interval > 0 else None
    )
    log.info("Via %s ready (data dir: %s)", __version__, settings.data_dir.resolve())
    try:
        yield
    finally:
        if janitor_task:
            janitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await janitor_task
        hub.close()
        await ctx.dispatcher.aclose()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        lock = DataDirLock(settings.data_dir)
        try:
            lock.acquire()
        except DataDirLocked as e:
            raise RuntimeError(f"Can't start: {e}") from None
        try:
            async with _running(app, settings):
                yield
        finally:
            lock.release()

    app = FastAPI(
        title="Via",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        license_info={"name": "AGPL-3.0-or-later", "identifier": "AGPL-3.0-or-later"},
    )
    install_error_handlers(app)
    app.add_middleware(SecurityHeaders)
    app.include_router(v1_router)
    if settings.web_ui:
        mount_web_ui(app)  # last: it catches every path the API doesn't
    return app

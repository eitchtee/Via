"""Wakes devices up when something arrives in their inbox.

Wake-ups never carry content: at most the push id and kind. The device then fetches over
the authenticated API. Channels:

* the in-process hub (SSE streams and long-polls), always;
* UnifiedPush: POST to the endpoint the device registered;
* FCM: POST to the Via push relay, which holds the Firebase credentials of the official app.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import httpx
from sqlalchemy import update

from via import __version__
from via.crypto import DecryptionError, MasterKey
from via.db.engine import SessionMaker
from via.db.models import (
    PROVIDER_FCM_RELAY,
    PROVIDER_NONE,
    PROVIDER_UNIFIEDPUSH,
    Device,
)
from via.notify.hub import EventHub

log = logging.getLogger(__name__)


def push_target_aad(device_id: str) -> bytes:
    return b"via/push-target/" + device_id.encode()


class Dispatcher:
    def __init__(
        self,
        hub: EventHub,
        master: MasterKey,
        sessionmaker: SessionMaker,
        relay_url: str,
        relay_key: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.hub = hub
        self.master = master
        self.sessionmaker = sessionmaker
        self.relay_url = relay_url.rstrip("/")
        self.relay_headers = {"X-Via-Instance-Key": relay_key} if relay_key else {}
        self.http = http or httpx.AsyncClient(
            timeout=10, headers={"User-Agent": f"via-server/{__version__}"}
        )
        self._tasks: set[asyncio.Task[None]] = set()

    def notify(self, device: Device, push_id: str, kind: str) -> None:
        self.hub.publish(device.id, {"type": "push", "id": push_id, "kind": kind})
        if (
            device.push_provider not in (PROVIDER_UNIFIEDPUSH, PROVIDER_FCM_RELAY)
            or device.push_target is None
        ):
            return
        try:
            target = self.master.open(device.push_target, push_target_aad(device.id)).decode()
        except DecryptionError:
            log.error("cannot decrypt push target of device %s", device.id)
            return
        payload = {"t": "wake", "id": push_id}
        if device.push_provider == PROVIDER_UNIFIEDPUSH:
            self._spawn(self._post(device.id, PROVIDER_UNIFIEDPUSH, target, payload))
        elif self.relay_url:
            relay_payload = {"token": target, "id": push_id}
            url = f"{self.relay_url}/v1/wake"
            self._spawn(
                self._post(device.id, PROVIDER_FCM_RELAY, url, relay_payload, self.relay_headers)
            )

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(
        self,
        device_id: str,
        provider: str,
        url: str,
        payload: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> None:
        try:
            response = await self.http.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            log.warning("%s wake-up for device %s failed: %s", provider, device_id, e)
            return
        if response.status_code in (404, 410):
            # The endpoint or FCM token no longer exists: stop using it.
            log.info("%s target of device %s is gone; clearing it", provider, device_id)
            await self._clear(device_id, provider)
        elif response.is_error:
            log.warning(
                "%s wake-up for device %s returned %s", provider, device_id, response.status_code
            )

    async def _clear(self, device_id: str, provider: str) -> None:
        async with self.sessionmaker() as db:
            await db.execute(
                update(Device)
                .where(Device.id == device_id, Device.push_provider == provider)
                .values(push_provider=PROVIDER_NONE, push_target=None)
            )
            await db.commit()

    async def drain(self) -> None:
        """Wait for in-flight wake-ups (used by tests and at shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def aclose(self) -> None:
        try:
            await asyncio.wait_for(self.drain(), 5)
        except TimeoutError:
            for task in self._tasks:
                task.cancel()
        await self.http.aclose()

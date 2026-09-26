"""In-process event hub feeding SSE streams and long-polls.

Events are hints ("go look at your inbox"), never content, so dropping one when a
subscriber's queue is full is harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

Event = dict[str, Any]
_QUEUE_SIZE = 64


class EventHub:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = {}

    @contextmanager
    def subscribe(self, device_id: str) -> Iterator[asyncio.Queue[Event]]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subs.setdefault(device_id, set()).add(queue)
        try:
            yield queue
        finally:
            subs = self._subs.get(device_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    del self._subs[device_id]

    def publish(self, device_id: str, event: Event) -> None:
        for queue in list(self._subs.get(device_id, ())):
            _put(queue, event)

    def is_connected(self, device_id: str) -> bool:
        return bool(self._subs.get(device_id))

    def close(self) -> None:
        """Tell every subscriber to stop (server shutdown)."""
        for subs in self._subs.values():
            for queue in subs:
                _put(queue, {"type": "shutdown"}, force=True)


def _put(queue: asyncio.Queue[Event], event: Event, *, force: bool = False) -> None:
    if force and queue.full():
        queue.get_nowait()
    with contextlib.suppress(asyncio.QueueFull):
        queue.put_nowait(event)


async def sse_events(hub: EventHub, device_id: str, heartbeat: float) -> AsyncIterator[str]:
    """Server-sent event stream for one device.

    Sends ``ready`` once subscribed (clients should then fetch their inbox), ``push`` and
    ``recalled`` events as they happen, and a comment line every ``heartbeat`` seconds.
    """
    with hub.subscribe(device_id) as queue:
        yield "retry: 5000\nevent: ready\ndata: {}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), heartbeat)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event["type"] == "shutdown":
                return
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
            if event["type"] == "revoked":
                return

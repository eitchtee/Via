"""Long-lived server state shared by all requests."""

from __future__ import annotations

from dataclasses import dataclass

from via.config import Settings
from via.crypto import MasterKey
from via.db.engine import SessionMaker
from via.notify.dispatcher import Dispatcher
from via.notify.hub import EventHub
from via.ratelimit import RateLimiter
from via.storage import BlobStore


@dataclass
class Context:
    settings: Settings
    master: MasterKey
    blobs: BlobStore
    sessionmaker: SessionMaker
    hub: EventHub
    dispatcher: Dispatcher
    login_limiter: RateLimiter
    push_limiter: RateLimiter

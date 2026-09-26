"""In-process token-bucket rate limiter (Via runs as a single process)."""

from __future__ import annotations

import math
import time

from via.errors import APIError

_MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self, per_minute: int, burst: int | None = None) -> None:
        self.rate = per_minute / 60
        self.capacity = float(burst or per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}

    def check(self, key: str) -> None:
        """Consume one token for ``key`` or raise a 429 error. A rate of 0 disables limiting."""
        if self.rate <= 0:
            return
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens < 1:
            retry_after = math.ceil((1 - tokens) / self.rate)
            raise APIError(
                429,
                "rate_limited",
                "Too many requests, try again later",
                headers={"Retry-After": str(retry_after)},
            )
        self._buckets[key] = (tokens - 1, now)
        if len(self._buckets) > _MAX_KEYS:
            self._prune(now)

    def _prune(self, now: float) -> None:
        full_after = self.capacity / self.rate
        self._buckets = {k: v for k, v in self._buckets.items() if now - v[1] < full_after}

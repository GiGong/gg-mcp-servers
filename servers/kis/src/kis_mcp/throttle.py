"""Throttling that respects the KIS request-rate limit.

KIS limits requests per second per app key (20/s live, 5/s paper). Calls are
serialised through a single lock so that a burst of concurrent tool invocations
still cannot exceed the limit.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Enforces a minimum interval between calls.

    This is a "1/rate seconds since the previous call" limiter rather than a
    sliding window. It gives up burst capacity in exchange for never exceeding
    the counter on the KIS side.
    """

    def __init__(self, per_second: float) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be > 0")
        self._min_interval = 1.0 / per_second
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    @property
    def min_interval(self) -> float:
        return self._min_interval

    async def acquire(self) -> float:
        """Wait for a slot. Returns how many seconds were spent waiting."""
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            else:
                wait = 0.0
            self._next_allowed = now + self._min_interval
            return wait

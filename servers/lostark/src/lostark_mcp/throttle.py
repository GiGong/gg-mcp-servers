"""Throttling against the Lost Ark quota of 100 requests per minute per key.

Two counters are combined:

  - A local sliding window keeps this process under its configured share.
  - The server's X-RateLimit-* headers cover everything else spending the same
    key, such as `lostark-mcp collect` running in another process.

`reserve` makes a caller stop while the server reports that many requests or
fewer remaining, so a background collector leaves room for interactive use.
"""

from __future__ import annotations

import asyncio
import collections
import time
from typing import Any

from .fields import epoch_to_kst

WINDOW_SEC = 60.0


class QuotaExhausted(RuntimeError):
    def __init__(self, wait: float) -> None:
        super().__init__(f"quota exhausted; next slot in {wait:.1f}s")
        self.wait = wait


class MinuteQuota:
    def __init__(self, per_minute: int, *, max_wait: float, reserve: int = 0) -> None:
        if per_minute <= 0:
            raise ValueError("per_minute must be > 0")
        self._per_minute = per_minute
        self._max_wait = max_wait
        self._reserve = reserve
        self._stamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()
        self._limit: int | None = None
        self._remaining: int | None = None
        self._reset_epoch: float | None = None

    def observe(self, limit: Any, remaining: Any, reset_epoch: Any) -> None:
        """Record the server's view from response headers."""
        try:
            self._limit = int(limit) if limit is not None else self._limit
            self._remaining = int(remaining) if remaining is not None else self._remaining
            self._reset_epoch = float(reset_epoch) if reset_epoch is not None else self._reset_epoch
        except (TypeError, ValueError):
            pass

    def server_wait(self) -> float:
        """Seconds until the server's window resets, or 0."""
        if self._reset_epoch is None:
            return 0.0
        return max(0.0, self._reset_epoch - time.time())

    def snapshot(self) -> dict[str, Any] | None:
        if self._remaining is None:
            return None
        return {
            "limit_per_min": self._limit,
            "remaining": self._remaining,
            "resets_at_kst": epoch_to_kst(self._reset_epoch),
        }

    def _wait_needed(self) -> float:
        now = time.monotonic()
        while self._stamps and now - self._stamps[0] >= WINDOW_SEC:
            self._stamps.popleft()
        local = 0.0
        if len(self._stamps) >= self._per_minute:
            local = self._stamps[0] + WINDOW_SEC - now

        server = 0.0
        if self._remaining is not None and self._remaining <= self._reserve:
            server = self.server_wait()
            if server == 0.0:
                # The server's window has rolled over; its count is stale.
                self._remaining = None
        return max(local, server)

    async def acquire(self) -> float:
        """Wait for a slot. Raises QuotaExhausted rather than wait past max_wait."""
        async with self._lock:
            wait = self._wait_needed()
            if wait > self._max_wait:
                raise QuotaExhausted(wait)
            if wait > 0:
                await asyncio.sleep(wait)
                self._wait_needed()
            self._stamps.append(time.monotonic())
            if self._remaining is not None:
                # Optimistic until the response headers arrive.
                self._remaining -= 1
            return wait

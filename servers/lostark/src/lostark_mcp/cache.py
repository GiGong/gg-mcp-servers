"""In-memory TTL cache with forced refresh.

Every tool takes `refresh=True` to bypass the cache. A refresh of an entry
fetched less than `min_refresh_sec` ago is served from cache and flagged, so a
model that refreshes on every turn cannot burn the per-minute quota.

Concurrent requests for the same key share one fetch.
"""

from __future__ import annotations

import asyncio
import collections
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .fields import epoch_to_kst

# Time-to-live per kind of data, in seconds.
TTL_ARMORY = 180
TTL_ROSTER = 300
TTL_MARKET_SEARCH = 60
TTL_MARKET_HISTORY = 600
TTL_AUCTION_SEARCH = 30
TTL_GEM_BOARD = 120
TTL_OPTIONS = 86_400


@dataclass(frozen=True)
class Fetched:
    value: Any
    hit: bool
    age_sec: float
    fetched_epoch: float
    refresh_suppressed: bool = False

    def meta(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hit": self.hit,
            "age_sec": int(self.age_sec),
            "fetched_at_kst": epoch_to_kst(self.fetched_epoch),
        }
        if self.refresh_suppressed:
            out["refresh_suppressed"] = (
                "Fetched moments ago; served from cache to protect the per-minute quota."
            )
        return out


def merged_meta(items: list[Fetched]) -> dict[str, Any]:
    """Cache metadata for a response assembled from several fetches."""
    if not items:
        return {}
    oldest = min(items, key=lambda f: f.fetched_epoch)
    out: dict[str, Any] = {
        "hit": all(f.hit for f in items),
        "age_sec": int(max(f.age_sec for f in items)),
        "fetched_at_kst": epoch_to_kst(oldest.fetched_epoch),
    }
    if any(f.refresh_suppressed for f in items):
        out["refresh_suppressed"] = (
            "Some parts were fetched moments ago; served from cache to protect the per-minute quota."
        )
    return out


@dataclass
class _Entry:
    value: Any
    monotonic: float
    epoch: float


class TtlCache:
    def __init__(self, *, max_entries: int = 1024, min_refresh_sec: float = 10.0) -> None:
        self._entries: collections.OrderedDict[str, _Entry] = collections.OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._max_entries = max_entries
        self._min_refresh_sec = min_refresh_sec

    async def get(
        self,
        key: str,
        ttl: float,
        fetch: Callable[[], Awaitable[Any]],
        *,
        refresh: bool = False,
    ) -> Fetched:
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None:
                age = time.monotonic() - entry.monotonic
                if refresh and age < self._min_refresh_sec:
                    return Fetched(entry.value, True, age, entry.epoch, refresh_suppressed=True)
                if not refresh and age < ttl:
                    self._entries.move_to_end(key)
                    return Fetched(entry.value, True, age, entry.epoch)

            value = await fetch()
            entry = _Entry(value, time.monotonic(), time.time())
            self._entries[key] = entry
            self._entries.move_to_end(key)
            self._evict()
            return Fetched(value, False, 0.0, entry.epoch)

    def _evict(self) -> None:
        while len(self._entries) > self._max_entries:
            key, _ = self._entries.popitem(last=False)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                del self._locks[key]

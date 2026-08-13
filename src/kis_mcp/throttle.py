"""KIS 유량 제한 대응 스로틀러.

KIS 는 앱키 단위로 초당 요청 수를 제한한다 (실전 20건/초, 모의 5건/초).
동시 호출이 몰려도 최소 간격이 보장되도록 단일 락 기반 큐로 직렬화한다.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """초당 N건을 넘지 않도록 호출 간 최소 간격을 강제한다.

    슬라이딩 윈도우가 아니라 '직전 호출로부터 1/rate 초' 방식이다.
    버스트를 허용하지 않는 대신 KIS 쪽 카운터를 절대 넘지 않는다.
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
        """슬롯을 얻을 때까지 대기한다. 실제로 대기한 초를 돌려준다."""
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

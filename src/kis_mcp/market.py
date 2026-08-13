"""한국 시장 시간대/세션 판정.

모든 MCP 응답에 KST 타임스탬프와 "지금 장이 열려 있는가"를 붙이기 위한 계층.
휴장일은 추측하지 않고 KIS 국내휴장일조회(CTCA0903R) 결과를 캐시해서 쓴다.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any
from zoneinfo import ZoneInfo

from .client import KisClient, KisError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

HOLIDAY_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
HOLIDAY_TR_ID = "CTCA0903R"

# KRX 정규장 09:00–15:30, 장전 동시호가 08:30–09:00, 시간외 15:40–18:00.
_PRE_OPEN = dt.time(8, 30)
_OPEN = dt.time(9, 0)
_CLOSE = dt.time(15, 30)
_AFTER_HOURS_END = dt.time(18, 0)


def now_kst() -> dt.datetime:
    return dt.datetime.now(tz=KST)


def kst_iso(moment: dt.datetime | None = None) -> str:
    return (moment or now_kst()).isoformat(timespec="seconds")


class MarketCalendar:
    """휴장일 조회 결과를 날짜 단위로 캐시한다."""

    def __init__(self, client: KisClient) -> None:
        self._client = client
        self._cache: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def is_open_day(self, day: dt.date) -> bool | None:
        """개장일이면 True, 휴장일이면 False, 조회 실패 시 None."""
        key = day.strftime("%Y%m%d")
        if key in self._cache:
            return self._cache[key]

        async with self._lock:
            if key in self._cache:
                return self._cache[key]
            try:
                data = await self._client.get(
                    HOLIDAY_PATH,
                    tr_id=HOLIDAY_TR_ID,
                    params={"BASS_DT": key, "CTX_AREA_FK": "", "CTX_AREA_NK": ""},
                )
            except KisError as exc:
                # 휴장일 조회는 부가정보다. 실패해도 본 도구는 계속 동작해야 한다.
                logger.warning("holiday lookup failed for %s: %s", key, exc)
                return None

            rows = data.get("output") or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                bass_dt = str(row.get("bass_dt", "")).strip()
                opnd = str(row.get("opnd_yn", "")).strip().upper()
                if bass_dt and opnd in {"Y", "N"}:
                    self._cache[bass_dt] = opnd == "Y"
            return self._cache.get(key)

    async def session(self, moment: dt.datetime | None = None) -> dict[str, Any]:
        moment = moment or now_kst()
        clock = moment.timetz().replace(tzinfo=None)
        weekday = moment.weekday()

        open_day = await self.is_open_day(moment.date())
        if open_day is None:
            # KIS 조회 실패 시 주말만 확실히 걸러내고 나머지는 unknown 으로 남긴다.
            trading_day = weekday < 5
            day_source = "weekday-heuristic (KIS holiday lookup unavailable)"
        else:
            trading_day = open_day
            day_source = "KIS chk-holiday (CTCA0903R)"

        if not trading_day:
            phase = "holiday" if weekday < 5 else "weekend"
        elif clock < _PRE_OPEN:
            phase = "before_pre_open"
        elif clock < _OPEN:
            phase = "pre_open_auction"
        elif clock <= _CLOSE:
            phase = "regular"
        elif clock <= _AFTER_HOURS_END:
            phase = "after_hours"
        else:
            phase = "closed"

        return {
            "kst_time": kst_iso(moment),
            "trading_day": trading_day,
            "phase": phase,
            "is_live": phase == "regular",
            "trading_day_source": day_source,
            "note": _PHASE_NOTE[phase],
        }


_PHASE_NOTE = {
    "holiday": "한국거래소 휴장일입니다. 표시된 값은 직전 거래일 기준입니다.",
    "weekend": "주말입니다. 표시된 값은 직전 거래일 기준입니다.",
    "before_pre_open": "장 개시 전(08:30 이전)입니다. 값은 직전 거래일 종가 기준입니다.",
    "pre_open_auction": "장전 동시호가 시간(08:30~09:00)입니다.",
    "regular": "정규장(09:00~15:30) 진행 중입니다.",
    "after_hours": "정규장 마감 후(15:30 이후)입니다. 값은 당일 종가 기준입니다.",
    "closed": "장 마감 후입니다. 값은 당일 종가 기준입니다.",
}

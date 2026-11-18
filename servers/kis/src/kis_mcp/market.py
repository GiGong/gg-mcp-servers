"""Korean market clock and session state.

Every tool response carries a KST timestamp and an answer to "is the market
open right now". Holidays are not guessed: they come from the KIS holiday
endpoint (CTCA0903R) and are cached per date.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from .client import KisClient, KisError

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

HOLIDAY_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
HOLIDAY_TR_ID = "CTCA0903R"


@dataclass(frozen=True)
class SessionHours:
    pre_open: dt.time
    open: dt.time
    close: dt.time
    after_hours_end: dt.time


# KRX regular session 09:00-15:30, pre-open auction 08:30-09:00. Prices read on
# the KRX market code (J) keep changing until 20:00 as after-hours trades print
# (verified against the live API on 2026-11-03).
NORMAL_HOURS = SessionHours(
    pre_open=dt.time(8, 30),
    open=dt.time(9, 0),
    close=dt.time(15, 30),
    after_hours_end=dt.time(20, 0),
)

# Days the exchange runs on shifted hours. The KIS holiday endpoint reports only
# open or closed, so these are kept by hand, one entry per announcement.
SPECIAL_HOURS: dict[dt.date, tuple[SessionHours, str]] = {
    # KRX announcement of 2026-11-05: regular session 10:00-16:30, no
    # pre-market, after-hours still ends at 20:00.
    dt.date(2026, 11, 19): (
        SessionHours(
            pre_open=dt.time(9, 30),
            open=dt.time(10, 0),
            close=dt.time(16, 30),
            after_hours_end=dt.time(20, 0),
        ),
        "College entrance exam day: the session starts and ends one hour late.",
    ),
}


def hours_for(day: dt.date) -> tuple[SessionHours, str | None]:
    return SPECIAL_HOURS.get(day, (NORMAL_HOURS, None))


def now_kst() -> dt.datetime:
    return dt.datetime.now(tz=KST)


def kst_iso(moment: dt.datetime | None = None) -> str:
    return (moment or now_kst()).isoformat(timespec="seconds")


class MarketCalendar:
    """Caches holiday lookups per date."""

    def __init__(self, client: KisClient) -> None:
        self._client = client
        self._cache: dict[str, bool] = {}
        self._lock = asyncio.Lock()

    async def is_open_day(self, day: dt.date) -> bool | None:
        """True if the exchange trades that day, False if closed, None if unknown."""
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
                # This is supplementary information. A failure here must not
                # take down the tool that asked for it.
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
            # If the lookup failed, only weekends can be ruled out with
            # confidence; say so rather than implying a holiday check ran.
            trading_day = weekday < 5
            day_source = "weekday-heuristic (KIS holiday lookup unavailable)"
        else:
            trading_day = open_day
            day_source = "KIS chk-holiday (CTCA0903R)"

        hours, special = hours_for(moment.date())
        if not trading_day:
            phase = "holiday" if weekday < 5 else "weekend"
        elif clock < hours.pre_open:
            phase = "before_pre_open"
        elif clock < hours.open:
            phase = "pre_open_auction"
        elif clock <= hours.close:
            phase = "regular"
        elif clock <= hours.after_hours_end:
            phase = "after_hours"
        else:
            phase = "closed"

        note = _phase_note(phase, hours)
        if special and trading_day:
            note = f"{special} {note}"

        return {
            "kst_time": kst_iso(moment),
            "trading_day": trading_day,
            "phase": phase,
            "is_live": phase == "regular",
            "trading_day_source": day_source,
            "note": note,
        }


_NOT_THE_CLOSE = (
    "The current price, change, volume and today's daily close include "
    "after-hours trades, so they are not the regular-session close."
)


def _phase_note(phase: str, hours: SessionHours) -> str:
    def hhmm(value: dt.time) -> str:
        return value.strftime("%H:%M")

    stale = (
        "Prices are from the previous trading day and may include its "
        f"after-hours trades (until {hhmm(hours.after_hours_end)} KST), so they "
        "are not necessarily its regular-session close."
    )
    notes = {
        "holiday": f"Exchange holiday. {stale}",
        "weekend": f"Weekend. {stale}",
        "before_pre_open": (
            f"Before the pre-open auction (earlier than {hhmm(hours.pre_open)} KST). {stale}"
        ),
        "pre_open_auction": f"Pre-open auction ({hhmm(hours.pre_open)}-{hhmm(hours.open)} KST).",
        "regular": f"Regular session in progress ({hhmm(hours.open)}-{hhmm(hours.close)} KST).",
        "after_hours": (
            f"The regular session closed at {hhmm(hours.close)} KST. After-hours "
            f"trading continues until {hhmm(hours.after_hours_end)} KST. {_NOT_THE_CLOSE}"
        ),
        "closed": f"Trading has ended for the day. {_NOT_THE_CLOSE}",
    }
    return notes[phase]

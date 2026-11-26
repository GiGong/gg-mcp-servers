"""Regular-session close and latest trade, kept apart.

Prices read on the KRX market code (J) keep changing after the regular close
until 20:00 as after-hours trades print, and the current day's daily-chart row
follows them until the next trading day appears (verified against the live API
on 2026-11-03 and 2026-11-18). So a close is taken from one of two places:

  - FINAL: the unadjusted KIS daily-chart row for that date, once a later
    trading day exists in the chart. Finished days matched the 15:30 print and
    the next day's base price on every day checked.
  - PROVISIONAL: on the day itself, the closing auction print in the tick list,
    accepted only when it can be told apart from everything around it.

Anything else is PENDING, NO_TRADE, NOT_TRADING_DAY or ERROR rather than a
guess. Independent sources (KRX, optional) are reported separately in `checks`.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .client import KisClient, KisError
from .fields import integer, num, text
from .krx import KrxClient
from .market import KST, MarketCalendar, hours_for, kst_iso

logger = logging.getLogger(__name__)

DOMESTIC = "/uapi/domestic-stock/v1/quotations"
TR_DAILY_CHART = ("FHKST03010100", f"{DOMESTIC}/inquire-daily-itemchartprice")
TR_TICKS = ("FHPST01060000", f"{DOMESTIC}/inquire-time-itemconclusion")
TR_RECENT = ("FHKST01010300", f"{DOMESTIC}/inquire-ccnl")

FEED_CODE = "J"

# No trades print during the ten-minute closing auction, and the after-hours
# closing-price session starts ten minutes after the close.
_AUCTION = dt.timedelta(minutes=10)

# The closing auction ends at a random moment shortly after the close. Asking
# before this would usually find nothing yet.
_PRINT_GRACE = dt.timedelta(minutes=1)

_NEXT_DAY_SETTLES = (
    "Settles from the KIS daily chart once the next trading day appears there."
)


def new_close(trade_date: str | None) -> dict[str, Any]:
    return {
        "trade_date": trade_date,
        "venue": "KRX",
        "session": "REGULAR",
        "price": None,
        "trade_time_kst": None,
        "status": None,
        "source": None,
        "checks": [],
        "independent_verified": False,
    }


def close_error(
    close: dict[str, Any], message: str, *, retryable: bool, kis: KisError | None = None
) -> dict[str, Any]:
    close["status"] = "ERROR"
    close["error"] = {"message": message, "retryable": retryable}
    if kis is not None:
        close["error"].update({"kis_msg_cd": kis.msg_cd, "kis_msg1": kis.msg1, "tr_id": kis.tr_id})
    return close


def _parse_hhmmss(raw: Any) -> dt.time | None:
    value = text(raw)
    if not value or len(value) != 6 or not value.isdigit():
        return None
    try:
        return dt.time(int(value[:2]), int(value[2:4]), int(value[4:]))
    except ValueError:
        return None


class CloseResolver:
    def __init__(self, client: KisClient, calendar: MarketCalendar, krx: KrxClient) -> None:
        self._client = client
        self._calendar = calendar
        self._krx = krx

    @property
    def _env(self) -> str:
        return self._client.settings.kis_env

    async def daily_rows(self, code: str, start: dt.date, end: dt.date) -> dict[str, dict[str, Any]]:
        """Unadjusted daily-chart rows keyed by ISO date."""
        data = await self._client.get(
            TR_DAILY_CHART[1],
            tr_id=TR_DAILY_CHART[0],
            params={
                "FID_COND_MRKT_DIV_CODE": FEED_CODE,
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                # Unadjusted, so a later split or rights issue cannot rewrite a
                # close that has already been scored.
                "FID_ORG_ADJ_PRC": "1",
            },
        )
        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw = text(row.get("stck_bsop_date"))
            if raw and len(raw) == 8 and raw.isdigit():
                out[f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"] = row
        return out

    async def resolve(
        self,
        code: str,
        trade_date: dt.date,
        now: dt.datetime,
        rows: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        close = await self._resolve_price(code, trade_date, now, rows)
        if close["price"] is not None:
            check = await self._krx.check(code, trade_date, close["price"])
            close["checks"].append(check)
        results = [c.get("result") for c in close["checks"]]
        # One match must not hide a conflict from another source.
        close["independent_verified"] = "MATCH" in results and "MISMATCH" not in results
        return close

    async def _resolve_price(
        self,
        code: str,
        trade_date: dt.date,
        now: dt.datetime,
        rows: dict[str, dict[str, Any]] | None,
    ) -> dict[str, Any]:
        today = now.date()
        iso = trade_date.isoformat()
        close = new_close(iso)

        if trade_date > today:
            return close_error(close, "trade_date is in the future.", retryable=False)

        if rows is None:
            try:
                rows = await self.daily_rows(
                    code,
                    trade_date - dt.timedelta(days=10),
                    min(today, trade_date + dt.timedelta(days=20)),
                )
            except KisError as exc:
                return close_error(close, str(exc), retryable=True, kis=exc)

        row = rows.get(iso)
        settled = any(day > iso for day in rows)

        if row is not None and settled:
            volume = integer(row.get("acml_vol"))
            close["price"] = num(row.get("stck_clpr"))
            close["status"] = "NO_TRADE" if volume == 0 else "FINAL"
            close["source"] = {
                "kind": "kis_daily_chart",
                "tr_id": TR_DAILY_CHART[0],
                "feed_code": FEED_CODE,
                "price_basis": "unadjusted",
                "kis_env": self._env,
                "settled_because": "a later trading day exists in the KIS daily chart",
            }
            if close["price"] is None:
                return close_error(close, "The daily-chart row has no close.", retryable=False)
            return close

        if trade_date == today:
            if await self._calendar.is_open_day(today) is False:
                close["status"] = "NOT_TRADING_DAY"
                return close
            if row is None:
                # Without today's row there is no proof the tick list holds
                # today's trades rather than the previous session's.
                close["status"] = "PENDING"
                close["next_check"] = "No trade recorded for this symbol today yet."
                return close
            return await self._intraday(code, today, now, close)

        if row is not None:
            close["status"] = "PENDING"
            close["next_check"] = _NEXT_DAY_SETTLES
            return close

        if await self._calendar.is_open_day(trade_date) is False:
            close["status"] = "NOT_TRADING_DAY"
            return close
        if settled:
            return close_error(
                close,
                "No daily row for this symbol on that date although later days have one "
                "(halted, not yet listed, or the date was not a trading day).",
                retryable=False,
            )
        return close_error(close, "KIS returned no daily rows for that date.", retryable=True)

    async def _intraday(
        self, code: str, day: dt.date, now: dt.datetime, close: dict[str, Any]
    ) -> dict[str, Any]:
        hours, _ = hours_for(day)
        close_at = dt.datetime.combine(day, hours.close, tzinfo=KST)
        if now < close_at + _PRINT_GRACE:
            close["status"] = "PENDING"
            close["next_check"] = (
                f"The regular session closes at {hours.close.strftime('%H:%M')} KST; "
                "ask again after the closing auction has printed."
            )
            return close

        try:
            found, reason = await self.closing_print(code, day)
        except KisError as exc:
            return close_error(close, str(exc), retryable=True, kis=exc)

        if found is None:
            close["status"] = "PENDING"
            close["next_check"] = f"{reason} {_NEXT_DAY_SETTLES}"
            return close

        close["price"] = found["price"]
        close["trade_time_kst"] = kst_iso(found["at"])
        close["status"] = "PROVISIONAL"
        close["source"] = {
            "kind": "kis_closing_auction_print",
            "tr_id": TR_TICKS[0],
            "feed_code": FEED_CODE,
            "kis_env": self._env,
        }
        close["next_check"] = _NEXT_DAY_SETTLES
        return close

    async def closing_print(self, code: str, day: dt.date) -> tuple[dict[str, Any] | None, str]:
        """Find the day's closing auction print, or say why it cannot be trusted.

        The tick query returns at most 30 rows at or before a given time, newest
        first. Asking for the moment just before the after-hours session keeps
        the closing print inside that page.
        """
        hours, _ = hours_for(day)
        close_at = dt.datetime.combine(day, hours.close, tzinfo=KST)
        auction_start = (close_at - _AUCTION).timetz().replace(tzinfo=None)
        window_end = (close_at + _AUCTION).timetz().replace(tzinfo=None)
        close_t = hours.close
        query_at = close_at + _AUCTION - dt.timedelta(seconds=1)

        data = await self._client.get(
            TR_TICKS[1],
            tr_id=TR_TICKS[0],
            params={
                "FID_COND_MRKT_DIV_CODE": FEED_CODE,
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": query_at.strftime("%H%M%S"),
            },
        )
        ticks = []
        for row in data.get("output2") or []:
            at = _parse_hhmmss(row.get("stck_cntg_hour"))
            price = num(row.get("stck_prpr"))
            if at is not None and price is not None:
                ticks.append((at, price))

        after_close = [t for t in ticks if close_t <= t[0] < window_end]
        during_auction = [t for t in ticks if auction_start <= t[0] < close_t]
        before_auction = [t for t in ticks if t[0] < auction_start]

        if not after_close:
            return None, "No closing auction print found yet."
        if during_auction:
            return None, "Trades printed during the closing auction window, so the close is ambiguous."
        if not before_auction:
            return None, "The tick page did not reach back past the closing auction."
        if len({price for _, price in after_close}) != 1:
            return None, "Trades after the close printed at more than one price."

        first_at, price = min(after_close)
        return {"price": price, "at": dt.datetime.combine(day, first_at, tzinfo=KST)}, ""

    async def latest_trade(
        self,
        code: str,
        now: dt.datetime,
        rows: dict[str, dict[str, Any]],
        closing_print_at: str | None = None,
    ) -> dict[str, Any]:
        """The most recent trade on the J feed, with a date only when it is provable.

        The tick time carries no date. It is pinned to today only when today's
        daily row exists with volume, which means at least one trade printed
        today and the newest trade is therefore from today.
        """
        trade: dict[str, Any] = {
            "price": None,
            "trade_time_raw": None,
            "as_of_kst": None,
            "session": "UNKNOWN",
            "venue": "UNKNOWN",
            "feed_code": FEED_CODE,
            "observed_at_kst": kst_iso(now),
        }
        try:
            data = await self._client.get(
                TR_RECENT[1],
                tr_id=TR_RECENT[0],
                params={"FID_COND_MRKT_DIV_CODE": FEED_CODE, "FID_INPUT_ISCD": code},
            )
        except KisError as exc:
            trade["error"] = exc.to_dict()
            return trade

        newest = None
        for row in data.get("output") or []:
            at = _parse_hhmmss(row.get("stck_cntg_hour"))
            price = num(row.get("stck_prpr"))
            if at is not None and price is not None and (newest is None or at > newest[0]):
                newest = (at, price, text(row.get("stck_cntg_hour")))
        if newest is None:
            return trade

        at, price, raw = newest
        trade["price"] = price
        trade["trade_time_raw"] = raw

        today = now.date()
        today_row = rows.get(today.isoformat())
        traded_today = today_row is not None and (integer(today_row.get("acml_vol")) or 0) > 0
        if not traded_today or at > now.timetz().replace(tzinfo=None):
            return trade

        moment = dt.datetime.combine(today, at, tzinfo=KST)
        trade["as_of_kst"] = kst_iso(moment)
        hours, _ = hours_for(today)
        close_at = dt.datetime.combine(today, hours.close, tzinfo=KST)
        if at < hours.open:
            trade["session"] = "PRE_MARKET"
        elif at < hours.close:
            trade["session"], trade["venue"] = "REGULAR", "KRX"
        elif closing_print_at and trade["as_of_kst"] == closing_print_at:
            trade["session"], trade["venue"] = "REGULAR", "KRX"
        elif moment >= close_at + _AUCTION:
            # KRX's official daily volume equals the J feed's final volume
            # including these trades (2026-11-03/17/18), so they print on KRX.
            trade["session"], trade["venue"] = "AFTER_HOURS", "KRX"
        return trade

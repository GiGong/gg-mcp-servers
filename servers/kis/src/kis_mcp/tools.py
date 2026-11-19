"""MCP tool implementations.

Read-only. Every KIS path used here is a /quotations/ inquiry endpoint; order,
amend, cancel and balance endpoints are deliberately absent.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .client import KisClient, KisError
from .fields import integer, num, sign_label, signed, text, yyyymmdd_to_iso
from .krx import KrxClient
from .market import MarketCalendar, hours_for, kst_iso, now_kst
from .regular_close import CloseResolver, close_error, new_close

logger = logging.getLogger(__name__)

# --- KIS API coordinates, taken from the official sample sources ---------------
DOMESTIC = "/uapi/domestic-stock/v1/quotations"
OVERSEAS = "/uapi/overseas-price/v1/quotations"

TR_QUOTE = ("FHKST01010100", f"{DOMESTIC}/inquire-price")
TR_INVESTOR_ESTIMATE = ("HHPTJ04160200", f"{DOMESTIC}/investor-trend-estimate")
TR_INVESTOR_CONFIRMED = ("FHKST01010900", f"{DOMESTIC}/inquire-investor")
TR_DAILY_CHART = ("FHKST03010100", f"{DOMESTIC}/inquire-daily-itemchartprice")
TR_ORDERBOOK = ("FHKST01010200", f"{DOMESTIC}/inquire-asking-price-exp-ccn")
TR_INDEX = ("FHPUP02100000", f"{DOMESTIC}/inquire-index-price")
TR_OVERSEAS_DETAIL = ("HHDFS76200200", f"{OVERSEAS}/price-detail")
TR_OVERSEAS_CHART = ("FHKST03030100", f"{OVERSEAS}/inquire-daily-chartprice")

# Domestic sector index codes.
INDEX_ALIASES = {
    "KOSPI": "0001",
    "코스피": "0001",
    "KOSDAQ": "1001",
    "코스닥": "1001",
    "KOSPI200": "2001",
    "코스피200": "2001",
}

# Overseas index codes (FHKST03030100 with FID_COND_MRKT_DIV_CODE=N).
# Verified against the live API. The leading dot is inconsistent between
# indices, so these must not be guessed: SOX / SPX / COMP take no dot, only
# the Dow uses .DJI.
# Korean aliases are kept so Korean-language queries resolve directly.
OVERSEAS_INDEX_ALIASES = {
    "DJI": ".DJI",
    ".DJI": ".DJI",
    "DOW": ".DJI",
    "다우": ".DJI",
    "SPX": "SPX",
    "S&P500": "SPX",
    "SP500": "SPX",
    "COMP": "COMP",
    "IXIC": "COMP",
    "NASDAQ": "COMP",
    "나스닥": "COMP",
    "SOX": "SOX",
    "SOXX": "SOX",
    "필라델피아반도체": "SOX",
    "필반": "SOX",
}

# Tried in order when the exchange for a US ticker is unknown. KIS answers a
# wrong exchange with rt_cd=0 and an empty body, so the payload must be
# inspected rather than the status code.
US_EXCHANGE_FALLBACKS = ("NAS", "NYS", "AMS")

# Overseas exchange codes.
OVERSEAS_EXCHANGES = {
    "NAS": "NASDAQ",
    "NYS": "NYSE",
    "AMS": "NYSE American",
    "TSE": "Tokyo",
    "HKS": "Hong Kong",
    "SHS": "Shanghai",
    "SZS": "Shenzhen",
    "HSX": "Ho Chi Minh",
    "HNX": "Hanoi",
}

# Publication times for the provisional estimate, per KIS documentation.
# These correspond to the bsop_hour_gb values.
ESTIMATE_SNAPSHOT_TIMES = {
    "1": "09:30",
    "2": "10:00",
    "3": "11:20",
    "4": "13:20",
    "5": "14:30",
}

_PROVISIONAL_NOTE = (
    "Provisional estimate compiled and entered by brokerage staff during the "
    "session. Foreign figures update at 09:30/11:20/13:20/14:30 KST and "
    "institutional figures at 10:00/11:20/13:20/14:30 KST. Slot 1 carries no "
    "institutional figure yet, so it is null there. These may differ from the "
    "confirmed values."
)
_SHIFTED_SLOTS_NOTE = (
    "Today runs on shifted hours, so the usual publication times do not apply "
    "and approx_kst_time is null. The slot order still holds."
)
_CONFIRMED_NOTE = "Exchange-confirmed daily net buying."
_CLOSE_READING_GUIDE = (
    "status says how the price was obtained. FINAL: the unadjusted KIS daily "
    "chart, settled because a later trading day exists; use it for final "
    "scoring. PROVISIONAL: today's closing auction print; fine for a provisional "
    "score, final scoring waits. PENDING: not available yet, see next_check. "
    "ERROR: see error.retryable. NO_TRADE and NOT_TRADING_DAY: nothing to score, "
    "stop polling. checks lists independent sources such as the optional KRX "
    "Open API; a MISMATCH there means hold off on final scoring even when status "
    "is FINAL. Never use get_quote's price.current as a close."
)
_SAME_DAY_CONFIRMED_NOTE = (
    "Today's figures appear shortly after the regular close, before after-hours "
    "trading ends at 20:00 KST, and have been seen to change by the next day. "
    "Treat them as preliminary until then."
)


def _normalise_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise ValueError("symbol must not be empty")
    return cleaned


def _source(tr: tuple[str, str], env: str) -> dict[str, str]:
    return {"tr_id": tr[0], "api_path": tr[1], "kis_env": env}


class KisTools:
    def __init__(
        self, client: KisClient, calendar: MarketCalendar, krx: KrxClient | None = None
    ) -> None:
        self._client = client
        self._calendar = calendar
        self._closes = CloseResolver(client, calendar, krx or KrxClient(""))
        # inquire-price carries no security name, only a sector name. Fill it
        # once from the daily-chart endpoint and cache for the process lifetime.
        self._name_cache: dict[str, str | None] = {}

    @property
    def _env(self) -> str:
        return self._client.settings.kis_env

    @property
    def _env_dv(self) -> str:
        return self._client.settings.kis_env

    async def _resolve_name(self, code: str) -> str | None:
        """Read the security name from the daily-chart header once, then cache it."""
        if code in self._name_cache:
            return self._name_cache[code]

        end = now_kst().date()
        start = end - dt.timedelta(days=10)
        try:
            data = await self._client.get(
                TR_DAILY_CHART[1],
                tr_id=TR_DAILY_CHART[0],
                params={
                    "FID_COND_MRKT_DIV_CODE": "J",
                    "FID_INPUT_ISCD": code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                    "FID_ORG_ADJ_PRC": "0",
                },
            )
        except KisError as exc:
            logger.warning("name lookup failed for %s: %s", code, exc)
            return None

        name = text((data.get("output1") or {}).get("hts_kor_isnm"))
        self._name_cache[code] = name
        return name

    # ------------------------------------------------------------------
    # Primary tools
    # ------------------------------------------------------------------

    async def get_quote(self, symbol: str, market: str = "J") -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        now = now_kst()
        data = await self._client.get(
            TR_QUOTE[1],
            tr_id=TR_QUOTE[0],
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code},
        )
        out = data.get("output") or {}
        session = await self._calendar.session(now)

        price = num(out.get("stck_prpr"))
        base_price = num(out.get("stck_sdpr"))
        market_cap_eok = num(out.get("hts_avls"))  # unit: 100 million KRW

        close, latest_trade = await self._close_and_latest(code, now)

        return {
            "symbol": code,
            "name": await self._resolve_name(code),
            "market": text(out.get("rprs_mrkt_kor_name")),
            "sector": text(out.get("bstp_kor_isnm")),
            "as_of_kst": kst_iso(now),
            "market_status": session,
            "close": close,
            "latest_trade": latest_trade,
            "price": {
                "current": price,
                "open": num(out.get("stck_oprc")),
                "high": num(out.get("stck_hgpr")),
                "low": num(out.get("stck_lwpr")),
                "base_price": base_price,
                # Deprecated alias of base_price, kept for existing readers. It
                # is the exchange's base price, which differs from the previous
                # close on ex-rights, ex-dividend and listing days.
                "prev_close": base_price,
                "upper_limit": num(out.get("stck_mxpr")),
                "lower_limit": num(out.get("stck_llam")),
                "week52_high": num(out.get("w52_hgpr")),
                "week52_low": num(out.get("w52_lwpr")),
                "currency": "KRW",
            },
            "change": {
                "value": signed(out.get("prdy_vrss"), out.get("prdy_vrss_sign")),
                "percent": num(out.get("prdy_ctrt")),
                "direction": sign_label(out.get("prdy_vrss_sign")),
            },
            "volume": {
                "shares": integer(out.get("acml_vol")),
                "value_krw": integer(out.get("acml_tr_pbmn")),
                "turnover_ratio_percent": num(out.get("vol_tnrt")),
            },
            "valuation": {
                "market_cap_krw_eok": market_cap_eok,
                "market_cap_krw": None if market_cap_eok is None else int(market_cap_eok * 100_000_000),
                "per": num(out.get("per")),
                "pbr": num(out.get("pbr")),
                "eps": num(out.get("eps")),
                "bps": num(out.get("bps")),
                "listed_shares": integer(out.get("lstn_stcn")),
            },
            "status_code": text(out.get("iscd_stat_cls_code")),
            "source": _source(TR_QUOTE, self._env),
        }

    async def _close_and_latest(
        self, code: str, now: dt.datetime
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """The latest session's regular close and the newest trade, from one chart call.

        The close shown is today's once today's session has closed, otherwise
        the previous trading day's.
        """
        today = now.date()
        try:
            rows = await self._closes.daily_rows(code, today - dt.timedelta(days=15), today)
        except KisError as exc:
            failed = close_error(new_close(None), str(exc), retryable=True, kis=exc)
            return failed, await self._closes.latest_trade(code, now, {})

        hours, _ = hours_for(today)
        closed_today = today.isoformat() in rows and now.timetz().replace(tzinfo=None) > hours.close
        earlier = [day for day in rows if day < today.isoformat()]
        if closed_today or not earlier:
            target = today
        else:
            target = dt.date.fromisoformat(max(earlier))

        close = await self._closes.resolve(code, target, now, rows)
        print_at = close["trade_time_kst"] if close["status"] == "PROVISIONAL" else None
        latest = await self._closes.latest_trade(code, now, rows, print_at)
        return close, latest

    async def get_regular_close(self, symbol: str, trade_date: str) -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        raw = trade_date.strip().replace("-", "")
        try:
            day = dt.datetime.strptime(raw, "%Y%m%d").date()
        except ValueError as exc:
            raise ValueError(f"trade_date must be YYYY-MM-DD, got {trade_date!r}") from exc

        now = now_kst()
        return {
            "symbol": code,
            "name": await self._resolve_name(code),
            "as_of_kst": kst_iso(now),
            "close": await self._closes.resolve(code, day, now),
            "reading_guide": _CLOSE_READING_GUIDE,
        }

    async def get_investor_flow(self, symbol: str, confirmed_days: int = 5) -> dict[str, Any]:
        """Return the intraday provisional estimate and the confirmed daily flow.

        The two endpoints are independent, so a failure on one side still
        returns the other.
        """
        code = _normalise_symbol(symbol)
        session = await self._calendar.session()

        provisional = await self._provisional_flow(code)
        confirmed = await self._confirmed_flow(code, confirmed_days)

        return {
            "symbol": code,
            "as_of_kst": kst_iso(),
            "market_status": session,
            "provisional": provisional,
            "confirmed": confirmed,
            "reading_guide": (
                "provisional is an intraday broker-compiled estimate; confirmed is "
                "the exchange-published daily figure. The two can differ for the same "
                "date. Today's confirmed figures appear after the close but may still "
                "change by the next trading day; earlier days are settled."
            ),
        }

    async def _provisional_flow(self, code: str) -> dict[str, Any]:
        try:
            data = await self._client.get(
                TR_INVESTOR_ESTIMATE[1],
                tr_id=TR_INVESTOR_ESTIMATE[0],
                params={"MKSC_SHRN_ISCD": code},
            )
        except KisError as exc:
            payload = exc.to_dict()
            payload["available"] = False
            payload["hint"] = (
                "The provisional investor-flow endpoint (HHPTJ04160200) may only be "
                "available on live accounts. Check that KIS_ENV=real."
                if self._client.settings.is_demo
                else "The KIS error code is passed through unchanged."
            )
            payload["data_type"] = "provisional_estimate"
            return payload

        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]

        # The publication times are for normal hours. On a day with shifted
        # hours they do not hold (2026-11-19: only slot 1 at 10:39, slot 2 by
        # 10:52), so leave them out rather than label a slot wrongly.
        shifted = hours_for(now_kst().date())[1] is not None

        snapshots = []
        for row in rows:
            slot = text(row.get("bsop_hour_gb"))
            institution = integer(row.get("orgn_fake_ntby_qty"))
            combined = integer(row.get("sum_fake_ntby_qty"))
            if slot == "1" and institution == 0:
                # Institutions are first compiled in slot 2. A zero here means
                # not compiled yet, not zero net buying.
                institution = combined = None
            snapshots.append(
                {
                    "slot": slot,
                    "approx_kst_time": None if shifted else ESTIMATE_SNAPSHOT_TIMES.get(slot or ""),
                    "foreign_net_qty": integer(row.get("frgn_fake_ntby_qty")),
                    "institution_net_qty": institution,
                    "combined_net_qty": combined,
                }
            )

        # KIS returns the newest slot first. Order by slot number rather than
        # position so `latest` is the last snapshot of the day.
        snapshots.sort(key=lambda s: integer(s["slot"]) or 0)
        latest = snapshots[-1] if snapshots else None
        note = _PROVISIONAL_NOTE
        if shifted:
            note = f"{note} {_SHIFTED_SLOTS_NOTE}"
        return {
            "available": True,
            "data_type": "provisional_estimate",
            "is_confirmed": False,
            "note": note,
            "latest": latest,
            "snapshots": snapshots,
            "source": _source(TR_INVESTOR_ESTIMATE, self._env),
        }

    async def _confirmed_flow(self, code: str, days: int) -> dict[str, Any]:
        try:
            data = await self._client.get(
                TR_INVESTOR_CONFIRMED[1],
                tr_id=TR_INVESTOR_CONFIRMED[0],
                params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
        except KisError as exc:
            payload = exc.to_dict()
            payload["available"] = False
            payload["data_type"] = "confirmed"
            return payload

        rows = data.get("output") or []
        if isinstance(rows, dict):
            rows = [rows]

        today = now_kst().date().isoformat()
        series = []
        for row in rows[: max(1, days)]:
            foreign = integer(row.get("frgn_ntby_qty"))
            institution = integer(row.get("orgn_ntby_qty"))
            # The current day's row carries empty strings for every flow field
            # until after the close.
            published = foreign is not None or institution is not None
            date = yyyymmdd_to_iso(row.get("stck_bsop_date"))
            series.append(
                {
                    "date": date,
                    "close": num(row.get("stck_clpr")),
                    "change": signed(row.get("prdy_vrss"), row.get("prdy_vrss_sign")),
                    "is_published": published,
                    # Today's flow can change by the next trading day, and its
                    # close follows after-hours trades until then.
                    "preliminary": date == today,
                    "foreign_net_qty": foreign,
                    "institution_net_qty": institution,
                    "individual_net_qty": integer(row.get("prsn_ntby_qty")),
                    # KIS reports *_tr_pbmn in millions of KRW.
                    "foreign_net_value_krw_mn": integer(row.get("frgn_ntby_tr_pbmn")),
                    "institution_net_value_krw_mn": integer(row.get("orgn_ntby_tr_pbmn")),
                    "individual_net_value_krw_mn": integer(row.get("prsn_ntby_tr_pbmn")),
                }
            )

        published = [row for row in series if row["is_published"]]
        pending = [row for row in series if not row["is_published"]]

        note = _CONFIRMED_NOTE
        if published and published[0]["date"] == today:
            note = f"{note} {_SAME_DAY_CONFIRMED_NOTE}"

        result: dict[str, Any] = {
            "available": True,
            "data_type": "confirmed",
            "is_confirmed": True,
            "note": note,
            "value_unit": "Net quantities are in shares; *_value_krw_mn is in millions of KRW.",
            "latest": published[0] if published else None,
            "series": series,
            "source": _source(TR_INVESTOR_CONFIRMED, self._env),
        }
        if pending:
            result["pending_dates"] = [row["date"] for row in pending]
            result["pending_note"] = (
                f"Confirmed flow for {', '.join(row['date'] for row in pending)} "
                "has not been published yet (empty during the session). Use the "
                "provisional estimate for those dates. `latest` is the most recent "
                "trading day that does have confirmed data."
            )
        return result

    async def get_daily_ohlcv(
        self, symbol: str, days: int = 30, adjusted: bool = True
    ) -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        days = max(1, min(days, 100))  # KIS returns at most 100 rows per call

        end = now_kst().date()
        # The count is in trading days, so widen the calendar range to absorb
        # weekends and holidays.
        start = end - dt.timedelta(days=int(days * 1.9) + 15)

        data = await self._client.get(
            TR_DAILY_CHART[1],
            tr_id=TR_DAILY_CHART[0],
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",
            },
        )

        head = data.get("output1") or {}
        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]

        candles = []
        for row in rows:
            date = yyyymmdd_to_iso(row.get("stck_bsop_date"))
            if not date:
                continue
            candles.append(
                {
                    "date": date,
                    "open": num(row.get("stck_oprc")),
                    "high": num(row.get("stck_hgpr")),
                    "low": num(row.get("stck_lwpr")),
                    "close": num(row.get("stck_clpr")),
                    "volume": integer(row.get("acml_vol")),
                    "value_krw": integer(row.get("acml_tr_pbmn")),
                    "change": signed(row.get("prdy_vrss"), row.get("prdy_vrss_sign")),
                }
            )

        # KIS returns newest first. Flip to oldest-first and trim to the request.
        candles.sort(key=lambda c: c["date"])
        candles = candles[-days:]

        return {
            "symbol": code,
            "name": text(head.get("hts_kor_isnm")),
            "as_of_kst": kst_iso(),
            "market_status": await self._calendar.session(),
            "requested_days": days,
            "returned_days": len(candles),
            "price_basis": "adjusted" if adjusted else "unadjusted",
            "note": (
                "The newest candle can still include after-hours trades until the next "
                "trading day appears, so its close may not be the regular-session close. "
                "Use get_regular_close for a close to score against."
            ),
            "range": {
                "from": candles[0]["date"] if candles else None,
                "to": candles[-1]["date"] if candles else None,
            },
            "candles": candles,
            "source": _source(TR_DAILY_CHART, self._env),
        }

    # ------------------------------------------------------------------
    # Secondary tools
    # ------------------------------------------------------------------

    async def get_orderbook(self, symbol: str, depth: int = 10) -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        depth = max(1, min(depth, 10))
        data = await self._client.get(
            TR_ORDERBOOK[1],
            tr_id=TR_ORDERBOOK[0],
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        book = data.get("output1") or {}
        extra = data.get("output2") or {}

        asks = [
            {
                "level": i,
                "price": num(book.get(f"askp{i}")),
                "quantity": integer(book.get(f"askp_rsqn{i}")),
                "quantity_change": integer(book.get(f"askp_rsqn_icdc{i}")),
            }
            for i in range(1, depth + 1)
        ]
        bids = [
            {
                "level": i,
                "price": num(book.get(f"bidp{i}")),
                "quantity": integer(book.get(f"bidp_rsqn{i}")),
                "quantity_change": integer(book.get(f"bidp_rsqn_icdc{i}")),
            }
            for i in range(1, depth + 1)
        ]

        total_ask = integer(book.get("total_askp_rsqn"))
        total_bid = integer(book.get("total_bidp_rsqn"))
        ratio = None
        if total_ask and total_bid and total_ask > 0:
            ratio = round(total_bid / total_ask, 4)

        return {
            "symbol": code,
            "as_of_kst": kst_iso(),
            "quote_time_kst": text(book.get("aspr_acpt_hour")),
            "market_status": await self._calendar.session(),
            "asks": asks,
            "bids": bids,
            "totals": {
                "ask_quantity": total_ask,
                "bid_quantity": total_bid,
                "ask_quantity_change": integer(book.get("total_askp_rsqn_icdc")),
                "bid_quantity_change": integer(book.get("total_bidp_rsqn_icdc")),
                "bid_to_ask_ratio": ratio,
                "net_quantity": integer(book.get("ntby_aspr_rsqn")),
            },
            "after_hours_totals": {
                "ask_quantity": integer(book.get("ovtm_total_askp_rsqn")),
                "bid_quantity": integer(book.get("ovtm_total_bidp_rsqn")),
            },
            "expected_conclusion": {
                "price": num(extra.get("antc_cnpr")),
                "volume": integer(extra.get("antc_vol")),
                "change": signed(extra.get("antc_cntg_vrss"), extra.get("antc_cntg_vrss_sign")),
                "change_percent": num(extra.get("antc_cntg_prdy_ctrt")),
                "note": "Only meaningful during the pre-open and closing auctions.",
            },
            "current_price": num(extra.get("stck_prpr")),
            "source": _source(TR_ORDERBOOK, self._env),
        }

    async def get_index(self, code: str) -> dict[str, Any]:
        raw = code.strip().upper()

        if raw in OVERSEAS_INDEX_ALIASES or raw.startswith("."):
            return await self._overseas_index(OVERSEAS_INDEX_ALIASES.get(raw, raw), raw)

        index_code = INDEX_ALIASES.get(raw, raw)
        data = await self._client.get(
            TR_INDEX[1],
            tr_id=TR_INDEX[0],
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": index_code},
        )
        out = data.get("output") or {}
        if isinstance(out, list):
            out = out[0] if out else {}

        return {
            "index_code": index_code,
            "requested": raw,
            "scope": "domestic",
            "as_of_kst": kst_iso(),
            "market_status": await self._calendar.session(),
            "price": {
                "current": num(out.get("bstp_nmix_prpr")),
                "open": num(out.get("bstp_nmix_oprc")),
                "high": num(out.get("bstp_nmix_hgpr")),
                "low": num(out.get("bstp_nmix_lwpr")),
            },
            "change": {
                "value": signed(out.get("bstp_nmix_prdy_vrss"), out.get("prdy_vrss_sign")),
                "percent": num(out.get("bstp_nmix_prdy_ctrt")),
                "direction": sign_label(out.get("prdy_vrss_sign")),
            },
            "volume": {
                "shares": integer(out.get("acml_vol")),
                "value_krw": integer(out.get("acml_tr_pbmn")),
            },
            "breadth": {
                "advancing": integer(out.get("ascn_issu_cnt")),
                "declining": integer(out.get("down_issu_cnt")),
                "unchanged": integer(out.get("stnr_issu_cnt")),
                "limit_up": integer(out.get("uplm_issu_cnt")),
                "limit_down": integer(out.get("lslm_issu_cnt")),
            },
            "source": _source(TR_INDEX, self._env),
        }

    async def _overseas_index(self, index_code: str, requested: str) -> dict[str, Any]:
        end = now_kst().date()
        start = end - dt.timedelta(days=14)
        data = await self._client.get(
            TR_OVERSEAS_CHART[1],
            tr_id=TR_OVERSEAS_CHART[0],
            params={
                "FID_COND_MRKT_DIV_CODE": "N",
                "FID_INPUT_ISCD": index_code,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": "D",
            },
        )
        head = data.get("output1") or {}
        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        rows = [r for r in rows if text(r.get("stck_bsop_date"))]
        rows.sort(key=lambda r: str(r.get("stck_bsop_date")))
        latest = rows[-1] if rows else {}

        current = num(head.get("ovrs_nmix_prpr")) or num(latest.get("ovrs_nmix_prpr"))
        if not current:
            # For an unknown code KIS returns rt_cd=0 with 0.00 and an empty list.
            return {
                "error": True,
                "message": (
                    f"No data returned for index code {index_code!r}. "
                    f"Known codes: {', '.join(sorted(set(OVERSEAS_INDEX_ALIASES.values())))}"
                ),
                "index_code": index_code,
                "requested": requested,
                "as_of_kst": kst_iso(),
                "source": _source(TR_OVERSEAS_CHART, self._env),
            }

        return {
            "index_code": index_code,
            "requested": requested,
            "scope": "overseas",
            "name": text(head.get("hts_kor_isnm")),
            "as_of_kst": kst_iso(),
            "latest_session_date": yyyymmdd_to_iso(latest.get("stck_bsop_date")),
            "price": {
                "current": num(head.get("ovrs_nmix_prpr")) or num(latest.get("ovrs_nmix_prpr")),
                "open": num(latest.get("ovrs_nmix_oprc")),
                "high": num(latest.get("ovrs_nmix_hgpr")),
                "low": num(latest.get("ovrs_nmix_lwpr")),
                "prev_close": num(head.get("ovrs_nmix_prdy_clpr")),
            },
            "change": {
                "value": signed(head.get("ovrs_nmix_prdy_vrss"), head.get("prdy_vrss_sign")),
                "percent": num(head.get("prdy_ctrt")),
                "direction": sign_label(head.get("prdy_vrss_sign")),
            },
            "recent_closes": [
                {
                    "date": yyyymmdd_to_iso(r.get("stck_bsop_date")),
                    "close": num(r.get("ovrs_nmix_prpr")),
                }
                for r in rows[-10:]
            ],
            "delayed": True,
            "delay_note": (
                "Overseas indices come from the daily-chart endpoint, not a live "
                "tick. Read them as the previous regular session's close."
            ),
            "source": _source(TR_OVERSEAS_CHART, self._env),
        }

    async def get_overseas_quote(self, ticker: str, exchange: str = "NAS") -> dict[str, Any]:
        symbol = ticker.strip().upper()
        excd = exchange.strip().upper()
        if not symbol:
            raise ValueError("ticker must not be empty")

        # KIS answers a wrong exchange with rt_cd=0 and an empty output, so an
        # empty payload triggers a retry against the other US exchanges
        # (TSM, for instance, is NYSE rather than NASDAQ).
        candidates = [excd] + [e for e in US_EXCHANGE_FALLBACKS if e != excd]
        tried: list[str] = []
        out: dict[str, Any] = {}
        resolved = excd

        for candidate in candidates:
            data = await self._client.get(
                TR_OVERSEAS_DETAIL[1],
                tr_id=TR_OVERSEAS_DETAIL[0],
                params={"AUTH": "", "EXCD": candidate, "SYMB": symbol},
            )
            tried.append(candidate)
            candidate_out = data.get("output") or {}
            if num(candidate_out.get("last")) is not None:
                out = candidate_out
                resolved = candidate
                break
            # Do not fall back when a non-US exchange was requested explicitly.
            if excd not in US_EXCHANGE_FALLBACKS:
                break

        if not out:
            return {
                "error": True,
                "message": (
                    f"No quote found for {symbol}. Exchanges tried: {', '.join(tried)}. "
                    "KIS returns an empty response rather than an error when the "
                    "exchange code does not match. Check the ticker and exchange."
                ),
                "ticker": symbol,
                "requested_exchange": excd,
                "exchanges_tried": tried,
                "as_of_kst": kst_iso(),
                "source": _source(TR_OVERSEAS_DETAIL, self._env),
            }

        excd = resolved
        last = num(out.get("last"))
        base = num(out.get("base"))
        change = None if (last is None or base is None) else round(last - base, 6)
        change_pct = (
            None if (change is None or not base) else round(change / base * 100, 4)
        )
        realtime = self._client.settings.overseas_realtime

        return {
            "ticker": symbol,
            "exchange": excd,
            "exchange_name": OVERSEAS_EXCHANGES.get(excd, excd),
            "exchange_resolved_by_fallback": excd != tried[0],
            "as_of_kst": kst_iso(),
            "price": {
                "current": last,
                "open": num(out.get("open")),
                "high": num(out.get("high")),
                "low": num(out.get("low")),
                "prev_close": base,
                "week52_high": num(out.get("h52p")),
                "week52_low": num(out.get("l52p")),
                "currency": text(out.get("curr")),
            },
            "change": {"value": change, "percent": change_pct},
            "volume": {
                "shares": integer(out.get("tvol")),
                "value": integer(out.get("tamt")),
                "prev_shares": integer(out.get("pvol")),
            },
            "valuation": {
                "market_cap": num(out.get("tomv")),
                "per": num(out.get("perx")),
                "pbr": num(out.get("pbrx")),
                "eps": num(out.get("epsx")),
                "bps": num(out.get("bpsx")),
                "listed_shares": integer(out.get("shar")),
            },
            "krw_converted": {
                "price_krw": num(out.get("t_xprc")),
                "prev_price_krw": num(out.get("p_xprc")),
                "fx_rate_today": num(out.get("t_rate")),
                "fx_rate_prev": num(out.get("p_rate")),
                "note": "The KRW-converted price and FX rate as applied by KIS. "
                        "Usable directly for ADR-versus-home-listing comparisons.",
            },
            "tradable": text(out.get("e_ordyn")),
            "delayed": not realtime,
            "delay_note": (
                "Configured as an account subscribed to KIS real-time overseas "
                "data (KIS_OVERSEAS_REALTIME=true)."
                if realtime
                else "KIS overseas quotes are delayed by default (typically 15 "
                "minutes for US markets). If your account subscribes to real-time "
                "data, set KIS_OVERSEAS_REALTIME=true in .env."
            ),
            "source": _source(TR_OVERSEAS_DETAIL, self._env),
        }

    # ------------------------------------------------------------------
    # Optional tools
    # ------------------------------------------------------------------

    async def get_fx(self, pair: str = "USDKRW") -> dict[str, Any]:
        """USD/KRW exchange rate.

        Primary source: the FX mode of the overseas period-price endpoint
        (FID_COND_MRKT_DIV_CODE=X). If that fails, fall back to the applied
        conversion rate (t_rate) from the overseas quote detail endpoint.
        Either way the response records which path was used.
        """
        normalised = pair.strip().upper().replace("/", "").replace("-", "")
        fx_code = {"USDKRW": "FX@KRW", "KRWUSD": "FX@KRW"}.get(normalised, normalised)

        end = now_kst().date()
        start = end - dt.timedelta(days=14)
        try:
            data = await self._client.get(
                TR_OVERSEAS_CHART[1],
                tr_id=TR_OVERSEAS_CHART[0],
                params={
                    "FID_COND_MRKT_DIV_CODE": "X",
                    "FID_INPUT_ISCD": fx_code,
                    "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                    "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                    "FID_PERIOD_DIV_CODE": "D",
                },
            )
        except KisError as exc:
            logger.warning("FX chart lookup failed (%s); falling back to t_rate", exc)
            return await self._fx_from_applied_rate(pair, exc)

        head = data.get("output1") or {}
        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]
        rows = [r for r in rows if text(r.get("stck_bsop_date"))]
        rows.sort(key=lambda r: str(r.get("stck_bsop_date")))

        if not rows and not num(head.get("ovrs_nmix_prpr")):
            return await self._fx_from_applied_rate(pair, None)

        latest = rows[-1] if rows else {}
        return {
            "pair": "USD/KRW" if fx_code == "FX@KRW" else pair.upper(),
            "kis_code": fx_code,
            "as_of_kst": kst_iso(),
            "latest_session_date": yyyymmdd_to_iso(latest.get("stck_bsop_date")),
            "rate": num(head.get("ovrs_nmix_prpr")) or num(latest.get("ovrs_nmix_prpr")),
            "prev_rate": num(head.get("ovrs_nmix_prdy_clpr")),
            "change": {
                "value": signed(head.get("ovrs_nmix_prdy_vrss"), head.get("prdy_vrss_sign")),
                "percent": num(head.get("prdy_ctrt")),
            },
            "recent": [
                {
                    "date": yyyymmdd_to_iso(r.get("stck_bsop_date")),
                    "rate": num(r.get("ovrs_nmix_prpr")),
                }
                for r in rows[-10:]
            ],
            "method": "kis_fx_chart",
            "delayed": True,
            "delay_note": "Daily FX quotes, not an intraday live rate.",
            "source": _source(TR_OVERSEAS_CHART, self._env),
        }

    async def _fx_from_applied_rate(
        self, pair: str, primary_error: KisError | None
    ) -> dict[str, Any]:
        """Fallback: use the applied conversion rate from the quote detail endpoint."""
        fallback: dict[str, Any] = {
            "pair": "USD/KRW",
            "as_of_kst": kst_iso(),
            "method": "kis_applied_rate_from_price_detail",
            "method_note": (
                "The dedicated FX lookup failed, so this is the applied conversion "
                "rate (t_rate) from the overseas quote detail endpoint "
                "(HHDFS76200200). Since it is the rate KIS actually uses for KRW "
                "conversion, it is arguably the more consistent choice for ADR "
                "comparisons."
            ),
            "delayed": True,
        }
        if primary_error is not None:
            fallback["primary_error"] = primary_error.to_dict()

        try:
            data = await self._client.get(
                TR_OVERSEAS_DETAIL[1],
                tr_id=TR_OVERSEAS_DETAIL[0],
                params={"AUTH": "", "EXCD": "NAS", "SYMB": "AAPL"},
            )
        except KisError as exc:
            fallback["error"] = True
            fallback["fallback_error"] = exc.to_dict()
            fallback["rate"] = None
            return fallback

        out = data.get("output") or {}
        fallback["rate"] = num(out.get("t_rate"))
        fallback["prev_rate"] = num(out.get("p_rate"))
        fallback["probe_symbol"] = "AAPL@NAS"
        fallback["source"] = _source(TR_OVERSEAS_DETAIL, self._env)
        return fallback

    # ------------------------------------------------------------------
    # Auxiliary
    # ------------------------------------------------------------------

    async def get_market_status(self) -> dict[str, Any]:
        session = await self._calendar.session()
        return {
            "as_of_kst": kst_iso(),
            "krx": session,
            "kis_env": self._env,
            "throttle_per_second": self._client.settings.kis_rate_limit_per_sec,
        }

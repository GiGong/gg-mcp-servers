"""MCP 도구 구현.

읽기 전용. 여기서 다루는 KIS 경로는 전부 /quotations/ 조회 계열이며
주문·정정·취소·잔고 계열 경로는 의도적으로 포함하지 않는다.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from .client import KisClient, KisError
from .fields import integer, num, sign_label, signed, text, yyyymmdd_to_iso
from .market import MarketCalendar, kst_iso, now_kst

logger = logging.getLogger(__name__)

# --- KIS API 좌표 (공식 예제 소스에서 확인한 값) --------------------------------
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

# 국내 업종 지수 코드
INDEX_ALIASES = {
    "KOSPI": "0001",
    "코스피": "0001",
    "KOSDAQ": "1001",
    "코스닥": "1001",
    "KOSPI200": "2001",
    "코스피200": "2001",
}

# 해외 지수 코드 (FHKST03030100, FID_COND_MRKT_DIV_CODE=N)
# 실호출로 확인한 값이다. 접두 '.' 유무가 지수마다 다르므로 추측하면 안 된다.
#   SOX / SPX / COMP 는 접두사 없음, 다우만 .DJI
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

# 미국 종목의 거래소를 모를 때 순서대로 시도한다.
# KIS 는 거래소가 틀려도 rt_cd=0 에 빈 output 을 주므로 값으로 판별해야 한다.
US_EXCHANGE_FALLBACKS = ("NAS", "NYS", "AMS")

# 해외 거래소 코드
OVERSEAS_EXCHANGES = {
    "NAS": "나스닥",
    "NYS": "뉴욕",
    "AMS": "아멕스",
    "TSE": "도쿄",
    "HKS": "홍콩",
    "SHS": "상해",
    "SZS": "심천",
    "HSX": "호치민",
    "HNX": "하노이",
}

# 추정 가집계의 입력 시각 (KIS 공식 안내). bsop_hour_gb 순서와 대응한다.
ESTIMATE_SNAPSHOT_TIMES = {
    "1": "09:30",
    "2": "10:00",
    "3": "11:20",
    "4": "13:20",
    "5": "14:30",
}

_PROVISIONAL_NOTE = (
    "증권사 직원이 장중에 집계·입력한 추정 가집계(잠정)입니다. "
    "외국인은 09:30/11:20/13:20/14:30, 기관종합은 10:00/11:20/13:20/14:30 에 "
    "갱신되며 확정치와 다를 수 있습니다."
)
_CONFIRMED_NOTE = "거래소 확정 집계(일별 순매수)입니다."


def _normalise_symbol(symbol: str) -> str:
    cleaned = symbol.strip().upper()
    if not cleaned:
        raise ValueError("종목코드가 비어 있습니다.")
    return cleaned


def _source(tr: tuple[str, str], env: str) -> dict[str, str]:
    return {"tr_id": tr[0], "api_path": tr[1], "kis_env": env}


class KisTools:
    def __init__(self, client: KisClient, calendar: MarketCalendar) -> None:
        self._client = client
        self._calendar = calendar
        # inquire-price 응답에는 종목명 필드가 없다(업종명만 있음).
        # 일별 차트 API 의 hts_kor_isnm 으로 한 번만 채우고 프로세스 수명 동안 캐시한다.
        self._name_cache: dict[str, str | None] = {}

    @property
    def _env(self) -> str:
        return self._client.settings.kis_env

    @property
    def _env_dv(self) -> str:
        return self._client.settings.kis_env

    async def _resolve_name(self, code: str) -> str | None:
        """종목 한글명을 일별 차트 API 헤더에서 한 번만 읽어와 캐시한다."""
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
    # 1순위
    # ------------------------------------------------------------------

    async def get_quote(self, symbol: str, market: str = "J") -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        data = await self._client.get(
            TR_QUOTE[1],
            tr_id=TR_QUOTE[0],
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": code},
        )
        out = data.get("output") or {}
        session = await self._calendar.session()

        price = num(out.get("stck_prpr"))
        prev_close = num(out.get("stck_sdpr"))
        market_cap_eok = num(out.get("hts_avls"))  # 억원 단위

        return {
            "symbol": code,
            "name": await self._resolve_name(code),
            "market": text(out.get("rprs_mrkt_kor_name")),
            "sector": text(out.get("bstp_kor_isnm")),
            "as_of_kst": kst_iso(),
            "market_status": session,
            "price": {
                "current": price,
                "open": num(out.get("stck_oprc")),
                "high": num(out.get("stck_hgpr")),
                "low": num(out.get("stck_lwpr")),
                "prev_close": prev_close,
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

    async def get_investor_flow(self, symbol: str, confirmed_days: int = 5) -> dict[str, Any]:
        """장중 잠정(추정 가집계)과 일별 확정치를 함께 반환한다.

        두 API 는 서로 독립이므로 한쪽이 실패해도 다른 쪽은 그대로 돌려준다.
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
                "provisional 은 장중 추정 가집계(잠정), confirmed 는 거래소 확정치입니다. "
                "같은 날짜라도 두 값은 다를 수 있으며, 장 마감 후에는 confirmed 를 신뢰하세요."
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
                "종목별 외인기관 추정가집계(HHPTJ04160200)는 실전투자 계정에서만 "
                "제공될 수 있습니다. KIS_ENV=real 인지 확인하세요."
                if self._client.settings.is_demo
                else "KIS 가 반환한 오류 코드를 그대로 전달합니다."
            )
            payload["data_type"] = "provisional_estimate"
            return payload

        rows = data.get("output2") or []
        if isinstance(rows, dict):
            rows = [rows]

        snapshots = []
        for row in rows:
            slot = text(row.get("bsop_hour_gb"))
            snapshots.append(
                {
                    "slot": slot,
                    "approx_kst_time": ESTIMATE_SNAPSHOT_TIMES.get(slot or ""),
                    "foreign_net_qty": integer(row.get("frgn_fake_ntby_qty")),
                    "institution_net_qty": integer(row.get("orgn_fake_ntby_qty")),
                    "combined_net_qty": integer(row.get("sum_fake_ntby_qty")),
                }
            )

        latest = snapshots[-1] if snapshots else None
        return {
            "available": True,
            "data_type": "provisional_estimate",
            "is_confirmed": False,
            "note": _PROVISIONAL_NOTE,
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

        series = []
        for row in rows[: max(1, days)]:
            foreign = integer(row.get("frgn_ntby_qty"))
            institution = integer(row.get("orgn_ntby_qty"))
            # 당일 행은 장 마감 전까지 수급 필드가 빈 문자열로 온다.
            published = foreign is not None or institution is not None
            series.append(
                {
                    "date": yyyymmdd_to_iso(row.get("stck_bsop_date")),
                    "close": num(row.get("stck_clpr")),
                    "change": signed(row.get("prdy_vrss"), row.get("prdy_vrss_sign")),
                    "is_published": published,
                    "foreign_net_qty": foreign,
                    "institution_net_qty": institution,
                    "individual_net_qty": integer(row.get("prsn_ntby_qty")),
                    # KIS 의 *_tr_pbmn 단위는 백만원이다.
                    "foreign_net_value_krw_mn": integer(row.get("frgn_ntby_tr_pbmn")),
                    "institution_net_value_krw_mn": integer(row.get("orgn_ntby_tr_pbmn")),
                    "individual_net_value_krw_mn": integer(row.get("prsn_ntby_tr_pbmn")),
                }
            )

        published = [row for row in series if row["is_published"]]
        pending = [row for row in series if not row["is_published"]]

        result: dict[str, Any] = {
            "available": True,
            "data_type": "confirmed",
            "is_confirmed": True,
            "note": _CONFIRMED_NOTE,
            "value_unit": "순매수 수량은 주, *_value_krw_mn 는 백만원 단위입니다.",
            "latest": published[0] if published else None,
            "series": series,
            "source": _source(TR_INVESTOR_CONFIRMED, self._env),
        }
        if pending:
            result["pending_dates"] = [row["date"] for row in pending]
            result["pending_note"] = (
                f"{', '.join(row['date'] for row in pending)} 의 확정 수급은 아직 "
                "발표되지 않았습니다(장중에는 빈 값). 해당 일자는 provisional 의 "
                "추정 가집계를 참고하세요. latest 는 확정치가 나온 가장 최근 거래일입니다."
            )
        return result

    async def get_daily_ohlcv(
        self, symbol: str, days: int = 30, adjusted: bool = True
    ) -> dict[str, Any]:
        code = _normalise_symbol(symbol)
        days = max(1, min(days, 100))  # KIS 는 1회 호출당 최대 100건

        end = now_kst().date()
        # 거래일 기준이므로 달력일로는 넉넉히 잡는다 (주말/휴일 보정).
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

        # KIS 는 최신 → 과거 순으로 준다. 오래된 순으로 뒤집고 요청 개수만큼 자른다.
        candles.sort(key=lambda c: c["date"])
        candles = candles[-days:]

        return {
            "symbol": code,
            "name": text(head.get("hts_kor_isnm")),
            "as_of_kst": kst_iso(),
            "market_status": await self._calendar.session(),
            "requested_days": days,
            "returned_days": len(candles),
            "price_basis": "수정주가" if adjusted else "원주가",
            "range": {
                "from": candles[0]["date"] if candles else None,
                "to": candles[-1]["date"] if candles else None,
            },
            "candles": candles,
            "source": _source(TR_DAILY_CHART, self._env),
        }

    # ------------------------------------------------------------------
    # 2순위
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
                "note": "장전/장마감 동시호가 구간에서만 의미 있는 값입니다.",
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
            # 코드가 틀리면 KIS 는 rt_cd=0 에 0.00 과 빈 배열을 돌려준다.
            return {
                "error": True,
                "message": (
                    f"지수 코드 {index_code!r} 로 데이터를 받지 못했습니다. "
                    f"알려진 코드: {', '.join(sorted(set(OVERSEAS_INDEX_ALIASES.values())))}"
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
                "해외지수는 일별 차트 API 기반이라 실시간 틱이 아닙니다. "
                "직전 정규장 종가 기준으로 보세요."
            ),
            "source": _source(TR_OVERSEAS_CHART, self._env),
        }

    async def get_overseas_quote(self, ticker: str, exchange: str = "NAS") -> dict[str, Any]:
        symbol = ticker.strip().upper()
        excd = exchange.strip().upper()
        if not symbol:
            raise ValueError("티커가 비어 있습니다.")

        # KIS 는 거래소가 틀려도 rt_cd=0 에 빈 output 을 돌려준다.
        # 값이 비면 다른 미국 거래소를 순서대로 시도한다 (예: TSM 은 NAS 가 아니라 NYS).
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
            # 요청한 거래소가 명시적으로 미국 밖이면 폴백하지 않는다.
            if excd not in US_EXCHANGE_FALLBACKS:
                break

        if not out:
            return {
                "error": True,
                "message": (
                    f"{symbol} 시세를 찾지 못했습니다. 시도한 거래소: {', '.join(tried)}. "
                    "KIS 는 거래소 코드가 맞지 않으면 오류 대신 빈 응답을 돌려줍니다. "
                    "티커 철자와 거래소를 확인하세요."
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
                "note": "KIS 가 적용한 원환산 가격과 환율입니다. ADR-본주 괴리율 계산에 바로 쓸 수 있습니다.",
            },
            "tradable": text(out.get("e_ordyn")),
            "delayed": not realtime,
            "delay_note": (
                "KIS 실시간 해외시세를 신청한 계정으로 설정되어 있습니다 "
                "(KIS_OVERSEAS_REALTIME=true)."
                if realtime
                else "KIS 해외주식 기본 시세는 지연 시세입니다(미국 통상 15분). "
                "실시간 시세를 신청했다면 .env 의 KIS_OVERSEAS_REALTIME=true 로 바꾸세요."
            ),
            "source": _source(TR_OVERSEAS_DETAIL, self._env),
        }

    # ------------------------------------------------------------------
    # 3순위
    # ------------------------------------------------------------------

    async def get_fx(self, pair: str = "USDKRW") -> dict[str, Any]:
        """원달러 환율.

        1차: 해외 기간별시세 API 의 환율 모드(FID_COND_MRKT_DIV_CODE=X)
        2차: 실패 시 해외주식 현재가상세의 원환산 적용환율(t_rate)로 대체
        둘 다 응답에 출처를 명시한다.
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
            "delay_note": "일별 환율 시세 기준입니다. 장중 실시간 호가가 아닙니다.",
            "source": _source(TR_OVERSEAS_CHART, self._env),
        }

    async def _fx_from_applied_rate(
        self, pair: str, primary_error: KisError | None
    ) -> dict[str, Any]:
        """대체 경로: 해외주식 현재가상세가 돌려주는 원환산 적용환율을 쓴다."""
        fallback: dict[str, Any] = {
            "pair": "USD/KRW",
            "as_of_kst": kst_iso(),
            "method": "kis_applied_rate_from_price_detail",
            "method_note": (
                "환율 전용 시세 조회가 실패해 해외주식 현재가상세(HHDFS76200200)의 "
                "원환산 적용환율(t_rate)로 대체했습니다. KIS 가 원화 환산에 실제로 "
                "적용하는 환율이므로 ADR 괴리율 계산에는 오히려 정합적입니다."
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
    # 부가
    # ------------------------------------------------------------------

    async def get_market_status(self) -> dict[str, Any]:
        session = await self._calendar.session()
        return {
            "as_of_kst": kst_iso(),
            "krx": session,
            "kis_env": self._env,
            "throttle_per_second": self._client.settings.kis_rate_limit_per_sec,
        }

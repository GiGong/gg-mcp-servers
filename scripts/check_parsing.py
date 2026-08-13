#!/usr/bin/env python
"""Regression test for KIS response field mapping. No credentials, no network.

Swaps KisClient.get for synthetic fixtures and exercises every tool, catching
broken field names, lost signs, wrong ordering and unit conversion errors.

    PYTHONPATH=src .venv/bin/python scripts/check_parsing.py
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, "src")

_TMP = tempfile.mkdtemp()
os.environ.update(
    {
        "KIS_ENV": "real",
        "KIS_APP_KEY": "FIXTURE-KEY",
        "KIS_APP_SECRET": "FIXTURE-SECRET",
        "AUTH_MODE": "none",
        "OAUTH_STORE": f"{_TMP}/oauth.json",
        "KIS_TOKEN_CACHE": f"{_TMP}/token.json",
        "LOG_FILE": "",
        "LOG_LEVEL": "ERROR",
    }
)

from kis_mcp.client import KisClient  # noqa: E402
from kis_mcp.config import load_settings  # noqa: E402
from kis_mcp.market import KST, MarketCalendar  # noqa: E402
from kis_mcp.tools import KisTools  # noqa: E402

_ASK = {f"askp{i}": str(258000 + i * 500) for i in range(1, 11)}
_BID = {f"bidp{i}": str(257500 - i * 500) for i in range(1, 11)}
_ASK_Q = {f"askp_rsqn{i}": str(1000 * i) for i in range(1, 11)}
_BID_Q = {f"bidp_rsqn{i}": str(900 * i) for i in range(1, 11)}
_ICDC = {f"{s}_rsqn_icdc{i}": "0" for s in ("askp", "bidp") for i in range(1, 11)}

FIXTURES: dict[str, dict] = {
    "FHKST01010100": {
        "rt_cd": "0",
        "output": {
            "stck_prpr": "258000", "stck_oprc": "251000", "stck_hgpr": "259500",
            "stck_lwpr": "250500", "stck_sdpr": "249000", "prdy_vrss": "9000",
            "prdy_vrss_sign": "2", "prdy_ctrt": "3.61", "acml_vol": "4821330",
            "acml_tr_pbmn": "1234567890000", "hts_avls": "1878000", "per": "12.3",
            "pbr": "2.1", "eps": "20900", "bps": "122000", "lstn_stcn": "728002365",
            "vol_tnrt": "0.66", "w52_hgpr": "268000", "w52_lwpr": "151000",
            "rprs_mrkt_kor_name": "KOSPI", "bstp_kor_isnm": "Semiconductors",
            "iscd_stat_cls_code": "55",
        },
    },
    "FHKST03010100": {
        "rt_cd": "0",
        "output1": {"hts_kor_isnm": "SK하이닉스"},
        "output2": [
            {"stck_bsop_date": "20260812", "stck_oprc": "251000", "stck_hgpr": "259500",
             "stck_lwpr": "250500", "stck_clpr": "258000", "acml_vol": "4821330",
             "acml_tr_pbmn": "1", "prdy_vrss": "9000", "prdy_vrss_sign": "2"},
            {"stck_bsop_date": "20260811", "stck_oprc": "247000", "stck_hgpr": "250000",
             "stck_lwpr": "245000", "stck_clpr": "249000", "acml_vol": "3900000",
             "acml_tr_pbmn": "1", "prdy_vrss": "2000", "prdy_vrss_sign": "5"},
        ],
    },
    "HHPTJ04160200": {
        "rt_cd": "0",
        "output2": [
            {"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "120000",
             "orgn_fake_ntby_qty": "-30000", "sum_fake_ntby_qty": "90000"},
            {"bsop_hour_gb": "4", "frgn_fake_ntby_qty": "305000",
             "orgn_fake_ntby_qty": "-52000", "sum_fake_ntby_qty": "253000"},
        ],
    },
    "FHKST01010900": {
        "rt_cd": "0",
        "output": [
            # During the session the current day's flow fields arrive empty
            # (verified against the live API).
            {"stck_bsop_date": "20260814", "stck_clpr": "262000", "prdy_vrss": "4000",
             "prdy_vrss_sign": "2", "prsn_ntby_qty": "", "frgn_ntby_qty": "",
             "orgn_ntby_qty": "", "prsn_ntby_tr_pbmn": "", "frgn_ntby_tr_pbmn": "",
             "orgn_ntby_tr_pbmn": ""},
            {"stck_bsop_date": "20260812", "stck_clpr": "258000", "prdy_vrss": "9000",
             "prdy_vrss_sign": "2", "prsn_ntby_qty": "-210000", "frgn_ntby_qty": "288000",
             "orgn_ntby_qty": "-78000", "prsn_ntby_tr_pbmn": "-540000",
             "frgn_ntby_tr_pbmn": "742000", "orgn_ntby_tr_pbmn": "-201000"},
        ],
    },
    "CTCA0903R": {"rt_cd": "0", "output": [{"bass_dt": "20260812", "opnd_yn": "Y"}]},
    "FHKST01010200": {
        "rt_cd": "0",
        "output1": {**_ASK, **_BID, **_ASK_Q, **_BID_Q, **_ICDC,
                    "total_askp_rsqn": "55000", "total_bidp_rsqn": "49500",
                    "aspr_acpt_hour": "131530", "ntby_aspr_rsqn": "-5500"},
        "output2": {"stck_prpr": "258000", "antc_cnpr": "0", "antc_vol": "0"},
    },
    "FHPUP02100000": {
        "rt_cd": "0",
        "output": {"bstp_nmix_prpr": "2731.55", "bstp_nmix_oprc": "2710.10",
                   "bstp_nmix_hgpr": "2740.00", "bstp_nmix_lwpr": "2705.00",
                   "bstp_nmix_prdy_vrss": "21.45", "prdy_vrss_sign": "2",
                   "bstp_nmix_prdy_ctrt": "0.79", "acml_vol": "512000000",
                   "acml_tr_pbmn": "9", "ascn_issu_cnt": "520",
                   "down_issu_cnt": "330", "stnr_issu_cnt": "80"},
    },
    "HHDFS76200200": {
        "rt_cd": "0",
        "output": {"last": "128.44", "base": "125.10", "open": "126.00", "high": "129.30",
                   "low": "125.80", "tvol": "22000000", "tamt": "1", "pvol": "19000000",
                   "tomv": "140000000000", "perx": "22.1", "pbrx": "5.4", "epsx": "5.8",
                   "bpsx": "23.7", "shar": "1100000000", "curr": "USD",
                   "t_xprc": "178500", "p_xprc": "173800", "t_rate": "1389.5",
                   "p_rate": "1389.0", "h52p": "140.00", "l52p": "70.10", "e_ordyn": "Y"},
    },
    "FHKST03030100": {
        "rt_cd": "0",
        "output1": {"ovrs_nmix_prpr": "5820.11", "ovrs_nmix_prdy_clpr": "5750.00",
                    "ovrs_nmix_prdy_vrss": "70.11", "prdy_vrss_sign": "2",
                    "prdy_ctrt": "1.22", "hts_kor_isnm": "필라델피아반도체"},
        "output2": [{"stck_bsop_date": "20260811", "ovrs_nmix_prpr": "5750.00",
                     "ovrs_nmix_oprc": "5700", "ovrs_nmix_hgpr": "5760",
                     "ovrs_nmix_lwpr": "5690"}],
    },
}


async def _fake_get(self, path, *, tr_id, params, tr_cont=""):  # noqa: ANN001
    if tr_id not in FIXTURES:
        raise AssertionError(f"no fixture for tr_id={tr_id} ({path})")
    return dict(FIXTURES[tr_id])


_VALID_PHASES = {
    "holiday", "weekend", "before_pre_open", "pre_open_auction",
    "regular", "after_hours", "closed",
}

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
_failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _failures
    if not condition:
        _failures += 1
    print(f"{PASS if condition else FAIL}  {label}{(' — ' + detail) if detail else ''}")


async def main() -> int:
    KisClient.get = _fake_get  # type: ignore[method-assign]
    settings = load_settings(env_file="/nonexistent")
    client = KisClient(settings)
    tools = KisTools(client, MarketCalendar(client))

    print("\nField mapping regression test (synthetic fixtures)\n" + "-" * 58)

    quote = await tools.get_quote("000660")
    check("get_quote resolves the security name", quote["name"] == "SK하이닉스", str(quote["name"]))
    check("get_quote price and percent change",
          quote["price"]["current"] == 258000.0 and quote["change"]["percent"] == 3.61)
    check("get_quote converts market cap to KRW",
          quote["valuation"]["market_cap_krw"] == 187_800_000_000_000)
    # Session state depends on wall-clock time, so asserting a specific phase
    # here would be flaky. Check only that the block is present and valid; the
    # phase logic itself is verified below against fixed timestamps.
    status = quote["market_status"]
    check("get_quote includes the session block",
          status["phase"] in _VALID_PHASES
          and isinstance(status["trading_day"], bool)
          and status["kst_time"].endswith(("+09:00", "+0900")),
          f"phase={status['phase']}")

    flow = await tools.get_investor_flow("000660")
    check("investor_flow separates provisional from confirmed",
          flow["provisional"]["is_confirmed"] is False
          and flow["confirmed"]["is_confirmed"] is True)
    check("investor_flow maps provisional snapshot times",
          flow["provisional"]["latest"]["approx_kst_time"] == "13:20")
    check("investor_flow confirmed foreign net buying",
          flow["confirmed"]["latest"]["foreign_net_qty"] == 288000)
    check("investor_flow preserves negative institutional flow",
          flow["confirmed"]["latest"]["institution_net_qty"] == -78000)
    check("investor_flow labels value in millions of KRW",
          flow["confirmed"]["latest"]["foreign_net_value_krw_mn"] == 742000)
    check("investor_flow moves unpublished days to pending",
          flow["confirmed"].get("pending_dates") == ["2026-08-14"],
          str(flow["confirmed"].get("pending_dates")))
    check("investor_flow latest is the newest confirmed day",
          flow["confirmed"]["latest"]["date"] == "2026-08-12")

    ohlcv = await tools.get_daily_ohlcv("000660", 30)
    check("daily_ohlcv is ordered oldest to newest",
          ohlcv["candles"][0]["date"] == "2026-08-11"
          and ohlcv["candles"][-1]["date"] == "2026-08-12")
    check("daily_ohlcv restores negative sign for down days",
          ohlcv["candles"][0]["change"] == -2000.0,
          str(ohlcv["candles"][0]["change"]))

    book = await tools.get_orderbook("000660")
    check("orderbook totals and ratio", book["totals"]["bid_to_ask_ratio"] == 0.9)
    check("orderbook returns ten levels", len(book["asks"]) == 10 and len(book["bids"]) == 10)

    kospi = await tools.get_index("KOSPI")
    check("get_index resolves the domestic alias", kospi["index_code"] == "0001")
    check("get_index advancing issue count", kospi["breadth"]["advancing"] == 520)

    sox = await tools.get_index("SOX")
    check("get_index resolves SOX with no leading dot",
          sox["index_code"] == "SOX" and sox["scope"] == "overseas",
          str(sox.get("index_code")))
    check("get_index flags overseas as delayed", sox["delayed"] is True)

    overseas = await tools.get_overseas_quote("MU")
    check("overseas change calculation", overseas["change"]["value"] == 3.34,
          str(overseas["change"]))
    check("overseas exposes the applied FX rate",
          overseas["krw_converted"]["fx_rate_today"] == 1389.5)
    check("overseas defaults to delayed", overseas["delayed"] is True)

    # Verify the phase logic against fixed timestamps. Deterministic regardless
    # of when this runs.
    calendar = MarketCalendar(client)
    for label, moment, expected in (
        ("regular session", dt.datetime(2026, 8, 13, 10, 30, tzinfo=KST), "regular"),
        ("pre-open auction", dt.datetime(2026, 8, 13, 8, 45, tzinfo=KST), "pre_open_auction"),
        ("after hours", dt.datetime(2026, 8, 13, 16, 0, tzinfo=KST), "after_hours"),
        ("late night", dt.datetime(2026, 8, 13, 23, 30, tzinfo=KST), "closed"),
        ("weekend", dt.datetime(2026, 8, 15, 10, 30, tzinfo=KST), "weekend"),
    ):
        session = await calendar.session(moment)
        check(f"session phase - {label}", session["phase"] == expected,
              f"{session['phase']} (expected {expected})")

    await client.aclose()
    print("-" * 58)
    if _failures:
        print(f"{_failures} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

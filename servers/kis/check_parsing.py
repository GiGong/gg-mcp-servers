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
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

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
from kis_mcp import tools as tools_module  # noqa: E402
from kis_mcp.krx import KrxClient  # noqa: E402
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
        # Newest slot first, as the live API returns it (verified 2026-11-03).
        "output2": [
            {"bsop_hour_gb": "4", "frgn_fake_ntby_qty": "305000",
             "orgn_fake_ntby_qty": "-52000", "sum_fake_ntby_qty": "253000"},
            {"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "120000",
             "orgn_fake_ntby_qty": "-30000", "sum_fake_ntby_qty": "90000"},
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


FIXTURES["FHKST01010300"] = {
    "rt_cd": "0",
    "output": [{"stck_cntg_hour": "131530", "stck_prpr": "258000"}],
}
FIXTURES["FHPST01060000"] = {"rt_cd": "0", "output2": []}

# Per-scenario replacements for FIXTURES, and a log of every call made.
OVERRIDES: dict[str, dict] = {}
CALLS: list[tuple[str, dict]] = []


async def _fake_get(self, path, *, tr_id, params, tr_cont=""):  # noqa: ANN001
    CALLS.append((tr_id, dict(params)))
    source = OVERRIDES.get(tr_id) or FIXTURES.get(tr_id)
    if source is None:
        raise AssertionError(f"no fixture for tr_id={tr_id} ({path})")
    return dict(source)


def _daily(*rows: tuple[str, str, str]) -> dict:
    """Daily-chart fixture from (yyyymmdd, close, volume), newest first."""
    return {
        "rt_cd": "0",
        "output1": {"hts_kor_isnm": "SK하이닉스"},
        "output2": [
            {"stck_bsop_date": d, "stck_clpr": c, "acml_vol": v} for d, c, v in rows
        ],
    }


def _ticks(*rows: tuple[str, str]) -> dict:
    return {"rt_cd": "0", "output2": [{"stck_cntg_hour": t, "stck_prpr": p} for t, p in rows]}


def _recent(*rows: tuple[str, str]) -> dict:
    return {"rt_cd": "0", "output": [{"stck_cntg_hour": t, "stck_prpr": p} for t, p in rows]}


# 2026-11-03 SK hynix, as observed on the live API. The daily row for the day
# follows after-hours trades (1,396,000 at 16:28) while the closing auction
# printed 1,398,000 at 15:30:24.
NOV3_DAILY = _daily(
    ("20261103", "1396000", "2225437"),
    ("20261102", "1472000", "2594536"),
    ("20261030", "1418000", "3859903"),
)
NOV3_TICKS = _ticks(
    ("153024", "1398000"),
    ("151959", "1388000"),
    ("151959", "1387000"),
    ("151950", "1388000"),
)
NOV3_QUOTE = {"rt_cd": "0", "output": {"stck_prpr": "1396000", "stck_sdpr": "1472000"}}


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
    # Pin the tools' clock to the day after the fixtures' newest row.
    real_now = tools_module.now_kst
    tools_module.now_kst = lambda: dt.datetime(2026, 8, 13, 10, 30, tzinfo=KST)

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
    check("investor_flow orders provisional snapshots oldest to newest",
          [s["slot"] for s in flow["provisional"]["snapshots"]] == ["1", "4"])
    check("investor_flow confirmed note has no same-day caveat on a past day",
          "preliminary" not in flow["confirmed"]["note"])
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
        ("after hours runs to 20:00", dt.datetime(2026, 8, 13, 19, 30, tzinfo=KST), "after_hours"),
        ("late night", dt.datetime(2026, 8, 13, 20, 30, tzinfo=KST), "closed"),
        ("weekend", dt.datetime(2026, 8, 15, 10, 30, tzinfo=KST), "weekend"),
        ("exam day 09:45 is pre-open", dt.datetime(2026, 11, 19, 9, 45, tzinfo=KST), "pre_open_auction"),
        ("exam day 16:00 is still regular", dt.datetime(2026, 11, 19, 16, 0, tzinfo=KST), "regular"),
        ("exam day 16:45 is after hours", dt.datetime(2026, 11, 19, 16, 45, tzinfo=KST), "after_hours"),
    ):
        session = await calendar.session(moment)
        check(f"session phase - {label}", session["phase"] == expected,
              f"{session['phase']} (expected {expected})")

    after = await calendar.session(dt.datetime(2026, 8, 13, 16, 0, tzinfo=KST))
    check("after-hours note says the price is not the close",
          "not the regular-session close" in after["note"], after["note"])
    exam = await calendar.session(dt.datetime(2026, 11, 19, 16, 0, tzinfo=KST))
    check("exam day note names the shifted hours",
          "one hour late" in exam["note"] and "10:00-16:30" in exam["note"], exam["note"])

    # The same-day caveat depends on today's date, so move the clock to the
    # fixture's newest confirmed day.
    tools_module.now_kst = lambda: dt.datetime(2026, 8, 12, 18, 0, tzinfo=KST)
    same_day = await tools.get_investor_flow("000660")
    check("investor_flow flags same-day confirmed figures as preliminary",
          "preliminary" in same_day["confirmed"]["note"]
          and same_day["confirmed"]["series"][1]["preliminary"] is True
          and same_day["confirmed"]["series"][0]["preliminary"] is False)

    print("\nRegular close vs latest trade (fixtures from live observations)\n" + "-" * 58)

    def at(*parts: int) -> dt.datetime:
        return dt.datetime(*parts, tzinfo=KST)

    def scenario(now: dt.datetime, overrides: dict, krx: KrxClient | None = None) -> KisTools:
        OVERRIDES.clear()
        OVERRIDES.update(overrides)
        CALLS.clear()
        tools_module.now_kst = lambda: now
        return KisTools(client, MarketCalendar(client), krx)

    nov3 = {"FHKST03010100": NOV3_DAILY, "FHPST01060000": NOV3_TICKS,
            "FHKST01010100": NOV3_QUOTE,
            "FHKST01010300": _recent(("162807", "1396000"), ("162800", "1397000"))}

    t = scenario(at(2026, 11, 3, 16, 28, 7), nov3)
    quote = await t.get_quote("000660")
    close, latest = quote["close"], quote["latest_trade"]
    check("11-03 16:28: close is the 15:30:24 print, not the current price",
          close["trade_date"] == "2026-11-03" and close["price"] == 1398000.0
          and close["status"] == "PROVISIONAL"
          and close["trade_time_kst"] == "2026-11-03T15:30:24+09:00", str(close))
    check("11-03 16:28: latest trade is the after-hours price with its own time",
          latest["price"] == 1396000.0 and latest["session"] == "AFTER_HOURS"
          and latest["venue"] == "UNKNOWN"
          and latest["as_of_kst"] == "2026-11-03T16:28:07+09:00", str(latest))
    check("11-03 16:28: price.current keeps the raw KIS value",
          quote["price"]["current"] == 1396000.0)
    check("base_price added; prev_close kept as its alias",
          quote["price"]["base_price"] == 1472000.0 and quote["price"]["prev_close"] == 1472000.0)
    check("no KRX key: check reports NOT_CONFIGURED, not verified",
          close["checks"] == [{"source": "krx_openapi", "compares": "close price only",
                               "result": "NOT_CONFIGURED"}]
          and close["independent_verified"] is False, str(close["checks"]))

    t = scenario(at(2026, 11, 3, 16, 28, 7), nov3)
    final = (await t.get_regular_close("000660", "2026-11-02"))["close"]
    daily_calls = [p for tr, p in CALLS if tr == "FHKST03010100"]
    check("11-02: FINAL from the J daily chart (1,472,000, not UN's 1,480,000)",
          final["status"] == "FINAL" and final["price"] == 1472000.0, str(final))
    # The other daily call is the security-name lookup, which reads no prices.
    close_reads = [p for p in daily_calls if p["FID_INPUT_DATE_1"] == "20261023"]
    check("closes are read unadjusted from the J feed only",
          len(close_reads) == 1 and close_reads[0]["FID_ORG_ADJ_PRC"] == "1"
          and all(p["FID_COND_MRKT_DIV_CODE"] == "J" for p in daily_calls), str(daily_calls))

    for label, ticks, reason in (
        ("a trade inside the closing auction window",
         _ticks(("153024", "1398000"), ("152500", "1390000"), ("151959", "1388000")), "ambiguous"),
        ("a tick page that stops at the close",
         _ticks(("153024", "1398000"), ("153024", "1398000")), "reach back"),
        ("two prices after the close",
         _ticks(("153124", "1399000"), ("153024", "1398000"), ("151959", "1388000")),
         "more than one price"),
    ):
        t = scenario(at(2026, 11, 3, 16, 28, 7), {**nov3, "FHPST01060000": ticks})
        same = (await t.get_regular_close("000660", "2026-11-03"))["close"]
        check(f"11-03: PENDING, not a guess, with {label}",
              same["status"] == "PENDING" and same["price"] is None
              and reason in same.get("next_check", ""), str(same))

    t = scenario(at(2026, 11, 19, 15, 45), {
        "FHKST03010100": _daily(("20261119", "1150000", "900000"), ("20261118", "1144000", "3413164")),
        "FHPST01060000": _ticks(("153024", "1150000"), ("151959", "1149000")),
    })
    exam = (await t.get_regular_close("000660", "2026-11-19"))["close"]
    check("exam day 15:45: still PENDING (regular session runs to 16:30)",
          exam["status"] == "PENDING" and "16:30" in exam.get("next_check", ""), str(exam))

    for raw in ("195900", "080500"):
        t = scenario(at(2026, 11, 9, 8, 10), {
            "FHKST03010100": _daily(("20261106", "1150000", "3000000"), ("20261105", "1140000", "3100000")),
            "FHKST01010300": _recent((raw, "1150000")),
        })
        monday = await t.get_quote("000660")
        check(f"Monday 08:10: previous session's tick {raw} is not dated today",
              monday["latest_trade"]["as_of_kst"] is None
              and monday["latest_trade"]["session"] == "UNKNOWN"
              and monday["latest_trade"]["trade_time_raw"] == raw, str(monday["latest_trade"]))
    check("Monday 08:10: Friday's close waits for the next row (PENDING)",
          monday["close"]["trade_date"] == "2026-11-06" and monday["close"]["status"] == "PENDING",
          str(monday["close"]))

    t = scenario(at(2026, 11, 3, 16, 28, 7), {
        "FHKST03010100": _daily(("20261103", "1396000", "2225437"), ("20261102", "1472000", "0"))})
    halted = (await t.get_regular_close("000660", "2026-11-02"))["close"]
    check("a settled day with zero volume is NO_TRADE and keeps the official price",
          halted["status"] == "NO_TRADE" and halted["price"] == 1472000.0, str(halted))

    t = scenario(at(2026, 11, 3, 16, 28, 7), nov3)
    future = (await t.get_regular_close("000660", "2026-11-04"))["close"]
    check("a future date is a non-retryable ERROR",
          future["status"] == "ERROR" and future["error"]["retryable"] is False)

    t = scenario(at(2026, 11, 3, 16, 28, 7), {
        **nov3, "CTCA0903R": {"rt_cd": "0", "output": [{"bass_dt": "20261101", "opnd_yn": "N"}]}})
    sunday = (await t.get_regular_close("000660", "2026-11-01"))["close"]
    check("a closed market day is NOT_TRADING_DAY", sunday["status"] == "NOT_TRADING_DAY",
          str(sunday))

    for label, outcomes, expected, verified in (
        ("KRX agrees", [({"000660": 1472000.0}, "", None)], "MATCH", True),
        ("KRX disagrees", [({"000660": 1471000.0}, "", None)], "MISMATCH", False),
        ("KRX not published yet", [(None, "NOT_PUBLISHED", None)] * 2, "NOT_PUBLISHED", False),
        ("KOSDAQ dataset not approved",
         [({"005930": 1.0}, "", None), (None, "NOT_AUTHORIZED", "Unauthorized API Call")],
         "NOT_AUTHORIZED", False),
    ):
        krx = KrxClient("fixture-key")
        replies = iter(outcomes)

        async def fake_dataset(dataset, day, _replies=replies):  # noqa: ANN001
            return next(_replies)

        krx._dataset = fake_dataset  # type: ignore[method-assign]
        t = scenario(at(2026, 11, 3, 16, 28, 7), nov3, krx)
        checked = (await t.get_regular_close("000660", "2026-11-02"))["close"]
        await krx.aclose()
        check(f"KRX check: {label} -> {expected}",
              checked["status"] == "FINAL" and checked["checks"][0]["result"] == expected
              and checked["independent_verified"] is verified, str(checked["checks"]))

    # Exam day 2026-11-19 as observed at 10:52: slot 2 present, slot 1 has a
    # zero institutional figure because institutions are not compiled yet.
    exam_slots = {"rt_cd": "0", "output2": [
        {"bsop_hour_gb": "2", "frgn_fake_ntby_qty": "-00000000000022000",
         "orgn_fake_ntby_qty": "-00000000000038000", "sum_fake_ntby_qty": "-00000000000060000"},
        {"bsop_hour_gb": "1", "frgn_fake_ntby_qty": "000000000000008000",
         "orgn_fake_ntby_qty": "000000000000000000", "sum_fake_ntby_qty": "000000000000008000"},
    ]}
    t = scenario(at(2026, 11, 19, 10, 52), {"HHPTJ04160200": exam_slots})
    prov = (await t.get_investor_flow("000660"))["provisional"]
    first, latest = prov["snapshots"][0], prov["latest"]
    check("slot 1 institution 0 is reported as not compiled (null)",
          first["slot"] == "1" and first["institution_net_qty"] is None
          and first["combined_net_qty"] is None and first["foreign_net_qty"] == 8000, str(first))
    check("shifted-hours day: slot times are null, latest is the newest slot",
          latest["slot"] == "2" and latest["approx_kst_time"] is None
          and latest["institution_net_qty"] == -38000 and "shifted hours" in prov["note"], str(latest))

    OVERRIDES.clear()
    tools_module.now_kst = real_now

    await client.aclose()
    print("-" * 58)
    if _failures:
        print(f"{_failures} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

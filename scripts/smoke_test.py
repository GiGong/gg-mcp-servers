#!/usr/bin/env python
"""KIS 실호출 검증. .env 의 실제 자격증명이 필요하다.

    PYTHONPATH=src .venv/bin/python scripts/smoke_test.py
    PYTHONPATH=src .venv/bin/python scripts/smoke_test.py --tier 1   # 1순위만

각 도구를 실제로 한 번씩 호출하고 핵심 필드가 채워졌는지 확인한다.
KIS 오류는 삼키지 않고 코드/메시지를 그대로 출력한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

sys.path.insert(0, "src")

from kis_mcp.client import KisClient  # noqa: E402
from kis_mcp.config import load_settings  # noqa: E402
from kis_mcp.logging_setup import setup_logging  # noqa: E402
from kis_mcp.market import MarketCalendar  # noqa: E402
from kis_mcp.tools import KisTools  # noqa: E402

OK = "\033[32m OK \033[0m"
BAD = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

SKHY = "000660"  # SK하이닉스
SEC = "005930"  # 삼성전자


def dig(payload: dict[str, Any], path: str) -> Any:
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.warnings = 0

    async def run(
        self,
        label: str,
        coro,
        *,
        expect: list[str],
        allow_error: bool = False,
    ) -> dict[str, Any] | None:
        try:
            result = await coro
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            print(f"{BAD}  {label}\n        예외: {type(exc).__name__}: {exc}")
            return None

        if result.get("error"):
            marker, note = (WARN, "KIS 오류") if allow_error else (BAD, "KIS 오류")
            if allow_error:
                self.warnings += 1
            else:
                self.failures += 1
            print(
                f"{marker}  {label}\n        {note}: "
                f"{result.get('kis_msg_cd')} / {result.get('kis_msg1')}"
            )
            return result

        missing = [p for p in expect if dig(result, p) in (None, [], "")]
        if missing:
            self.failures += 1
            print(f"{BAD}  {label}\n        비어 있는 필드: {', '.join(missing)}")
        else:
            summary = " | ".join(f"{p.split('.')[-1]}={dig(result, p)}" for p in expect[:4])
            print(f"{OK}  {label}\n        {summary}")
        return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", type=int, default=3, help="1=1순위만, 2=2순위까지, 3=전부")
    parser.add_argument("--json", action="store_true", help="전체 응답을 JSON 으로 덤프")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging("WARNING", None)

    print(f"\nKIS 실호출 검증  env={settings.kis_env}  appkey={settings.masked_app_key()}")
    print(f"스로틀={settings.kis_rate_limit_per_sec}/s  타임아웃={settings.kis_timeout}s")
    print("-" * 70)

    client = KisClient(settings)
    tools = KisTools(client, MarketCalendar(client))
    report = Report()
    dumps: dict[str, Any] = {}

    try:
        try:
            await client.warmup()
            print(f"{OK}  접근토큰 발급/캐시")
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD}  접근토큰 발급 실패: {exc}")
            return 1

        status = await report.run(
            "장 상태 (chk-holiday CTCA0903R)",
            tools.get_market_status(),
            expect=["krx.phase", "krx.trading_day", "as_of_kst"],
        )
        dumps["market_status"] = status

        print("\n[1순위]")
        dumps["quote_skhy"] = await report.run(
            f"get_quote({SKHY}) SK하이닉스",
            tools.get_quote(SKHY),
            expect=["name", "price.current", "change.percent", "volume.shares",
                    "price.prev_close", "valuation.market_cap_krw_eok"],
        )
        dumps["quote_sec"] = await report.run(
            f"get_quote({SEC}) 삼성전자",
            tools.get_quote(SEC),
            expect=["name", "price.current", "change.percent", "volume.shares"],
        )
        dumps["flow"] = await report.run(
            f"get_investor_flow({SKHY})",
            tools.get_investor_flow(SKHY),
            expect=["confirmed.latest.date", "confirmed.latest.foreign_net_qty",
                    "confirmed.latest.institution_net_qty"],
        )
        if dumps["flow"] and not dumps["flow"].get("error"):
            prov = dumps["flow"].get("provisional", {})
            if prov.get("available"):
                print(f"        └ 잠정(추정 가집계): latest={prov.get('latest')}")
            else:
                report.warnings += 1
                print(
                    f"{WARN}  └ 추정 가집계(HHPTJ04160200) 미제공: "
                    f"{prov.get('kis_msg_cd')} / {prov.get('kis_msg1')}"
                )
            conf = dumps["flow"].get("confirmed", {})
            if conf.get("pending_dates"):
                print(f"        └ 확정치 미발표 일자: {conf['pending_dates']}")

        dumps["ohlcv"] = await report.run(
            f"get_daily_ohlcv({SKHY}, 30)",
            tools.get_daily_ohlcv(SKHY, 30),
            expect=["returned_days", "range.from", "range.to", "candles"],
        )
        if dumps["ohlcv"] and dumps["ohlcv"].get("candles"):
            print(f"        └ 마지막 캔들: {dumps['ohlcv']['candles'][-1]}")

        if args.tier >= 2:
            print("\n[2순위]")
            dumps["orderbook"] = await report.run(
                f"get_orderbook({SKHY})",
                tools.get_orderbook(SKHY),
                expect=["totals.ask_quantity", "totals.bid_quantity", "asks", "bids"],
            )
            dumps["kospi"] = await report.run(
                "get_index(KOSPI)",
                tools.get_index("KOSPI"),
                expect=["price.current", "change.percent"],
            )
            dumps["kosdaq"] = await report.run(
                "get_index(KOSDAQ)",
                tools.get_index("KOSDAQ"),
                expect=["price.current", "change.percent"],
            )
            for alias in ("SOX", "COMP", "SPX"):
                dumps[f"idx_{alias}"] = await report.run(
                    f"get_index({alias}) 해외지수",
                    tools.get_index(alias),
                    expect=["price.current", "index_code"],
                )
            # TSM 은 NYSE 상장이다. NAS 로 요청해도 폴백이 찾아내야 한다.
            for ticker in ("MU", "NVDA", "TSM", "SKHY"):
                result = await report.run(
                    f"get_overseas_quote({ticker}, NAS)",
                    tools.get_overseas_quote(ticker, "NAS"),
                    expect=["price.current", "price.prev_close",
                            "krw_converted.fx_rate_today"],
                )
                dumps[f"os_{ticker}"] = result
                if result and result.get("exchange_resolved_by_fallback"):
                    print(f"        └ 거래소 폴백으로 해결: {result['exchange']}")

        if args.tier >= 3:
            print("\n[3순위]")
            dumps["fx"] = await report.run(
                "get_fx(USDKRW)",
                tools.get_fx("USDKRW"),
                expect=["rate", "method"],
            )
    finally:
        await client.aclose()

    print("-" * 70)
    if args.json:
        print(json.dumps(dumps, ensure_ascii=False, indent=2, default=str))

    if report.failures:
        print(f"실패 {report.failures}건, 경고 {report.warnings}건")
        return 1
    print(f"전부 통과 (경고 {report.warnings}건)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

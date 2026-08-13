#!/usr/bin/env python
"""Live KIS verification. Requires real credentials in .env.

    PYTHONPATH=src .venv/bin/python scripts/smoke_test.py
    PYTHONPATH=src .venv/bin/python scripts/smoke_test.py --tier 1   # primary only

Calls each tool once against the live API and checks that key fields are
populated. KIS errors are printed with their original code and message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from kis_mcp.client import KisClient  # noqa: E402
from kis_mcp.config import load_settings  # noqa: E402
from kis_mcp.logging_setup import setup_logging  # noqa: E402
from kis_mcp.market import MarketCalendar  # noqa: E402
from kis_mcp.tools import KisTools  # noqa: E402

OK = "\033[32m OK \033[0m"
BAD = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

SKHY = "000660"  # SK hynix
SEC = "005930"  # Samsung Electronics


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
            print(f"{BAD}  {label}\n        exception: {type(exc).__name__}: {exc}")
            return None

        if result.get("error"):
            marker, note = (WARN, "KIS error") if allow_error else (BAD, "KIS error")
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
            print(f"{BAD}  {label}\n        empty fields: {', '.join(missing)}")
        else:
            summary = " | ".join(f"{p.split('.')[-1]}={dig(result, p)}" for p in expect[:4])
            print(f"{OK}  {label}\n        {summary}")
        return result


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", type=int, default=3, help="1=primary only, 2=through secondary, 3=all")
    parser.add_argument("--json", action="store_true", help="dump the full responses as JSON")
    args = parser.parse_args()

    settings = load_settings()
    setup_logging("WARNING", None)

    print(f"\nLive KIS check  env={settings.kis_env}  appkey={settings.masked_app_key()}")
    print(f"throttle={settings.kis_rate_limit_per_sec}/s  timeout={settings.kis_timeout}s")
    print("-" * 70)

    client = KisClient(settings)
    tools = KisTools(client, MarketCalendar(client))
    report = Report()
    dumps: dict[str, Any] = {}

    try:
        try:
            await client.warmup()
            print(f"{OK}  access token issued and cached")
        except Exception as exc:  # noqa: BLE001
            print(f"{BAD}  access token issuance failed: {exc}")
            return 1

        status = await report.run(
            "market status (chk-holiday CTCA0903R)",
            tools.get_market_status(),
            expect=["krx.phase", "krx.trading_day", "as_of_kst"],
        )
        dumps["market_status"] = status

        print("\n[primary]")
        dumps["quote_skhy"] = await report.run(
            f"get_quote({SKHY}) SK hynix",
            tools.get_quote(SKHY),
            expect=["name", "price.current", "change.percent", "volume.shares",
                    "price.prev_close", "valuation.market_cap_krw_eok"],
        )
        dumps["quote_sec"] = await report.run(
            f"get_quote({SEC}) Samsung Electronics",
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
                print(f"        - provisional estimate: latest={prov.get('latest')}")
            else:
                report.warnings += 1
                print(
                    f"{WARN}  - provisional estimate (HHPTJ04160200) unavailable: "
                    f"{prov.get('kis_msg_cd')} / {prov.get('kis_msg1')}"
                )
            conf = dumps["flow"].get("confirmed", {})
            if conf.get("pending_dates"):
                print(f"        - confirmed data pending for: {conf['pending_dates']}")

        dumps["ohlcv"] = await report.run(
            f"get_daily_ohlcv({SKHY}, 30)",
            tools.get_daily_ohlcv(SKHY, 30),
            expect=["returned_days", "range.from", "range.to", "candles"],
        )
        if dumps["ohlcv"] and dumps["ohlcv"].get("candles"):
            print(f"        - last candle: {dumps['ohlcv']['candles'][-1]}")

        if args.tier >= 2:
            print("\n[secondary]")
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
                    f"get_index({alias}) overseas index",
                    tools.get_index(alias),
                    expect=["price.current", "index_code"],
                )
            # TSM lists on NYSE. Requesting NAS should still resolve via fallback.
            for ticker in ("MU", "NVDA", "TSM", "SKHY"):
                result = await report.run(
                    f"get_overseas_quote({ticker}, NAS)",
                    tools.get_overseas_quote(ticker, "NAS"),
                    expect=["price.current", "price.prev_close",
                            "krw_converted.fx_rate_today"],
                )
                dumps[f"os_{ticker}"] = result
                if result and result.get("exchange_resolved_by_fallback"):
                    print(f"        - resolved via exchange fallback: {result['exchange']}")

        if args.tier >= 3:
            print("\n[optional]")
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
        print(f"{report.failures} failed, {report.warnings} warnings")
        return 1
    print(f"all passed ({report.warnings} warnings)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

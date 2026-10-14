#!/usr/bin/env python
"""Regression test for response parsing. No credentials, no network.

Runs every service against recorded API responses in fixtures/ (character
names, guild, server and estate masked; large sections trimmed) and checks the
behaviour found against the live API: label-based tooltip parsing, per-bundle
prices, null result lists, option value codes and the both-bounds rule.

    .venv/bin/python servers/lostark/check_parsing.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile
import time
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "src"))

_TMP = pathlib.Path(tempfile.mkdtemp())
os.environ.update(
    {
        "LOSTARK_API_KEY": "FIXTURE-KEY",
        "AUTH_MODE": "none",
        "LOG_FILE": "",
        "LOG_LEVEL": "ERROR",
        "LOSTARK_SNAPSHOT_DB": str(_TMP / "snapshots.sqlite3"),
    }
)

from lostark_mcp import fields, tooltip  # noqa: E402
from lostark_mcp.armory import MAX_RESPONSE_CHARS, SECTIONS, Armory, ArmoryService  # noqa: E402
from lostark_mcp.auction import AuctionService  # noqa: E402
from lostark_mcp.cache import TtlCache  # noqa: E402
from lostark_mcp.catalog import Catalog  # noqa: E402
from lostark_mcp.client import LostarkError  # noqa: E402
from lostark_mcp.config import load_settings  # noqa: E402
from lostark_mcp.craft import CraftService  # noqa: E402
from lostark_mcp.market import MarketService  # noqa: E402
from lostark_mcp.snapshots import SnapshotStore  # noqa: E402
from lostark_mcp.stats import build_stats  # noqa: E402
from lostark_mcp.throttle import MinuteQuota, QuotaExhausted  # noqa: E402

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
_failures = 0


def check(label: str, condition: bool, detail: Any = "") -> None:
    global _failures
    if not condition:
        _failures += 1
    suffix = f" — {detail}" if detail != "" and not condition else ""
    print(f"{PASS if condition else FAIL}  {label}{suffix}")


def fixture(name: str) -> Any:
    return json.loads((HERE / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))


class FakeClient:
    """Routes calls to fixtures and records POST bodies."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.calls = 0

    def quota(self) -> None:
        return None

    async def get(self, path: str, params: dict | None = None) -> Any:
        self.calls += 1
        if path == "/markets/options":
            return fixture("market_options")
        if path == "/auctions/options":
            return fixture("auction_options")
        if path.startswith("/armories/characters/"):
            return fixture("armory") if "%ED%85%8C%EC%8A%A4%ED%8A%B8" in path else None
        if path.endswith("/siblings"):
            return fixture("siblings")
        if path.startswith("/markets/items/"):
            return fixture("market_history")
        raise AssertionError(f"unexpected GET {path}")

    async def post(self, path: str, body: dict) -> Any:
        self.calls += 1
        self.posts.append((path, body))
        if path == "/markets/items":
            data = fixture("market_search_logging")
            wanted = body.get("ItemName")
            if body.get("CategoryCode") != 90300 and body.get("CategoryCode") != 90000:
                return {"PageNo": 1, "PageSize": 10, "TotalCount": 0, "Items": None}
            if wanted:
                data["Items"] = [i for i in data["Items"] if wanted in i["Name"]]
            return data
        if path == "/auctions/items":
            if body.get("CategoryCode") == 210000:
                return fixture("auction_gems")
            return {"PageNo": 1, "PageSize": 10, "TotalCount": 0, "Items": None}
        raise AssertionError(f"unexpected POST {path}")


def all_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in all_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in all_strings(v)]
    return []


async def main() -> int:
    settings = load_settings(for_server=False)
    client = FakeClient()
    cache = TtlCache()
    catalog = Catalog(client, cache)  # type: ignore[arg-type]
    armory_svc = ArmoryService(client, cache)  # type: ignore[arg-type]
    market = MarketService(client, cache, catalog, settings.data_dir)  # type: ignore[arg-type]
    auction = AuctionService(client, cache, catalog)  # type: ignore[arg-type]
    craft = CraftService(market, settings.data_dir)

    print("== fields / tooltip")
    check("clean strips tags and entities", fields.clean("<FONT COLOR='#fff'>A&amp;B</FONT><BR>C") == "A&B\nC")
    check("num parses thousands separators", fields.num("1,726.66") == 1726.66)
    check("naive KST timestamp gets +09:00", fields.kst_from_naive("2026-10-13T20:52:35.497") == "2026-10-13T20:52:35+09:00")
    check("tooltip parser survives junk", tooltip.parse("not json")[0].type == "Text" and tooltip.parse(None) == [])

    print("== armory")
    ov = await armory_svc.overview("테스트캐릭터")
    ch = ov.get("character", {})
    check("profile name", ch.get("name") == "테스트캐릭터", ch.get("name"))
    check("item level string -> float", ch.get("item_level") == 1700.0, ch.get("item_level"))
    check("combat power string -> float", ch.get("combat_power") == 1800.0, ch.get("combat_power"))
    check("build from ark passive title", ch.get("build") == "잔재된 기운", ch.get("build"))
    check("overview has no markup", not any("<" in s for s in all_strings(ov)))
    check("overview stays small", len(json.dumps(ov, ensure_ascii=False)) < 4000, len(json.dumps(ov, ensure_ascii=False)))
    check(
        "weapon brief line",
        "무기 +19 상급재련40 품질100 (Lv1725)" in ov.get("equipment", []),
        ov.get("equipment"),
    )

    armory = Armory(fixture("armory"))
    weapon = armory.section("equipment", "standard", ["무기"])
    check("match filter keeps one slot", len(weapon) == 1, len(weapon))
    w = weapon[0]
    check(
        "weapon fields",
        (w.get("refine"), w.get("advanced_refine"), w.get("quality"), w.get("tier"), w.get("item_level"))
        == (19, 40, 100, 4, 1725),
        w,
    )
    helmet = armory.section("equipment", "standard", ["투구"])[0]
    neck = armory.section("accessories", "standard", ["목걸이"])[0]
    check("ark points found by label (helmet)", helmet.get("ark_passive_points") == {"진화": 24}, helmet.get("ark_passive_points"))
    check("ark points found by label (necklace)", neck.get("ark_passive_points") == {"깨달음": 13}, neck.get("ark_passive_points"))
    check("necklace polish effects", len(neck.get("effects", {}).get("연마 효과", [])) == 3, neck.get("effects"))
    stone = armory.section("ability_stone", "standard")
    penalties = {e["name"]: e["penalty"] for e in stone.get("engravings", [])}
    check("stone penalty read from colour", penalties.get("방어력 감소") is True and penalties.get("원한") is False, penalties)
    check("stone has no quality (-1 -> None)", "quality" not in stone, stone.get("quality"))
    bracelet = armory.section("bracelet", "standard")
    check("bracelet effects", "특화 +70" in bracelet.get("effects", {}).get("팔찌 효과", []), bracelet.get("effects"))

    gems = armory.section("gems", "standard")
    check("gem summary sorts levels numerically", gems["summary"]["by_level"].startswith("9레벨"), gems["summary"])
    check("gem kind and skill", all(g.get("kind") == "광휘" and g.get("skill") for g in gems["gems"]), gems["gems"][:1])

    invested = armory.section("skills", "standard", None, True)
    everything = armory.section("skills", "standard", None, False)
    check("invested_only hides untouched skills", len(invested) == 2 and len(everything) == 4, (len(invested), len(everything)))
    full = armory.section("skills", "full", ["보이드"], True)[0]
    check("skill tooltip stops before next-level preview", not any("다음 스킬 레벨" in l for l in full["tooltip"]), full["tooltip"][-3:])
    check("tripod tier is 1-based", [t["tier"] for t in full["tripods"]] == [1, 2, 3], full["tripods"])

    ap = armory.section("arkpassive", "standard")
    node = next(n for n in ap["nodes"] if n.get("name") == "잔재된 기운")
    check("ark passive node parsed", (node["tree"], node["tier"], node["level"]) == ("깨달음", 2, 3), node)
    grid = armory.section("arkgrid", "standard")
    core = grid["cores"][0]
    active = {o["points"]: o["active"] for o in core["options"] if "points" in o}
    check("ark grid core split", (core["order"], core["celestial"], core["core"]) == ("질서", "해", "일섬"), core["name"])
    check("ark grid option active by points", active.get(17) is True and active.get(18) is False, active)

    big = await armory_svc.detail("테스트캐릭터", list(SECTIONS), detail="full")
    check("full detail fits the response budget", len(json.dumps(big, ensure_ascii=False)) <= MAX_RESPONSE_CHARS + 2000)

    try:
        await armory_svc.overview("없는캐릭터")
        check("missing character (200 null) -> not_found", False)
    except LostarkError as exc:
        check("missing character (200 null) -> not_found", exc.kind == "not_found", exc.kind)

    roster = await armory_svc.roster("테스트캐릭터")
    rows = roster["servers"]["테스트서버"]
    check("roster sorted by item level", rows[0]["item_level"] >= rows[-1]["item_level"])
    check("roster marks queried character", any(r.get("queried") for r in rows))

    print("== market")
    search = await market.search(category="벌목 전리품")
    wood = next(i for i in search["items"] if i["name"] == "목재")
    check("bundle of 100 -> unit price", wood["bundle_count"] == 100 and wood["unit_price"]["current_min"] == wood["current_min_price"] / 100, wood)
    row, _ = await market.find("아비도스 목재")
    check("find by exact name across categories", row.get("name") == "아비도스 목재", row)
    try:
        await market.find("없는재료")
        check("unknown item -> not_found", False)
    except LostarkError as exc:
        check("unknown item -> not_found", exc.kind == "not_found", exc.kind)
    history = await market.history(6861013)
    days = history["entries"][0]["days"]
    check("history oldest first", days[0]["date"] < days[-1]["date"], (days[0]["date"], days[-1]["date"]))
    check("history summary present", history["entries"][0].get("summary", {}).get("days", 0) >= 13)

    print("== auction")
    gem_page = await auction.search(category="보석", query="겁화", buyout_only=False)
    first = gem_page["items"][0]
    check("gem level from name, not Level", first.get("gem_level") and "item_level" not in first, first)
    with_buyout = await auction.search(category="보석", buyout_only=True)
    check(
        "buyout_only drops null BuyPrice and says so",
        all(i.get("buy_price") for i in with_buyout["items"])
        and any("bid-only" in n for n in with_buyout.get("notices", [])),
        with_buyout.get("notices"),
    )
    empty = await auction.search(category="목걸이")
    check("Items null -> empty list", empty.get("returned") == 0 and empty.get("total_count") == 0, empty)

    neck_cat = await catalog.auction_category("목걸이")
    opt = await catalog.resolve_etc_option("적에게 주는 피해", neck_cat)
    check("tooltip wording resolves to catalog option", opt.name == "적에게 주는 피해 증가", opt.name)
    check("display 1.20% -> code 120", Catalog.encode_value(opt, 1.2, "min") == 120)
    check("min rounds up to the next shown value", Catalog.encode_value(opt, 1.21, "min") == 140)
    check("max rounds down", Catalog.encode_value(opt, 1.19, "max") == 115)
    try:
        await catalog.resolve_etc_option("공격력 %", neck_cat)
        check("earring-only option rejected for necklace", False)
    except LostarkError as exc:
        check("earring-only option rejected for necklace", "귀걸이" in str(exc), str(exc))

    client.posts.clear()
    await auction.search(category="목걸이", options=[{"option": "추가 피해", "min": 1.6}])
    sent = client.posts[-1][1]["EtcOptions"][0]
    check("min-only filter is sent with both bounds", sent.get("MinValue") == 160 and sent.get("MaxValue") is not None, sent)

    print("== craft")
    result = await craft.profit(
        output_item="아비도스 목재",
        output_qty=1,
        inputs=[{"name": "목재", "qty": 10}],
        craft_gold=1,
        sell_fee_rate=0.05,
    )
    wood_unit = wood["unit_price"]["current_min"]
    abidos_unit = row["unit_price"]["current_min"]
    expected = round(abidos_unit * 0.95 - 10 * wood_unit - 1, 2)
    check("craft profit arithmetic", result["per_craft"]["buy_materials_profit"] == expected, (result["per_craft"], expected))
    try:
        await craft.profit(recipe=None, output_item="아비도스 목재", output_qty=1, inputs=[], craft_gold=1)
        check("incomplete recipe rejected", False)
    except LostarkError as exc:
        check("incomplete recipe rejected", exc.kind == "bad_request")

    print("== stats")
    store = SnapshotStore(settings.snapshot_db)
    doc = armory.snapshot_doc()
    store.save(doc, source="rankers")
    other = json.loads(json.dumps(doc))
    other["profile"]["name"] = "다른캐릭터"
    store.save(other, source="rankers")
    summary = build_stats(store, aspect="summary", character_class="블레이드")
    check("stats over latest snapshot per name", summary.get("sample", {}).get("n") == 2, summary.get("sample"))
    skills = build_stats(store, aspect="skills", character_class="블레이드")
    check("skill usage computed", skills.get("skills") and skills["skills"][0]["pct"] == 100.0, skills.get("skills", [])[:1])
    none = build_stats(store, aspect="summary", character_class="바드")
    check("empty sample reported, not invented", none.get("kind") == "no_data")
    store.close()

    print("== throttle / cache")
    quota = MinuteQuota(2, max_wait=0.05)
    await quota.acquire()
    await quota.acquire()
    try:
        await quota.acquire()
        check("local window refuses past max_wait", False)
    except QuotaExhausted:
        check("local window refuses past max_wait", True)
    reserve = MinuteQuota(90, max_wait=0.05, reserve=10)
    reserve.observe(100, 5, time.time() + 30)
    try:
        await reserve.acquire()
        check("collector reserve respects server counter", False)
    except QuotaExhausted:
        check("collector reserve respects server counter", True)

    fetches = 0

    async def fetch() -> int:
        nonlocal fetches
        fetches += 1
        return fetches

    c = TtlCache(min_refresh_sec=0.2)
    await c.get("k", 60, fetch)
    hit = await c.get("k", 60, fetch)
    soon = await c.get("k", 60, fetch, refresh=True)
    await asyncio.sleep(0.25)
    later = await c.get("k", 60, fetch, refresh=True)
    check("cache hit within ttl", hit.hit and hit.value == 1)
    check("refresh within min interval suppressed", soon.refresh_suppressed and soon.value == 1)
    check("refresh after min interval refetches", not later.hit and later.value == 2)

    print()
    print("all passed" if _failures == 0 else f"{_failures} failed")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

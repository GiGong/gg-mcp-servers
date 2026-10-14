"""Auction house (경매장): gems, accessories, bracelets, ability stones, gear.

Behaviour found against the live API:
  - BuyPrice is null on bid-only listings.
  - Items is null, not [], when nothing matches.
  - For gems, `Level` is the equip level (1640), not the gem level; the gem
    level is only in the name ("7레벨 겁화의 보석").
  - Option values are codes. A polish effect shown as 0.40% is sent as 40.
  - An option's MinValue or MaxValue alone is silently ignored; the value
    filter applies only when both are sent.
  - 광휘 gems are character-bound and never appear here.
"""

from __future__ import annotations

import re
from typing import Any

from .cache import TTL_AUCTION_SEARCH, TTL_GEM_BOARD, Fetched, TtlCache, merged_meta
from .catalog import GEM_KIND_EFFECT, Catalog, Category, norm
from .client import LostarkClient
from .fields import clean, compact, kst_from_naive, kst_iso

AUCTION_SORTS = ("BIDSTART_PRICE", "BUY_PRICE", "EXPIREDATE", "ITEM_GRADE", "ITEM_LEVEL", "ITEM_QUALITY")
MAX_PAGES = 5
MAX_GEM_BOARD_CALLS = 20
OPTION_LIST_LIMIT = 80
# Upper bound sent when only a minimum is given for an option without a value table.
_OPEN_MAX = 999_999

_RE_GEM = re.compile(r"(\d+)레벨\s+(.+?)의 보석")


def _option(o: dict[str, Any]) -> dict[str, Any]:
    value = o.get("Value")
    if o.get("IsValuePercentage") and value is not None:
        value = f"{value:g}%"
    return compact(
        {
            "type": o.get("Type"),
            "name": clean(o.get("OptionName")),
            "tripod": clean(o.get("OptionNameTripod")),
            "value": value,
            "penalty": True if o.get("IsPenalty") else None,
            "class": o.get("ClassName") or None,
        }
    )


def auction_row(row: dict[str, Any]) -> dict[str, Any]:
    info = row.get("AuctionInfo") or {}
    name = clean(row.get("Name")) or ""
    gem = _RE_GEM.search(name)
    return compact(
        {
            "name": name,
            "grade": row.get("Grade"),
            "tier": row.get("Tier"),
            "gem_level": int(gem.group(1)) if gem else None,
            "gem_kind": gem.group(2) if gem else None,
            "item_level": None if gem else row.get("Level"),
            "quality": row.get("GradeQuality"),
            "buy_price": info.get("BuyPrice"),
            "bid_start_price": info.get("BidStartPrice"),
            "current_bid": info.get("BidPrice") or None,
            "bid_count": info.get("BidCount") or None,
            "ends_kst": kst_from_naive(info.get("EndDate")),
            "trade_allow_count": info.get("TradeAllowCount"),
            "upgrade_level": info.get("UpgradeLevel"),
            "options": [_option(o) for o in row.get("Options") or []],
        }
    )


class AuctionService:
    def __init__(self, client: LostarkClient, cache: TtlCache, catalog: Catalog) -> None:
        self._client = client
        self._cache = cache
        self._catalog = catalog

    async def _page(self, body: dict[str, Any], ttl: float, *, refresh: bool) -> Fetched:
        key = "auction:search:" + repr(sorted((k, repr(v)) for k, v in body.items()))
        return await self._cache.get(
            key, ttl, lambda: self._client.post("/auctions/items", body), refresh=refresh
        )

    # -- option browser ---------------------------------------------------

    async def options(
        self,
        *,
        kind: str = "categories",
        query: str | None = None,
        category: Any = None,
        character_class: str | None = None,
    ) -> dict[str, Any]:
        categories = await self._catalog.auction_categories()
        if kind == "categories":
            return {
                "categories": [c.as_dict() for c in categories],
                "quality_steps": await self._catalog.quality_steps(),
            }

        if kind == "etc_options":
            cat = await self._catalog.auction_category(category) if category is not None else None
            names = {c.code: c.label for c in categories}
            wanted = norm(query) if query else None
            rows = []
            for o in await self._catalog.etc_options():
                if wanted and wanted not in norm(o.name) and wanted not in norm(o.group):
                    continue
                if not Catalog.applies(o, cat, categories):
                    continue
                rows.append(
                    compact(
                        {
                            "option": o.label,
                            "applies_to": [names.get(c, str(c)) for c in o.categories] or None,
                            "tiers": list(o.tiers),
                            "values": [display for display, _ in o.values] or None,
                        }
                    )
                )
            return {
                "count": len(rows),
                "options": rows[:OPTION_LIST_LIMIT],
                "truncated": len(rows) > OPTION_LIST_LIMIT or None,
                "usage": (
                    "Pass options=[{option: 'group > name', min: 0.95, max: ...}] to auction_search. "
                    "Values are the displayed numbers; the server converts them to codes."
                ),
            }

        if kind == "skills":
            if not character_class and not query:
                raise ValueError("kind='skills' needs character_class or query; there are hundreds")
            wanted = norm(query) if query else None
            rows = [
                {
                    "class": s.character_class,
                    "skill": s.name,
                    "gem_effects": [text for text, _ in s.gem_effects],
                }
                for s in await self._catalog.skills()
                if (not character_class or norm(s.character_class) == norm(character_class))
                and (not wanted or wanted in norm(s.name))
            ]
            return {"count": len(rows), "skills": rows[:OPTION_LIST_LIMIT]}

        raise ValueError("kind must be 'categories', 'etc_options' or 'skills'")

    # -- search -----------------------------------------------------------

    async def search(
        self,
        *,
        category: Any,
        query: str | None = None,
        grade: str | None = None,
        tier: int | None = None,
        quality_min: int | None = None,
        item_level_min: int | None = None,
        item_level_max: int | None = None,
        upgrade_level: int | None = None,
        trade_allow_count: int | None = None,
        character_class: str | None = None,
        options: list[dict[str, Any]] | None = None,
        skill: str | None = None,
        skill_effect: str | None = None,
        sort: str = "BUY_PRICE",
        order: str = "ASC",
        pages: int = 1,
        buyout_only: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if sort not in AUCTION_SORTS:
            raise ValueError(f"sort must be one of {AUCTION_SORTS}")
        cat = await self._catalog.auction_category(category)
        notices: list[str] = []

        if quality_min is not None:
            steps = await self._catalog.quality_steps()
            allowed = [q for q in steps if q <= quality_min]
            if allowed and allowed[-1] != quality_min:
                notices.append(
                    f"quality_min {quality_min} is not a search step; searched {allowed[-1]} "
                    f"and filtered the results to >= {quality_min}."
                )
            api_quality = allowed[-1] if allowed else None
        else:
            api_quality = None

        etc = []
        applied = []
        for spec in options or []:
            name = spec.get("option") or spec.get("name")
            if not name:
                raise ValueError("each option needs 'option' (its name)")
            opt = await self._catalog.resolve_etc_option(str(name), cat)
            low = self._catalog.encode_value(opt, spec["min"], "min") if spec.get("min") is not None else None
            high = self._catalog.encode_value(opt, spec["max"], "max") if spec.get("max") is not None else None
            shown = dict((code, display) for display, code in opt.values)
            applied.append(
                compact(
                    {
                        "option": opt.label,
                        "min": shown.get(low, low) if low is not None else None,
                        "max": shown.get(high, high) if high is not None else None,
                    }
                )
            )
            # The API ignores a bound unless both are present, so close the
            # open side with the widest value it accepts.
            codes = [code for _, code in opt.values]
            if low is not None and high is None:
                high = max(codes) if codes else _OPEN_MAX
            if high is not None and low is None:
                low = min(codes) if codes else 0
            etc.append(
                compact({"FirstOption": opt.group_code, "SecondOption": opt.code, "MinValue": low, "MaxValue": high})
            )

        skill_options = []
        if skill:
            resolved = await self._catalog.resolve_skill(skill, character_class)
            effect = skill_effect or next(
                (eff for kind, eff in GEM_KIND_EFFECT.items() if query and kind in query), None
            )
            entry: dict[str, Any] = {"FirstOption": resolved.code}
            if effect:
                entry["SecondOption"] = self._catalog.gem_effect_code(resolved, effect)
            skill_options.append(entry)

        body = compact(
            {
                "CategoryCode": cat.code,
                "ItemName": query.strip() if query else None,
                "ItemGrade": grade,
                "ItemTier": tier,
                "ItemGradeQuality": api_quality,
                "ItemLevelMin": item_level_min,
                "ItemLevelMax": item_level_max,
                "ItemUpgradeLevel": upgrade_level,
                "ItemTradeAllowCount": trade_allow_count,
                "CharacterClass": character_class,
                "EtcOptions": etc,
                "SkillOptions": skill_options,
                "Sort": sort,
                "SortCondition": order.upper(),
            }
        )

        rows: list[dict[str, Any]] = []
        fetches: list[Fetched] = []
        total = 0
        for page_no in range(1, max(1, min(pages, MAX_PAGES)) + 1):
            fetched = await self._page({**body, "PageNo": page_no}, TTL_AUCTION_SEARCH, refresh=refresh)
            fetches.append(fetched)
            data = fetched.value or {}
            total = int(data.get("TotalCount") or 0)
            items = data.get("Items") or []
            rows.extend(items)
            if not items or page_no * int(data.get("PageSize") or 10) >= total:
                break

        parsed = [auction_row(r) for r in rows]
        if quality_min is not None:
            parsed = [r for r in parsed if r.get("quality") is None or r["quality"] >= quality_min]
        dropped = 0
        if buyout_only:
            kept = [r for r in parsed if r.get("buy_price") is not None]
            dropped = len(parsed) - len(kept)
            parsed = kept
        if dropped:
            notices.append(f"{dropped} bid-only listings hidden (buyout_only=true).")

        out = compact(
            {
                "as_of_kst": kst_iso(),
                "cache": merged_meta(fetches),
                "category": cat.label,
                "filters_applied": applied,
                "total_count": total,
                "returned": len(parsed),
            }
        )
        # Always present, even when empty, so "no results" is unambiguous.
        out["items"] = parsed
        if notices:
            out["notices"] = notices
        return out

    # -- gem price board --------------------------------------------------

    async def gem_board(
        self,
        *,
        levels: list[int],
        kinds: list[str],
        tier: int = 4,
        character_class: str | None = None,
        skill: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if len(levels) * len(kinds) > MAX_GEM_BOARD_CALLS:
            raise ValueError(
                f"levels x kinds must be <= {MAX_GEM_BOARD_CALLS} lookups; narrow the request"
            )
        cat: Category = await self._catalog.auction_category("보석")
        resolved = await self._catalog.resolve_skill(skill, character_class) if skill else None

        rows = []
        fetches: list[Fetched] = []
        for kind in kinds:
            for level in sorted(levels, reverse=True):
                body: dict[str, Any] = {
                    "CategoryCode": cat.code,
                    "ItemName": f"{level}레벨 {kind}",
                    "ItemTier": tier,
                    "Sort": "BUY_PRICE",
                    "SortCondition": "ASC",
                    "PageNo": 1,
                }
                if resolved is not None:
                    effect = GEM_KIND_EFFECT.get(kind)
                    entry: dict[str, Any] = {"FirstOption": resolved.code}
                    if effect:
                        entry["SecondOption"] = self._catalog.gem_effect_code(resolved, effect)
                    body["SkillOptions"] = [entry]
                fetched = await self._page(body, TTL_GEM_BOARD, refresh=refresh)
                fetches.append(fetched)
                data = fetched.value or {}
                listings = [
                    auction_row(r)
                    for r in data.get("Items") or []
                ]
                buyouts = [
                    r["buy_price"]
                    for r in listings
                    if r.get("gem_level") == level and r.get("gem_kind") == kind and r.get("buy_price")
                ]
                rows.append(
                    compact(
                        {
                            "gem": f"{level}레벨 {kind}",
                            "min_buy_price": min(buyouts) if buyouts else None,
                            "next_buy_prices": sorted(buyouts)[1:4] or None,
                            "total_listings": int(data.get("TotalCount") or 0),
                        }
                    )
                )
        return compact(
            {
                "as_of_kst": kst_iso(),
                "cache": merged_meta(fetches),
                "tier": tier,
                "skill": f"{resolved.character_class} > {resolved.name}" if resolved else None,
                "board": rows,
                "note": (
                    "Lowest buyout prices from the cheapest page; bid-only listings excluded. "
                    "total_listings includes bid-only ones. 광휘 gems are character-bound and "
                    "never listed."
                ),
            }
        )

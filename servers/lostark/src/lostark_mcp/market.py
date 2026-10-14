"""Market (거래소): materials, life-skill goods, engraving books and the like.

Prices are quoted per bundle. Most life-skill materials and honing stones trade
in bundles of 100 (목재: 276 gold per 100), fusion materials in bundles of 1.
Every row therefore carries `bundle_count` and per-unit prices next to the raw
per-bundle figures, and the price history's AvgPrice is per bundle as well.

Searches return 10 items per page and require a category code.
"""

from __future__ import annotations

import datetime as dt
import statistics
import tomllib
from pathlib import Path
from typing import Any

from .cache import TTL_MARKET_HISTORY, TTL_MARKET_SEARCH, Fetched, TtlCache, merged_meta
from .catalog import Catalog, Category
from .client import LostarkClient, LostarkError
from .fields import clean, compact, kst_iso, now_kst

MARKET_SORTS = ("GRADE", "YDAY_AVG_PRICE", "RECENT_PRICE", "CURRENT_MIN_PRICE")
MAX_PAGES = 5
MAX_LOOKUPS = 20

# Top-level categories searched, in order, when an item is looked up by name
# alone. Each costs one call, so the rest (avatars, mounts, pets...) need an
# explicit category. Names, not codes, so a renumbering does not break this.
_LOOKUP_ORDER = ("강화 재료", "생활", "전투 용품", "요리", "각인서", "기타")

_PRICE_NOTE = (
    "current_min_price / recent_price / yday_avg_price are per bundle of bundle_count; "
    "unit_price divides them by bundle_count."
)


def _unit(value: Any, bundle: int) -> float | None:
    if value is None:
        return None
    return round(float(value) / bundle, 4)


def market_row(row: dict[str, Any]) -> dict[str, Any]:
    bundle = int(row.get("BundleCount") or 1)
    return compact(
        {
            "id": row.get("Id"),
            "name": clean(row.get("Name")),
            "grade": row.get("Grade"),
            "bundle_count": bundle,
            "current_min_price": row.get("CurrentMinPrice"),
            "recent_price": row.get("RecentPrice"),
            "yday_avg_price": row.get("YDayAvgPrice"),
            "unit_price": compact(
                {
                    "current_min": _unit(row.get("CurrentMinPrice"), bundle),
                    "recent": _unit(row.get("RecentPrice"), bundle),
                    "yday_avg": _unit(row.get("YDayAvgPrice"), bundle),
                }
            ),
            "trade_remain_count": row.get("TradeRemainCount"),
        }
    )


class MarketService:
    def __init__(self, client: LostarkClient, cache: TtlCache, catalog: Catalog, data_dir: Path) -> None:
        self._client = client
        self._cache = cache
        self._catalog = catalog
        self._presets_path = data_dir / "market_presets.toml"
        # item name -> category code, learned from successful look-ups
        self._known_category: dict[str, int] = {}

    # -- presets --------------------------------------------------------

    def presets(self) -> dict[str, dict[str, Any]]:
        try:
            with self._presets_path.open("rb") as fh:
                return tomllib.load(fh).get("presets", {})
        except FileNotFoundError:
            return {}

    # -- raw paging -----------------------------------------------------

    async def _page(self, body: dict[str, Any], *, refresh: bool) -> Fetched:
        key = "market:search:" + repr(sorted(body.items()))
        return await self._cache.get(
            key, TTL_MARKET_SEARCH, lambda: self._client.post("/markets/items", body), refresh=refresh
        )

    async def _collect(
        self, body: dict[str, Any], pages: int, *, refresh: bool
    ) -> tuple[list[dict[str, Any]], int, list[Fetched]]:
        rows: list[dict[str, Any]] = []
        fetches: list[Fetched] = []
        total = 0
        for page_no in range(1, pages + 1):
            fetched = await self._page({**body, "PageNo": page_no}, refresh=refresh)
            fetches.append(fetched)
            data = fetched.value or {}
            total = int(data.get("TotalCount") or 0)
            items = data.get("Items") or []  # null when nothing matches
            rows.extend(items)
            if not items or page_no * int(data.get("PageSize") or 10) >= total:
                break
        return rows, total, fetches

    # -- tools ----------------------------------------------------------

    async def search(
        self,
        *,
        query: str | None = None,
        category: Any = None,
        grade: str | None = None,
        tier: int | None = None,
        character_class: str | None = None,
        sort: str = "CURRENT_MIN_PRICE",
        order: str = "ASC",
        pages: int = 1,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if category is None:
            if query:
                raise ValueError(
                    "The market API needs a category for every search. Pass one, or use "
                    "market_prices(items=[...]) to look an item up by exact name."
                )
            return {
                "categories": [c.as_dict() for c in await self._catalog.market_categories()],
                "note": "Pass one of these as category (name or code).",
            }
        if sort not in MARKET_SORTS:
            raise ValueError(f"sort must be one of {MARKET_SORTS}")
        cat = await self._catalog.market_category(category)
        body = compact(
            {
                "CategoryCode": cat.code,
                "ItemName": query.strip() if query else None,
                "ItemGrade": grade,
                "ItemTier": tier,
                "CharacterClass": character_class,
                "Sort": sort,
                "SortCondition": order.upper(),
            }
        )
        rows, total, fetches = await self._collect(body, max(1, min(pages, MAX_PAGES)), refresh=refresh)
        return {
            "as_of_kst": kst_iso(),
            "cache": merged_meta(fetches),
            "category": cat.label,
            "total_count": total,
            "returned": len(rows),
            "items": [market_row(r) for r in rows],
            "note": _PRICE_NOTE,
        }

    async def _lookup_order(self, category: Any) -> list[Category]:
        if category is not None:
            return [await self._catalog.market_category(category)]
        top = [c for c in await self._catalog.market_categories() if c.parent_code is None]
        return [c for name in _LOOKUP_ORDER for c in top if c.name == name]

    async def find(
        self, name: str, *, category: Any = None, refresh: bool = False
    ) -> tuple[dict[str, Any], Fetched]:
        """Find one item by exact name. Tries likely categories until it matches."""
        wanted = name.strip()
        if not wanted:
            raise ValueError("item name must not be empty")
        order = await self._lookup_order(category)
        known = self._known_category.get(wanted)
        if known is not None and category is None:
            order.sort(key=lambda c: c.code != known)

        candidates: list[str] = []
        for cat in order:
            fetched = await self._page({"CategoryCode": cat.code, "ItemName": wanted, "PageNo": 1}, refresh=refresh)
            items = (fetched.value or {}).get("Items") or []
            exact = [i for i in items if clean(i.get("Name")) == wanted]
            if exact:
                self._known_category[wanted] = cat.code
                return market_row(exact[0]), fetched
            if items:
                # A partial match means the item family lives here; stop searching.
                candidates = [clean(i.get("Name")) or "" for i in items]
                break
        searched = ", ".join(c.name for c in order)
        raise LostarkError(
            f"No market item named exactly {wanted!r} (searched: {searched}). "
            "For other categories pass category explicitly.",
            kind="not_found",
            candidates=candidates or None,
        )

    async def prices(
        self,
        *,
        items: list[str] | None = None,
        preset: str | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        presets = self.presets()
        if not items and not preset:
            return {"presets": {k: v.get("description", "") for k, v in presets.items()}}

        rows: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        fetches: list[Fetched] = []

        if preset:
            spec = presets.get(preset)
            if spec is None:
                raise LostarkError(
                    f"No preset named {preset!r}.", kind="bad_request", candidates=sorted(presets)
                )
            body = compact(
                {
                    "CategoryCode": (await self._catalog.market_category(spec["category"])).code,
                    "ItemName": spec.get("query"),
                    "ItemTier": spec.get("tier"),
                    "ItemGrade": spec.get("grade"),
                    "Sort": "CURRENT_MIN_PRICE",
                    "SortCondition": "ASC",
                }
            )
            found, _, page_fetches = await self._collect(body, MAX_PAGES, refresh=refresh)
            fetches.extend(page_fetches)
            rows.extend(market_row(r) for r in found)

        for name in (items or [])[:MAX_LOOKUPS]:
            try:
                row, fetched = await self.find(name, refresh=refresh)
            except LostarkError as exc:
                if exc.kind != "not_found":
                    raise
                missing.append(compact({"name": name, "candidates": exc.candidates}))
                continue
            fetches.append(fetched)
            rows.append(row)

        out = compact(
            {
                "as_of_kst": kst_iso(),
                "cache": merged_meta(fetches),
                "preset": preset,
            }
        )
        out["items"] = rows
        out.update(
            compact(
                {
                    "not_found": missing,
                    "skipped": (items or [])[MAX_LOOKUPS:],
                    "note": _PRICE_NOTE,
                }
            )
        )
        return out

    async def history(self, item: str | int, *, category: Any = None, refresh: bool = False) -> dict[str, Any]:
        if isinstance(item, int) or str(item).strip().isdigit():
            item_id = int(item)
        else:
            row, _ = await self.find(str(item), category=category, refresh=refresh)
            item_id = int(row["id"])

        fetched = await self._cache.get(
            f"market:history:{item_id}",
            TTL_MARKET_HISTORY,
            lambda: self._client.get(f"/markets/items/{item_id}"),
            refresh=refresh,
        )
        entries = fetched.value or []
        if not entries:
            raise LostarkError(f"No market item with id {item_id}.", kind="not_found")

        today = now_kst().date().isoformat()
        out = []
        for entry in entries:
            bundle = int(entry.get("BundleCount") or 1)
            stats = sorted(entry.get("Stats") or [], key=lambda s: s.get("Date") or "")
            days = [
                compact(
                    {
                        "date": s.get("Date"),
                        "avg_price": s.get("AvgPrice"),
                        "unit_avg_price": _unit(s.get("AvgPrice"), bundle),
                        "trade_count": s.get("TradeCount"),
                        "partial": True if s.get("Date") == today else None,
                    }
                )
                for s in stats
            ]
            out.append(
                compact(
                    {
                        "name": clean(entry.get("Name")),
                        "bundle_count": bundle,
                        "trade_remain_count": entry.get("TradeRemainCount"),
                        "summary": _summarise([d for d in days if not d.get("partial")], bundle),
                        "days": days,
                    }
                )
            )
        return {
            "as_of_kst": kst_iso(),
            "cache": fetched.meta(),
            "item_id": item_id,
            "entries": out,
            "note": (
                "Oldest first. avg_price is per bundle; unit_avg_price per unit. Today's row is "
                "marked partial and excluded from the summary."
            ),
        }


def _summarise(days: list[dict[str, Any]], bundle: int) -> dict[str, Any] | None:
    prices = [d["avg_price"] for d in days if d.get("avg_price") is not None]
    if not prices:
        return None
    last7 = prices[-7:]
    first, last = prices[0], prices[-1]
    return compact(
        {
            "days": len(prices),
            "last_full_day": days[-1].get("date"),
            "last_avg_price": last,
            "avg_7d": round(statistics.fmean(last7), 2),
            "min": min(prices),
            "max": max(prices),
            "change_pct": round((last - first) / first * 100, 2) if first else None,
            "unit_last_avg_price": _unit(last, bundle),
        }
    )

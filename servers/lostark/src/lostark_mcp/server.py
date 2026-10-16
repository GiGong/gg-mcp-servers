"""FastMCP server assembly, served over Streamable HTTP.

Every tool is read-only. The Lost Ark Open API offers nothing that changes game
state; the only writes anywhere are the local snapshot database, and only the
`collect` command writes to it.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import BaseModel, Field

from localauth import PassphraseOAuthProvider, StaticBearerVerifier

from .armory import ArmoryService
from .auction import AuctionService
from .cache import TtlCache
from .catalog import Catalog
from .client import LostarkClient, LostarkError
from .config import Settings, load_settings
from .craft import CraftService
from .logging_setup import setup_logging
from .market import MarketService
from .snapshots import SnapshotStore
from .stats import build_stats

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """\
Read-only Lost Ark (KR) data from the official Lost Ark Open API.

Coverage
  - Characters: profile, gear, accessories, engravings, gems, cards, skills,
    ark passive, ark grid, roster (other characters on the same account)
  - Market (거래소): materials and life-skill goods, current prices, 14-day history
  - Auction house (경매장): gems, accessories, bracelets, ability stones
  - Crafting profit, and build statistics over a locally collected sample

How to work
  - Start with character_overview (about 2 KB). Then read only what you need
    with character_detail: pick sections, narrow with match=[...], and choose
    detail='brief' | 'standard' | 'full'. Detail calls reuse the cached
    character, so they cost no API quota.
  - The API allows 100 requests per minute. Prefer cached data; pass
    refresh=true only when the user needs the latest state. Every response has
    `cache.age_sec` and most have `quota.remaining`.

How to read the responses
  - Market prices are per bundle (`bundle_count`, often 100 for materials).
    Use `unit_price` for per-item comparisons.
  - 광휘 gems are character-bound and never on the auction house; tradeable
    tier-4 gems are 겁화 (damage) and 작열 (cooldown).
  - Item level and combat power come from the character profile.
  - stats_builds uses names the operator collected, not an official ranking.
    Always report its sample size.
  - Recipe quantities, crafting fees and the market fee are not in the API.
    calc_craft_profit reports which of them were assumed or unverified; say so.
  - There is no guild, ranking or character-search API.
  - On failure a tool returns `error: true` with `kind` (not_found,
    rate_limited, maintenance, bad_request, ...). Report it; do not invent data.
"""

Section = Literal[
    "profile",
    "stats",
    "equipment",
    "accessories",
    "bracelet",
    "ability_stone",
    "misc_equipment",
    "engravings",
    "gems",
    "cards",
    "skills",
    "arkpassive",
    "arkgrid",
    "avatars",
    "collectibles",
    "colosseums",
]
Aspect = Literal[
    "summary",
    "build",
    "arkpassive",
    "engravings",
    "skills",
    "gems",
    "stats",
    "accessories",
    "bracelet",
    "cards",
    "arkgrid",
    "equipment",
]

Refresh = Annotated[
    bool, Field(description="Bypass the cache and fetch fresh data. Costs API quota.")
]
CharacterName = Annotated[str, Field(description="Exact character name, e.g. 홍길동")]


class AuctionOptionFilter(BaseModel):
    option: str = Field(
        description="Option name as shown by auction_options(kind='etc_options'), "
        "e.g. '추가 피해' or '연마 효과 > 추가 피해' when ambiguous"
    )
    min: float | None = Field(default=None, description="Minimum displayed value, e.g. 1.6 for 1.60%")
    max: float | None = Field(default=None, description="Maximum displayed value")


class CraftInput(BaseModel):
    name: str = Field(description="Exact market item name")
    qty: float = Field(description="Quantity consumed per craft", gt=0)


def build_server(settings: Settings) -> tuple[FastMCP, LostarkClient]:
    client = LostarkClient(settings)
    cache = TtlCache()
    catalog = Catalog(client, cache)
    armory = ArmoryService(client, cache)
    market = MarketService(client, cache, catalog, settings.data_dir)
    auction = AuctionService(client, cache, catalog)
    craft = CraftService(market, settings.data_dir)

    async def run(factory, *args, **kwargs) -> dict[str, Any]:
        """Turn failures into structured results and attach the quota state."""
        try:
            result = await factory(*args, **kwargs)
        except LostarkError as exc:
            logger.warning("tool failed: %s (%s)", exc, exc.kind)
            result = exc.to_dict()
        except ValueError as exc:
            result = {"error": True, "kind": "invalid_argument", "message": str(exc)}
        quota = client.quota()
        if isinstance(result, dict) and quota:
            result["quota"] = quota
        return result

    auth = None
    if settings.auth_mode in {"oauth", "both"}:
        auth = PassphraseOAuthProvider(
            base_url=settings.public_url,
            passphrase=settings.oauth_passphrase,
            signing_secret=settings.oauth_signing_secret,
            store_path=settings.oauth_store,
            access_token_ttl=settings.oauth_access_ttl,
            refresh_token_ttl=settings.oauth_refresh_ttl,
            server_label=settings.auth_page_label,
            page_subtitle=settings.auth_page_subtitle,
            static_bearer_token=settings.bearer_token if settings.auth_mode == "both" else None,
        )
        logger.info(
            "auth: built-in OAuth 2.1 (passphrase-gated) at %s%s",
            settings.public_url,
            " + static bearer token" if settings.auth_mode == "both" else "",
        )
    elif settings.auth_mode == "bearer":
        auth = StaticBearerVerifier(settings.bearer_token)
        logger.info("auth: static bearer token (%d chars); no OAuth routes", len(settings.bearer_token))
    else:
        logger.warning("auth: DISABLED (AUTH_MODE=none). Do not expose this server publicly.")

    mcp: FastMCP = FastMCP(name="lostark", instructions=SERVER_INSTRUCTIONS, auth=auth)

    # ---------------- characters ----------------

    @mcp.tool
    async def character_overview(name: CharacterName, refresh: Refresh = False) -> dict[str, Any]:
        """One-screen summary of a character (about 2 KB).

        Class, item level, combat power, build, combat stats, ark passive,
        one line per gear piece and accessory, bracelet, ability stone,
        engravings, gem summary, card sets, ark grid cores and invested skills.
        Use character_detail to drill into any part.
        """
        return await run(armory.overview, name, refresh=refresh)

    @mcp.tool
    async def character_detail(
        name: CharacterName,
        sections: Annotated[
            list[Section],
            Field(
                description=(
                    "Parts to return. equipment = weapon and armour; accessories = necklace, "
                    "earrings, rings; misc_equipment = compass, charm and the like."
                ),
                min_length=1,
            ),
        ],
        detail: Annotated[
            Literal["brief", "standard", "full"],
            Field(
                description="brief: one line per item. standard: parsed fields. "
                "full: standard plus tooltip text (large; combine with match)."
            ),
        ] = "standard",
        match: Annotated[
            list[str] | None,
            Field(
                description="Keep only items whose slot or name contains any of these, "
                "e.g. ['무기'], ['보이드 스트라이크'], ['깨달음']"
            ),
        ] = None,
        invested_only: Annotated[
            bool, Field(description="skills: only skills with points, tripods or a rune")
        ] = True,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Selected parts of a character in chosen depth, cut from the cached character.

        Costs no API quota when the character was fetched in the last few minutes.
        """
        return await run(
            armory.detail,
            name,
            list(sections),
            detail=detail,
            match=match,
            invested_only=invested_only,
            refresh=refresh,
        )

    @mcp.tool
    async def character_roster(name: CharacterName, refresh: Refresh = False) -> dict[str, Any]:
        """Every character on the same account, grouped by server, highest item level first."""
        return await run(armory.roster, name, refresh=refresh)

    # ---------------- market ----------------

    @mcp.tool
    async def market_search(
        category: Annotated[
            str | None,
            Field(
                description="Category name or code, e.g. '강화 재료', '재련 재료', '생활', "
                "'벌목 전리품', 50000. Omit to list categories."
            ),
        ] = None,
        query: Annotated[str | None, Field(description="Substring of the item name")] = None,
        grade: Annotated[str | None, Field(description="e.g. 희귀, 영웅, 전설, 유물")] = None,
        tier: Annotated[int | None, Field(description="Item tier, 2-4", ge=1, le=4)] = None,
        character_class: Annotated[str | None, Field(description="Class filter, e.g. 블레이드")] = None,
        sort: Annotated[
            Literal["GRADE", "YDAY_AVG_PRICE", "RECENT_PRICE", "CURRENT_MIN_PRICE"],
            Field(description="Sort key"),
        ] = "CURRENT_MIN_PRICE",
        order: Annotated[Literal["ASC", "DESC"], Field(description="Sort order")] = "ASC",
        pages: Annotated[int, Field(description="Pages of 10 to fetch (1 call each)", ge=1, le=5)] = 1,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Search the market (거래소). Prices are per bundle; see unit_price."""
        return await run(
            market.search,
            query=query,
            category=category,
            grade=grade,
            tier=tier,
            character_class=character_class,
            sort=sort,
            order=order,
            pages=pages,
            refresh=refresh,
        )

    @mcp.tool
    async def market_prices(
        items: Annotated[
            list[str] | None,
            Field(description="Exact item names, e.g. ['아비도스 목재', '운명의 파괴석']. Max 20."),
        ] = None,
        preset: Annotated[
            str | None,
            Field(description="Saved search from data/market_presets.toml. Omit both to list presets."),
        ] = None,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Current prices for named items or a preset group, with per-unit prices."""
        return await run(market.prices, items=items, preset=preset, refresh=refresh)

    @mcp.tool
    async def market_history(
        item: Annotated[str, Field(description="Exact item name or numeric item id")],
        category: Annotated[
            str | None, Field(description="Optional category hint to speed up the name look-up")
        ] = None,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """14 days of daily average price and trade count for a market item, oldest first."""
        return await run(market.history, item, category=category, refresh=refresh)

    # ---------------- auction house ----------------

    @mcp.tool
    async def auction_search(
        category: Annotated[
            str,
            Field(
                description="Category name or code: 보석, 목걸이, 귀걸이, 반지, 팔찌, "
                "어빌리티 스톤, 무기, 투구, ... (see auction_options)"
            ),
        ],
        query: Annotated[
            str | None, Field(description="Substring of the item name, e.g. '7레벨 겁화'")
        ] = None,
        grade: Annotated[str | None, Field(description="e.g. 유물, 고대")] = None,
        tier: Annotated[int | None, Field(description="Item tier", ge=1, le=4)] = 4,
        quality_min: Annotated[int | None, Field(description="Minimum quality, 0-100", ge=0, le=100)] = None,
        item_level_min: Annotated[int | None, Field(description="Minimum equip level")] = None,
        item_level_max: Annotated[int | None, Field(description="Maximum equip level")] = None,
        upgrade_level: Annotated[int | None, Field(description="Polish/upgrade level filter")] = None,
        trade_allow_count: Annotated[int | None, Field(description="Remaining trade count filter")] = None,
        character_class: Annotated[str | None, Field(description="Class, needed with skill")] = None,
        options: Annotated[
            list[AuctionOptionFilter] | None,
            Field(description="Option filters by name and displayed value; codes are resolved here"),
        ] = None,
        skill: Annotated[
            str | None, Field(description="Gem search: the skill the gem must target")
        ] = None,
        skill_effect: Annotated[
            str | None,
            Field(description="Gem effect: '피해 증가' (겁화) or '재사용 대기시간 감소' (작열). "
            "Inferred from query when it names the gem kind."),
        ] = None,
        sort: Annotated[
            Literal["BIDSTART_PRICE", "BUY_PRICE", "EXPIREDATE", "ITEM_GRADE", "ITEM_LEVEL", "ITEM_QUALITY"],
            Field(description="Sort key"),
        ] = "BUY_PRICE",
        order: Annotated[Literal["ASC", "DESC"], Field(description="Sort order")] = "ASC",
        pages: Annotated[int, Field(description="Pages of 10 to fetch (1 call each)", ge=1, le=5)] = 1,
        buyout_only: Annotated[bool, Field(description="Hide bid-only listings")] = True,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Search current auction listings. Option names and values are converted to API codes."""
        return await run(
            auction.search,
            category=category,
            query=query,
            grade=grade,
            tier=tier,
            quality_min=quality_min,
            item_level_min=item_level_min,
            item_level_max=item_level_max,
            upgrade_level=upgrade_level,
            trade_allow_count=trade_allow_count,
            character_class=character_class,
            options=[o.model_dump() for o in options or []],
            skill=skill,
            skill_effect=skill_effect,
            sort=sort,
            order=order,
            pages=pages,
            buyout_only=buyout_only,
            refresh=refresh,
        )

    @mcp.tool
    async def auction_gem_board(
        levels: Annotated[
            list[int], Field(description="Gem levels to price", min_length=1, max_length=10)
        ] = [6, 7, 8, 9, 10],  # noqa: B006 - pydantic copies defaults
        kinds: Annotated[
            list[str], Field(description="Gem kinds: 겁화, 작열 (tier 4); 멸화, 홍염 (tier 3)")
        ] = ["겁화", "작열"],  # noqa: B006
        tier: Annotated[int, Field(description="Gem tier", ge=3, le=4)] = 4,
        character_class: Annotated[str | None, Field(description="Class, needed with skill")] = None,
        skill: Annotated[
            str | None, Field(description="Price gems for this skill only (prices differ by skill)")
        ] = None,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Lowest buyout price per gem level and kind. One API call per level x kind (max 20)."""
        return await run(
            auction.gem_board,
            levels=levels,
            kinds=kinds,
            tier=tier,
            character_class=character_class,
            skill=skill,
            refresh=refresh,
        )

    @mcp.tool
    async def auction_options(
        kind: Annotated[
            Literal["categories", "etc_options", "skills"],
            Field(description="categories, etc_options (accessory/bracelet/stone filters), or skills"),
        ] = "categories",
        query: Annotated[str | None, Field(description="Substring filter on option or skill names")] = None,
        category: Annotated[
            str | None, Field(description="etc_options: only options usable in this category")
        ] = None,
        character_class: Annotated[str | None, Field(description="skills: class filter")] = None,
    ) -> dict[str, Any]:
        """Browse what auction_search accepts: categories, option names and values, skills."""
        return await run(
            auction.options, kind=kind, query=query, category=category, character_class=character_class
        )

    # ---------------- calculations ----------------

    @mcp.tool
    async def calc_craft_profit(
        recipe: Annotated[
            str | None,
            Field(description="Recipe name from data/recipes.toml. Omit everything to list recipes."),
        ] = None,
        output_item: Annotated[str | None, Field(description="Crafted item (exact market name)")] = None,
        output_qty: Annotated[float | None, Field(description="Items produced per craft", gt=0)] = None,
        inputs: Annotated[
            list[CraftInput] | None, Field(description="Materials consumed per craft")
        ] = None,
        craft_gold: Annotated[float | None, Field(description="Gold fee per craft", ge=0)] = None,
        craft_seconds: Annotated[float | None, Field(description="Craft time per run", gt=0)] = None,
        crafts: Annotated[int, Field(description="Number of crafts to total", ge=1)] = 1,
        sell_fee_rate: Annotated[
            float | None, Field(description="Market sale fee as a fraction; default from data/fees.toml", ge=0, lt=1)
        ] = None,
        refresh: Refresh = False,
    ) -> dict[str, Any]:
        """Profit of crafting at current market prices.

        Compares crafting against buying the materials and against selling your
        own materials instead. Inline values override the recipe file.
        """
        return await run(
            craft.profit,
            recipe=recipe,
            output_item=output_item,
            output_qty=output_qty,
            inputs=[i.model_dump() for i in inputs] if inputs is not None else None,
            craft_gold=craft_gold,
            craft_seconds=craft_seconds,
            crafts=crafts,
            sell_fee_rate=sell_fee_rate,
            refresh=refresh,
        )

    # ---------------- statistics ----------------

    @mcp.tool
    async def stats_builds(
        character_class: Annotated[str | None, Field(description="Class, e.g. 블레이드")] = None,
        aspect: Annotated[
            Aspect,
            Field(
                description="summary, build, arkpassive, engravings, skills, gems, stats, "
                "accessories, bracelet, cards, arkgrid, equipment"
            ),
        ] = "summary",
        min_item_level: Annotated[float | None, Field(description="Only characters at or above")] = None,
        build: Annotated[
            str | None, Field(description="Only this build (ark passive title), e.g. 잔재된 기운")
        ] = None,
        max_age_days: Annotated[float | None, Field(description="Ignore older snapshots", gt=0)] = 30,
        top: Annotated[int, Field(description="Entries per list", ge=1, le=30)] = 10,
    ) -> dict[str, Any]:
        """Build statistics over locally collected characters (the operator's ranker list).

        Not an official ranking. Collected by `lostark-mcp collect`; reports sample size.
        """

        async def compute() -> dict[str, Any]:
            store = SnapshotStore(settings.snapshot_db)
            try:
                return build_stats(
                    store,
                    aspect=aspect,
                    character_class=character_class,
                    min_item_level=min_item_level,
                    build=build,
                    max_age_days=max_age_days,
                    top=top,
                )
            finally:
                store.close()

        return await run(compute)

    return mcp, client


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_file)

    logger.info(
        "starting lostark-mcp | key=%s throttle=%d/min auth=%s bind=%s:%s%s",
        settings.masked_api_key(),
        settings.rate_limit_per_min,
        settings.auth_mode,
        settings.host,
        settings.port,
        settings.mcp_path,
    )

    mcp, _client = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001, ANN202
        from starlette.responses import JSONResponse

        # Reachable without authentication through the tunnel: liveness only.
        return JSONResponse({"status": "ok"})

    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
    )

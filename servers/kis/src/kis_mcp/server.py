"""FastMCP server assembly, served over Streamable HTTP.

Every exposed tool is read-only. Order, amend, cancel and transfer endpoints are
not implemented anywhere in this server and are not registered as tools.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from localauth import PassphraseOAuthProvider

from .client import KisClient, KisError
from .config import Settings, load_settings
from .logging_setup import setup_logging
from .market import MarketCalendar
from .tools import KisTools

logger = logging.getLogger(__name__)

SERVER_INSTRUCTIONS = """\
Read-only market data from the Korea Investment & Securities (KIS) Open API.

Coverage
  - Korean equities: quotes, daily OHLCV, order book, investor flow
  - Domestic and overseas indices, US equities, USD/KRW

How to read the responses
  - `as_of_kst` on every response is Korea Standard Time.
  - Use `market_status.phase` to tell a live figure from a stale one, and read
    `market_status.note`. Prices read on the KRX market code (J) keep changing
    after the regular close until 20:00 KST. In `after_hours` and `closed`, the
    current price, change, volume and today's daily close include those
    after-hours trades, so they are not the regular-session close. Session hours shift on some days (e.g. the college
    entrance exam day); the note says so.
  - `get_investor_flow` returns `provisional` (intraday broker-compiled
    estimate) and `confirmed` (exchange-confirmed) separately. Never mix them.
    The current day's confirmed figures are not published until after the close,
    so during the session you must read `provisional` and say so.
  - Overseas quotes are delayed unless the account subscribes to real-time data.
    Check the `delayed` field.
  - When KIS returns an error it is passed through as `error` / `kis_msg_cd` /
    `kis_msg1`. Report that error rather than inventing a value.

This server cannot place trades.
"""


def _wrap(result_factory):
    """Turn KisError into a structured result instead of swallowing it."""

    async def runner(*args, **kwargs) -> dict[str, Any]:
        try:
            return await result_factory(*args, **kwargs)
        except KisError as exc:
            logger.warning("tool failed with KIS error: %s", exc)
            return exc.to_dict()

    return runner


def build_server(settings: Settings) -> tuple[FastMCP, KisClient]:
    client = KisClient(settings)
    calendar = MarketCalendar(client)
    tools = KisTools(client, calendar)

    auth = None
    if settings.auth_mode == "oauth":
        auth = PassphraseOAuthProvider(
            base_url=settings.public_url,
            passphrase=settings.oauth_passphrase,
            signing_secret=settings.oauth_signing_secret,
            store_path=settings.oauth_store,
            access_token_ttl=settings.oauth_access_ttl,
            refresh_token_ttl=settings.oauth_refresh_ttl,
            server_label=settings.auth_page_label,
            page_subtitle=settings.auth_page_subtitle,
        )
        logger.info("auth: built-in OAuth 2.1 (passphrase-gated) at %s", settings.public_url)
    else:
        logger.warning(
            "auth: DISABLED (AUTH_MODE=none). Do not expose this server publicly."
        )

    mcp: FastMCP = FastMCP(
        name="kis-stock-quotes",
        instructions=SERVER_INSTRUCTIONS,
        auth=auth,
    )

    # ---------------- primary ----------------

    @mcp.tool
    async def get_quote(
        symbol: Annotated[
            str, Field(description="Six-digit Korean ticker, e.g. 000660 for SK hynix")
        ],
    ) -> dict[str, Any]:
        """Current quote for a Korean listed security.

        Returns price, percent change, open/high/low, previous close, volume,
        market capitalisation and valuation ratios, along with a KST timestamp
        and the current market session state. After the regular close the price
        follows after-hours trades; `market_status` says when that applies.
        """
        return await _wrap(tools.get_quote)(symbol)

    @mcp.tool
    async def get_investor_flow(
        symbol: Annotated[str, Field(description="Six-digit Korean ticker, e.g. 000660")],
        confirmed_days: Annotated[
            int, Field(description="How many days of confirmed data to return", ge=1, le=30)
        ] = 5,
    ) -> dict[str, Any]:
        """Foreign and institutional net buying for a Korean security.

        `provisional` is the intraday broker-compiled estimate, refreshed four
        times a session. `confirmed` is the exchange-confirmed daily figure.
        The two are reported separately and must not be conflated.
        """
        return await _wrap(tools.get_investor_flow)(symbol, confirmed_days)

    @mcp.tool
    async def get_daily_ohlcv(
        symbol: Annotated[str, Field(description="Six-digit Korean ticker")],
        days: Annotated[
            int, Field(description="Number of recent trading days", ge=1, le=100)
        ] = 30,
        adjusted: Annotated[bool, Field(description="Use split-adjusted prices")] = True,
    ) -> dict[str, Any]:
        """Daily OHLCV for a Korean security, ordered oldest to newest."""
        return await _wrap(tools.get_daily_ohlcv)(symbol, days, adjusted)

    # ---------------- secondary ----------------

    @mcp.tool
    async def get_orderbook(
        symbol: Annotated[str, Field(description="Six-digit Korean ticker")],
        depth: Annotated[int, Field(description="Number of price levels", ge=1, le=10)] = 10,
    ) -> dict[str, Any]:
        """Order book depth, including total bid and ask size and their ratio."""
        return await _wrap(tools.get_orderbook)(symbol, depth)

    @mcp.tool
    async def get_index(
        code: Annotated[
            str,
            Field(
                description=(
                    "Index code or alias. Domestic: KOSPI, KOSDAQ, KOSPI200, or the "
                    "raw codes 0001/1001/2001. Overseas: SOX (Philadelphia "
                    "Semiconductor), NASDAQ, SPX, DJI."
                )
            ),
        ] = "KOSPI",
    ) -> dict[str, Any]:
        """Index level and percent change.

        Domestic indices are live; overseas indices reflect the previous regular
        session's close.
        """
        return await _wrap(tools.get_index)(code)

    @mcp.tool
    async def get_overseas_quote(
        ticker: Annotated[str, Field(description="Overseas ticker, e.g. MU, NVDA, TSM")],
        exchange: Annotated[
            str,
            Field(description="Exchange code: NAS, NYS, AMS. Corrected automatically if wrong."),
        ] = "NAS",
    ) -> dict[str, Any]:
        """Current quote for an overseas listed security.

        Also returns the KRW-converted price and the FX rate KIS applied, so an
        ADR can be compared against its home listing without a separate FX call.
        Check the `delayed` field for quote latency.
        """
        return await _wrap(tools.get_overseas_quote)(ticker, exchange)

    # ---------------- optional ----------------

    @mcp.tool
    async def get_fx(
        pair: Annotated[str, Field(description="Currency pair; USDKRW by default")] = "USDKRW",
    ) -> dict[str, Any]:
        """USD/KRW exchange rate.

        Falls back to the conversion rate KIS applies to overseas quotes if the
        dedicated FX lookup is unavailable; the response records which was used.
        """
        return await _wrap(tools.get_fx)(pair)

    @mcp.tool
    async def get_market_status() -> dict[str, Any]:
        """Whether the Korean market is currently open, closed or on holiday."""
        return await _wrap(tools.get_market_status)()

    return mcp, client


async def _warmup(client: KisClient) -> None:
    try:
        await client.warmup()
    except KisError as exc:
        # Start anyway. A token failure will surface on the first tool call.
        logger.error("KIS token warmup failed: %s", exc)


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_file)

    logger.info(
        "starting kis-mcp | env=%s appkey=%s throttle=%.1f/s auth=%s bind=%s:%s%s",
        settings.kis_env,
        settings.masked_app_key(),
        settings.kis_rate_limit_per_sec,
        settings.auth_mode,
        settings.host,
        settings.port,
        settings.mcp_path,
    )

    mcp, client = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001, ANN202
        from starlette.responses import JSONResponse

        # Reachable without authentication through the tunnel, so it reports
        # liveness only. Which KIS environment is in use is not public business.
        return JSONResponse({"status": "ok"})

    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
    )

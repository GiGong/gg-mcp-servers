"""FastMCP 서버 조립. Streamable HTTP 로 서비스한다.

노출하는 도구는 전부 조회 전용이다. 주문/정정/취소/이체 계열 KIS API 는
이 서버에 구현되어 있지 않으며 도구로도 등록하지 않는다.
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
한국투자증권(KIS) Open API 를 감싼 조회 전용 MCP 서버입니다.

제공 범위
  - 국내주식 현재가/일봉/호가/투자자 수급
  - 국내외 지수, 해외주식(미국) 시세, 원달러 환율

중요한 해석 규칙
  - 모든 응답의 as_of_kst 는 한국시간(KST)입니다.
  - market_status.phase 로 정규장/장마감/휴장 여부를 판단하세요.
    장 마감 후나 휴장일에는 값이 직전 거래일 기준입니다.
  - get_investor_flow 는 provisional(장중 추정 가집계, 잠정)과
    confirmed(거래소 확정치)를 나눠서 돌려줍니다. 둘을 섞지 마세요.
  - 해외 시세는 별도 신청이 없으면 지연 시세입니다. delayed 필드를 확인하세요.
  - KIS 가 오류를 반환하면 error/kis_msg_cd/kis_msg1 필드로 그대로 전달됩니다.
    값을 지어내지 말고 그 오류를 사용자에게 알리세요.

이 서버는 매매 주문을 실행할 수 없습니다.
"""


def _wrap(result_factory):
    """KIS 오류를 삼키지 않고 구조화된 결과로 바꿔주는 데코레이터용 헬퍼."""

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

    # ---------------- 1순위 ----------------

    @mcp.tool
    async def get_quote(
        symbol: Annotated[str, Field(description="국내 종목코드 6자리. 예: SK하이닉스 000660")],
    ) -> dict[str, Any]:
        """국내주식 현재가 시세.

        현재가, 등락률, 시가/고가/저가, 전일종가, 거래량, 시가총액, PER/PBR 을
        한국시간 타임스탬프 및 장 상태와 함께 반환합니다.
        """
        return await _wrap(tools.get_quote)(symbol)

    @mcp.tool
    async def get_investor_flow(
        symbol: Annotated[str, Field(description="국내 종목코드 6자리. 예: 000660")],
        confirmed_days: Annotated[
            int, Field(description="확정치를 며칠치 돌려줄지 (1~30)", ge=1, le=30)
        ] = 5,
    ) -> dict[str, Any]:
        """종목별 외국인·기관 매매 동향.

        provisional 은 장중 추정 가집계(잠정, 하루 4회 갱신),
        confirmed 는 거래소 확정 일별 순매수입니다. 두 값은 명확히 구분됩니다.
        """
        return await _wrap(tools.get_investor_flow)(symbol, confirmed_days)

    @mcp.tool
    async def get_daily_ohlcv(
        symbol: Annotated[str, Field(description="국내 종목코드 6자리")],
        days: Annotated[
            int, Field(description="최근 몇 거래일치 (1~100, 기본 30)", ge=1, le=100)
        ] = 30,
        adjusted: Annotated[bool, Field(description="수정주가 사용 여부")] = True,
    ) -> dict[str, Any]:
        """국내주식 일별 OHLCV. 오래된 날짜 → 최신 날짜 순으로 정렬해 반환합니다."""
        return await _wrap(tools.get_daily_ohlcv)(symbol, days, adjusted)

    # ---------------- 2순위 ----------------

    @mcp.tool
    async def get_orderbook(
        symbol: Annotated[str, Field(description="국내 종목코드 6자리")],
        depth: Annotated[int, Field(description="호가 단계 수 (1~10)", ge=1, le=10)] = 10,
    ) -> dict[str, Any]:
        """국내주식 호가 잔량. 총 매수/매도 잔량과 매수·매도 비율을 포함합니다."""
        return await _wrap(tools.get_orderbook)(symbol, depth)

    @mcp.tool
    async def get_index(
        code: Annotated[
            str,
            Field(
                description=(
                    "지수 코드 또는 별칭. 국내: KOSPI, KOSDAQ, KOSPI200, 0001, 1001, 2001. "
                    "해외: SOX(필라델피아 반도체), NASDAQ, SPX, DJI 또는 .으로 시작하는 원본 코드."
                )
            ),
        ] = "KOSPI",
    ) -> dict[str, Any]:
        """지수 현재가와 등락률. 국내 지수는 실시간, 해외 지수는 일별 종가 기준입니다."""
        return await _wrap(tools.get_index)(code)

    @mcp.tool
    async def get_overseas_quote(
        ticker: Annotated[str, Field(description="해외 티커. 예: SKHY, MU, NVDA, TSM")],
        exchange: Annotated[
            str,
            Field(description="거래소 코드. NAS(나스닥), NYS(뉴욕), AMS(아멕스) 등"),
        ] = "NAS",
    ) -> dict[str, Any]:
        """해외주식 현재가.

        원환산 가격(t_xprc)과 KIS 적용 환율(t_rate)을 함께 돌려주므로
        ADR-본주 괴리율을 바로 계산할 수 있습니다. 지연 시세 여부는 delayed 필드 참고.
        """
        return await _wrap(tools.get_overseas_quote)(ticker, exchange)

    # ---------------- 3순위 / 부가 ----------------

    @mcp.tool
    async def get_fx(
        pair: Annotated[str, Field(description="통화쌍. 기본 USDKRW")] = "USDKRW",
    ) -> dict[str, Any]:
        """원달러 환율. 환율 시세 조회가 막히면 KIS 원환산 적용환율로 대체합니다."""
        return await _wrap(tools.get_fx)(pair)

    @mcp.tool
    async def get_market_status() -> dict[str, Any]:
        """지금 한국 증시가 열려 있는지, 휴장일인지 확인합니다."""
        return await _wrap(tools.get_market_status)()

    return mcp, client


async def _warmup(client: KisClient) -> None:
    try:
        await client.warmup()
    except KisError as exc:
        # 토큰 발급 실패해도 서버는 뜨게 둔다. 도구 호출 시 오류가 그대로 노출된다.
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

        return JSONResponse({"status": "ok", "kis_env": settings.kis_env})

    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
    )

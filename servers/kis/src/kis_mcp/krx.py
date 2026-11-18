"""Optional cross-check of regular-session closes against the KRX Open API.

KRX publishes each day's official close (TDD_CLSPRC) only after the day is
over, so it can confirm a close but never supply one on the day itself. The
check is off unless KRX_AUTH_KEY is set. Every outcome is reported as a result
code rather than raised, so a missing key, a missing approval or a KRX outage
never breaks the tool that asked.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from typing import Any

import httpx

from .fields import num, text

logger = logging.getLogger(__name__)

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"

# Daily trading data, one dataset per market, each approved separately on
# openapi.krx.co.kr. A symbol is looked up in each in turn until it is found,
# so KOSPI symbols cost one call per day and no market mapping is needed.
DATASETS = ("sto/stk_bydd_trd", "sto/ksq_bydd_trd")

# An empty dataset means KRX has not published that day yet. Ask again after
# this long rather than caching the absence.
_UNPUBLISHED_RETRY_SEC = 600

# Published days kept in memory. Each is a code -> close map of ~1,000 entries.
_MAX_CACHED_DAYS = 40

SOURCE = "krx_openapi"


class KrxClient:
    def __init__(self, auth_key: str, timeout: float = 10.0) -> None:
        self._key = auth_key
        self._http = (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                headers={"User-Agent": "kis-mcp/0.1 (read-only)"},
            )
            if auth_key
            else None
        )
        self._published: dict[tuple[str, str], dict[str, float | None]] = {}
        self._retry_after: dict[tuple[str, str], float] = {}
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    async def check(self, symbol: str, trade_date: dt.date, price: float) -> dict[str, Any]:
        """Compare `price` with KRX's official close for that day."""
        result: dict[str, Any] = {"source": SOURCE, "compares": "close price only"}
        if not self.configured:
            result["result"] = "NOT_CONFIGURED"
            return result

        day = trade_date.strftime("%Y%m%d")
        outcomes: dict[str, str] = {}
        for dataset in DATASETS:
            closes, outcome, detail = await self._dataset(dataset, day)
            if closes is None:
                outcomes[dataset] = outcome
                if detail:
                    result.setdefault("detail", {})[dataset] = detail
                continue
            official = closes.get(symbol)
            if official is None:
                outcomes[dataset] = "NOT_FOUND"
                continue
            result["dataset"] = dataset
            result["krx_price"] = official
            result["result"] = "MATCH" if official == price else "MISMATCH"
            return result

        # Not found anywhere. Report the reason that most limits what we know.
        for code in ("NOT_PUBLISHED", "ERROR", "NOT_AUTHORIZED"):
            if code in outcomes.values():
                result["result"] = code
                result["datasets"] = outcomes
                if code == "ERROR":
                    result["retryable"] = True
                return result
        result["result"] = "UNSUPPORTED"
        result["datasets"] = outcomes
        return result

    async def _dataset(
        self, dataset: str, day: str
    ) -> tuple[dict[str, float | None] | None, str, str | None]:
        """Closes by code for one dataset and day, or (None, outcome, detail)."""
        key = (dataset, day)
        if key in self._published:
            return self._published[key], "", None
        if self._retry_after.get(key, 0.0) > time.monotonic():
            return None, "NOT_PUBLISHED", None

        async with self._lock:
            if key in self._published:
                return self._published[key], "", None
            assert self._http is not None
            try:
                response = await self._http.get(
                    f"{KRX_BASE}/{dataset}",
                    params={"basDd": day},
                    headers={"AUTH_KEY": self._key},
                )
            except httpx.HTTPError as exc:
                logger.warning("KRX %s %s failed: %s", dataset, day, type(exc).__name__)
                return None, "ERROR", type(exc).__name__

            try:
                body = response.json()
            except ValueError:
                body = {}
            if response.status_code == 401:
                message = text(body.get("respMsg")) or "Unauthorized"
                # "Unauthorized API Call" means the key is valid but this
                # dataset has not been approved for it. Anything else (e.g.
                # "Unauthorized Key") is a configuration error.
                outcome = "NOT_AUTHORIZED" if message == "Unauthorized API Call" else "ERROR"
                return None, outcome, message
            if response.status_code != 200 or not isinstance(body.get("OutBlock_1"), list):
                logger.warning("KRX %s %s -> HTTP %s", dataset, day, response.status_code)
                return None, "ERROR", f"HTTP {response.status_code}"

            rows = body["OutBlock_1"]
            if not rows:
                self._retry_after[key] = time.monotonic() + _UNPUBLISHED_RETRY_SEC
                return None, "NOT_PUBLISHED", None

            closes = {
                code: num(row.get("TDD_CLSPRC"))
                for row in rows
                if (code := text(row.get("ISU_CD")))
            }
            if len(self._published) >= _MAX_CACHED_DAYS:
                self._published.pop(next(iter(self._published)))
            self._published[key] = closes
            self._retry_after.pop(key, None)
            return closes, "", None

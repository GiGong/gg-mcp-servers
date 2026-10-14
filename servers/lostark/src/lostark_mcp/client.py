"""Lost Ark Open API client.

Responsibilities:
  - Allow only the inquiry paths this server uses
  - Respect the per-minute quota (MinuteQuota), synced from response headers
  - Turn HTTP failures into structured errors instead of swallowing them

The API answers a missing character with 200 and a `null` body (siblings: `[]`)
rather than 404, and a bad search with 400 and an empty body. Callers check for
those; this module reports what the HTTP layer can see.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Any
from urllib.parse import quote

import httpx

from .config import API_BASE_URL, Settings
from .throttle import MinuteQuota, QuotaExhausted

logger = logging.getLogger(__name__)

# Read-only by construction. The two POST paths are searches; nothing the API
# offers can change game state, but this list still names exactly what is used.
_GET_PREFIXES = ("/characters/", "/armories/characters/", "/markets/items/")
_GET_EXACT = {"/markets/options", "/auctions/options"}
_POST_EXACT = {"/markets/items", "/auctions/items"}

# During maintenance the API answers 503. Stop calling for this long.
MAINTENANCE_BACKOFF_SEC = 60.0

_MAX_ATTEMPTS = 2
_RETRY_DELAY_SEC = 1.0


class LostarkError(RuntimeError):
    """A failed call, with a machine-readable kind.

    kind: not_found | rate_limited | maintenance | unauthorized | bad_request
          | upstream | network
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        http_status: int | None = None,
        path: str | None = None,
        retry_after_sec: float | None = None,
        candidates: list[Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.path = path
        self.retry_after_sec = retry_after_sec
        self.candidates = candidates

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": True, "kind": self.kind, "message": str(self)}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        if self.path is not None:
            out["api_path"] = self.path
        if self.retry_after_sec is not None:
            out["retry_after_sec"] = math.ceil(self.retry_after_sec)
        if self.candidates:
            out["candidates"] = self.candidates
        return out


def quote_name(name: str) -> str:
    """Percent-encode a character name for use as a path segment."""
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("character name must not be empty")
    return quote(cleaned, safe="")


def _allowed(method: str, path: str) -> bool:
    if method == "GET":
        return path in _GET_EXACT or path.startswith(_GET_PREFIXES)
    if method == "POST":
        return path in _POST_EXACT
    return False


class LostarkClient:
    def __init__(self, settings: Settings, *, quota: MinuteQuota | None = None) -> None:
        self._settings = settings
        self._quota = quota or MinuteQuota(
            settings.rate_limit_per_min, max_wait=settings.max_wait_sec
        )
        self._http = httpx.AsyncClient(
            base_url=API_BASE_URL,
            timeout=httpx.Timeout(settings.timeout),
            headers={
                "accept": "application/json",
                "authorization": f"bearer {settings.api_key}",
                "user-agent": "lostark-mcp/0.1 (read-only)",
            },
        )
        self._maintenance_until = 0.0

    def quota(self) -> dict[str, Any] | None:
        return self._quota.snapshot()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: dict[str, str] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: dict[str, Any]) -> Any:
        return await self._request("POST", path, body=body)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        if not _allowed(method, path):
            raise ValueError(f"unexpected Lost Ark call: {method} {path!r}")

        if time.monotonic() < self._maintenance_until:
            raise LostarkError(
                "The Lost Ark API reported maintenance moments ago; not retrying yet.",
                kind="maintenance",
                http_status=503,
                path=path,
                retry_after_sec=self._maintenance_until - time.monotonic(),
            )

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                waited = await self._quota.acquire()
            except QuotaExhausted as exc:
                raise LostarkError(
                    "The per-minute request quota is used up. Retry later or rely on cached data.",
                    kind="rate_limited",
                    path=path,
                    retry_after_sec=exc.wait,
                ) from None
            if waited > 0.05:
                logger.debug("throttled %.2fs before %s %s", waited, method, path)

            started = time.monotonic()
            try:
                response = await self._http.request(method, path, params=params, json=body)
            except httpx.TransportError as exc:
                if attempt < _MAX_ATTEMPTS:
                    logger.warning("network error on %s %s: %s; retrying", method, path, exc)
                    await asyncio.sleep(_RETRY_DELAY_SEC)
                    continue
                raise LostarkError(
                    f"Network error talking to the Lost Ark API: {exc.__class__.__name__}",
                    kind="network",
                    path=path,
                ) from None
            elapsed = (time.monotonic() - started) * 1000

            headers = response.headers
            self._quota.observe(
                headers.get("x-ratelimit-limit"),
                headers.get("x-ratelimit-remaining"),
                headers.get("x-ratelimit-reset"),
            )
            logger.info(
                "LOA %s %s -> %s (%.0fms, %dB, remaining=%s)%s",
                method,
                path,
                response.status_code,
                elapsed,
                len(response.content),
                headers.get("x-ratelimit-remaining", "-"),
                f" body={json.dumps(body, ensure_ascii=False)}" if body else "",
            )

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError:
                    raise LostarkError(
                        "The Lost Ark API returned a non-JSON body.",
                        kind="upstream",
                        http_status=status,
                        path=path,
                    ) from None

            if status == 429:
                wait = self._quota.server_wait()
                if attempt < _MAX_ATTEMPTS and wait <= self._settings.max_wait_sec:
                    logger.warning("429 on %s; waiting %.1fs for the quota to reset", path, wait)
                    await asyncio.sleep(wait + 0.5)
                    continue
                raise LostarkError(
                    "The Lost Ark API rate limit was exceeded.",
                    kind="rate_limited",
                    http_status=status,
                    path=path,
                    retry_after_sec=wait or 60.0,
                )

            if status == 503:
                self._maintenance_until = time.monotonic() + MAINTENANCE_BACKOFF_SEC
                raise LostarkError(
                    "The Lost Ark API is unavailable, usually because of game maintenance.",
                    kind="maintenance",
                    http_status=status,
                    path=path,
                    retry_after_sec=MAINTENANCE_BACKOFF_SEC,
                )

            if status in (401, 403):
                raise LostarkError(
                    "The Lost Ark API rejected the key. Check LOSTARK_API_KEY.",
                    kind="unauthorized",
                    http_status=status,
                    path=path,
                )

            if status in (400, 404, 415):
                raise LostarkError(
                    "The Lost Ark API rejected the request. Check the search parameters "
                    "(category codes and option codes must come from the options catalog).",
                    kind="bad_request",
                    http_status=status,
                    path=path,
                )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SEC)
                continue
            raise LostarkError(
                f"The Lost Ark API failed with HTTP {status}.",
                kind="upstream",
                http_status=status,
                path=path,
            )

        raise LostarkError("Lost Ark call failed", kind="upstream", path=path)

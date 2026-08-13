"""KIS Open API 클라이언트.

책임:
  - 접근토큰 발급/캐싱/갱신 (파일 캐시, 만료 임박 시에만 재발급)
  - 유량 제한 준수 (RateLimiter)
  - KIS 에러코드를 삼키지 않고 구조화해서 위로 전달
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .throttle import RateLimiter

logger = logging.getLogger(__name__)

# 만료까지 이만큼 남으면 미리 갱신한다.
TOKEN_REFRESH_MARGIN_SEC = 900

# KIS 는 접근토큰 발급을 1분에 1회로 제한한다 (초과 시 EGW00133).
TOKEN_MIN_ISSUE_INTERVAL_SEC = 60.0

# 토큰 만료/무효를 뜻하는 KIS 메시지 코드.
#   EGW00121 유효하지 않은 token / EGW00123 기간이 만료된 token
_TOKEN_EXPIRED_CODES = {"EGW00121", "EGW00123"}

# 접근토큰 발급 빈도 초과 (1분 1회).
_TOKEN_RATE_LIMIT_CODE = "EGW00133"

# 호출 유량 초과. 토큰 문제가 아니므로 재발급하면 안 되고 잠깐 쉬었다 재시도해야 한다.
_THROTTLE_CODE = "EGW00201"
_THROTTLE_BACKOFF_SEC = 1.0
_MAX_ATTEMPTS = 3


class KisError(RuntimeError):
    """KIS 호출 실패. 에러코드/메시지를 그대로 보존한다."""

    def __init__(
        self,
        message: str,
        *,
        rt_cd: str | None = None,
        msg_cd: str | None = None,
        msg1: str | None = None,
        http_status: int | None = None,
        tr_id: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.rt_cd = rt_cd
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.http_status = http_status
        self.tr_id = tr_id
        self.path = path

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": True,
            "message": str(self),
            "kis_rt_cd": self.rt_cd,
            "kis_msg_cd": self.msg_cd,
            "kis_msg1": self.msg1,
            "http_status": self.http_status,
            "tr_id": self.tr_id,
            "api_path": self.path,
        }


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float
    issued_at: float
    fingerprint: str

    def remaining(self) -> float:
        return self.expires_at - time.time()


def _fingerprint(settings: Settings) -> str:
    """앱키/환경이 바뀌면 캐시를 자동 무효화하기 위한 지문. 키 자체는 저장하지 않는다."""
    raw = f"{settings.kis_env}:{settings.kis_app_key}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


class TokenManager:
    """접근토큰을 메모리 + 파일에 캐시하고 만료 임박 시에만 재발급한다."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http
        self._lock = asyncio.Lock()
        self._token: _CachedToken | None = None
        self._fingerprint = _fingerprint(settings)
        self._last_issue_attempt = 0.0

    # -- file cache ------------------------------------------------------

    def _read_cache(self) -> _CachedToken | None:
        path = self._settings.kis_token_cache
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        if raw.get("fingerprint") != self._fingerprint:
            logger.info("token cache fingerprint mismatch; ignoring cached token")
            return None
        try:
            return _CachedToken(
                access_token=raw["access_token"],
                expires_at=float(raw["expires_at"]),
                issued_at=float(raw.get("issued_at", 0.0)),
                fingerprint=raw["fingerprint"],
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _write_cache(self, token: _CachedToken) -> None:
        path = self._settings.kis_token_cache
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = {
            "access_token": token.access_token,
            "expires_at": token.expires_at,
            "issued_at": token.issued_at,
            "fingerprint": token.fingerprint,
        }
        # 0600 으로 만든 뒤 원자적으로 교체한다.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    # -- issuance --------------------------------------------------------

    async def _issue(self) -> _CachedToken:
        """POST /oauth2/tokenP 로 새 토큰을 발급받는다. 1분 1회 제한을 지킨다."""
        since = time.monotonic() - self._last_issue_attempt
        if self._last_issue_attempt and since < TOKEN_MIN_ISSUE_INTERVAL_SEC:
            wait = TOKEN_MIN_ISSUE_INTERVAL_SEC - since
            logger.warning(
                "token issuance throttled by KIS 1/min rule; sleeping %.1fs", wait
            )
            await asyncio.sleep(wait)

        self._last_issue_attempt = time.monotonic()
        url = f"{self._settings.kis_base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self._settings.kis_app_key,
            "appsecret": self._settings.kis_app_secret,
        }
        logger.info(
            "issuing KIS access token (env=%s, appkey=%s)",
            self._settings.kis_env,
            self._settings.masked_app_key(),
        )
        response = await self._http.post(
            url, json=body, headers={"content-type": "application/json"}
        )
        try:
            data = response.json()
        except ValueError:
            raise KisError(
                f"token endpoint returned non-JSON (HTTP {response.status_code})",
                http_status=response.status_code,
                path="/oauth2/tokenP",
            ) from None

        if "access_token" not in data:
            msg_cd = data.get("error_code") or data.get("msg_cd")
            msg1 = data.get("error_description") or data.get("msg1")
            if msg_cd == _TOKEN_RATE_LIMIT_CODE:
                raise KisError(
                    "KIS refused token issuance: 1 token per minute per app key",
                    msg_cd=msg_cd,
                    msg1=msg1,
                    http_status=response.status_code,
                    path="/oauth2/tokenP",
                )
            raise KisError(
                f"token issuance failed: {msg_cd} {msg1}",
                msg_cd=msg_cd,
                msg1=msg1,
                http_status=response.status_code,
                path="/oauth2/tokenP",
            )

        now = time.time()
        # expires_in 은 보통 86400(24h). 없으면 보수적으로 12시간으로 잡는다.
        expires_in = float(data.get("expires_in") or 43200)
        token = _CachedToken(
            access_token=data["access_token"],
            expires_at=now + expires_in,
            issued_at=now,
            fingerprint=self._fingerprint,
        )
        self._write_cache(token)
        logger.info("KIS access token issued; valid for %.0f hours", expires_in / 3600)
        return token

    # -- public ----------------------------------------------------------

    async def get_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if force_refresh:
                self._token = None

            if self._token is None:
                self._token = self._read_cache()

            if (
                self._token is not None
                and not force_refresh
                and self._token.remaining() > TOKEN_REFRESH_MARGIN_SEC
            ):
                return self._token.access_token

            self._token = await self._issue()
            return self._token.access_token

    def invalidate(self) -> None:
        self._token = None


class KisClient:
    """읽기 전용 KIS 호출기. 주문/정정/취소 계열 경로는 아예 다루지 않는다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.kis_timeout),
            headers={"User-Agent": "kis-mcp/0.1 (read-only)"},
        )
        self._tokens = TokenManager(settings, self._http)
        self._limiter = RateLimiter(settings.kis_rate_limit_per_sec)

    @property
    def settings(self) -> Settings:
        return self._settings

    async def aclose(self) -> None:
        await self._http.aclose()

    async def warmup(self) -> None:
        """기동 시 토큰을 한 번 확보해 첫 호출 지연을 없앤다."""
        await self._tokens.get_token()

    async def _headers(self, tr_id: str, tr_cont: str, token: str) -> dict[str, str]:
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "appkey": self._settings.kis_app_key,
            "appsecret": self._settings.kis_app_secret,
            "tr_id": tr_id,
            "tr_cont": tr_cont,
            "custtype": "P",
        }

    async def get(
        self,
        path: str,
        *,
        tr_id: str,
        params: dict[str, str],
        tr_cont: str = "",
    ) -> dict[str, Any]:
        """조회 API 호출. rt_cd != '0' 이면 KisError 를 던진다."""
        if not path.startswith("/uapi/"):
            raise ValueError(f"unexpected KIS path: {path!r}")

        force_refresh = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            token = await self._tokens.get_token(force_refresh=force_refresh)
            force_refresh = False
            waited = await self._limiter.acquire()
            if waited > 0.05:
                logger.debug("throttled %.3fs before %s", waited, tr_id)

            started = time.monotonic()
            response = await self._http.get(
                f"{self._settings.kis_base_url}{path}",
                params=params,
                headers=await self._headers(tr_id, tr_cont, token),
            )
            elapsed = (time.monotonic() - started) * 1000

            try:
                data = response.json()
            except ValueError:
                raise KisError(
                    f"KIS returned non-JSON (HTTP {response.status_code})",
                    http_status=response.status_code,
                    tr_id=tr_id,
                    path=path,
                ) from None

            rt_cd = str(data.get("rt_cd", ""))
            msg_cd = data.get("msg_cd")
            msg1 = (data.get("msg1") or "").strip()

            logger.info(
                "KIS %s %s -> HTTP %s rt_cd=%s msg_cd=%s (%.0fms) params=%s",
                tr_id,
                path,
                response.status_code,
                rt_cd or "-",
                msg_cd or "-",
                elapsed,
                params,
            )

            if msg_cd in _TOKEN_EXPIRED_CODES and attempt < _MAX_ATTEMPTS:
                logger.warning("KIS says token invalid/expired (%s); reissuing", msg_cd)
                self._tokens.invalidate()
                force_refresh = True
                continue

            if msg_cd == _THROTTLE_CODE and attempt < _MAX_ATTEMPTS:
                # 유량 초과. 토큰 문제가 아니므로 재발급하지 않는다.
                backoff = _THROTTLE_BACKOFF_SEC * attempt
                logger.warning(
                    "KIS throttled the call (%s); backing off %.1fs (attempt %d/%d)",
                    msg_cd,
                    backoff,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                await asyncio.sleep(backoff)
                continue

            if rt_cd != "0":
                raise KisError(
                    msg1 or f"KIS call failed (rt_cd={rt_cd})",
                    rt_cd=rt_cd or None,
                    msg_cd=msg_cd,
                    msg1=msg1 or None,
                    http_status=response.status_code,
                    tr_id=tr_id,
                    path=path,
                )

            data["_tr_cont"] = response.headers.get("tr_cont", "")
            return data

        raise KisError(
            f"KIS call failed after {_MAX_ATTEMPTS} attempts", tr_id=tr_id, path=path
        )

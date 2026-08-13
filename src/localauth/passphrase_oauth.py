"""패스프레이즈로 게이트하는 자체 완결형 OAuth 2.1 인가서버.

왜 이게 필요한가:
  Claude.ai 의 커스텀 커넥터는 정적 `Authorization: Bearer` 헤더를 보낼 수 없다.
  고급 설정에 OAuth Client ID/Secret 칸만 있고 임의 헤더 칸이 없다.
  따라서 원격 MCP 를 인증으로 보호하려면 서버가 OAuth 2.1 + DCR 을 제공해야 한다.
  이 모듈은 외부 IdP(GitHub, WorkOS 등) 없이 그 역할을 한다.

보안 설계:
  - 인가 코드는 브라우저에서 패스프레이즈를 맞게 입력해야만 발급된다.
  - 액세스/리프레시 토큰은 평문으로 저장하지 않는다. HMAC-SHA256 해시만 저장한다.
  - 패스프레이즈 비교는 상수 시간 비교(compare_digest)를 쓴다.
  - 실패 시도는 지수 백오프로 제한한다.
  - PKCE(S256)는 MCP SDK 의 인가/토큰 핸들러가 강제한다.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fastmcp.server.auth.auth import ClientRegistrationOptions, OAuthProvider

from .store import JsonStore

logger = logging.getLogger(__name__)

AUTH_CODE_TTL = 300
PENDING_TTL = 600
LOGIN_PATH = "/auth/login"

# 무차별 대입 방어: 실패가 쌓이면 대기 시간이 늘어난다.
_LOCKOUT_STEPS = [0, 0, 1, 3, 10, 30, 60, 300]


class PassphraseOAuthProvider(OAuthProvider):
    """DCR 을 받아주되, 사람이 패스프레이즈를 입력해야 토큰을 내주는 인가서버."""

    def __init__(
        self,
        *,
        base_url: AnyHttpUrl | str,
        passphrase: str,
        signing_secret: str,
        store_path: Path,
        access_token_ttl: int = 8 * 3600,
        refresh_token_ttl: int = 30 * 86400,
        server_label: str = "Private connector",
        page_subtitle: str = "연결하려면 패스프레이즈를 입력하세요.",
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            required_scopes=required_scopes,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )
        if len(passphrase) < 16:
            raise ValueError("passphrase must be at least 16 characters")
        if len(signing_secret) < 16:
            raise ValueError("signing_secret must be at least 16 characters")

        self._passphrase = passphrase.encode()
        self._secret = signing_secret.encode()
        self._store = JsonStore(store_path)
        self._access_ttl = access_token_ttl
        self._refresh_ttl = refresh_token_ttl
        self._server_label = server_label
        self._page_subtitle = page_subtitle

        # rid -> {client_id, redirect_uri, state, scopes, code_challenge, ...}
        self._pending: dict[str, dict[str, Any]] = {}
        self._failures = 0
        self._locked_until = 0.0

    # -- helpers ---------------------------------------------------------

    def _digest(self, token: str) -> str:
        return hmac.new(self._secret, token.encode(), hashlib.sha256).hexdigest()

    def _clients(self) -> dict[str, Any]:
        return self._store.section("clients")

    def _access(self) -> dict[str, Any]:
        return self._store.section("access_tokens")

    def _refresh(self) -> dict[str, Any]:
        return self._store.section("refresh_tokens")

    def _codes(self) -> dict[str, Any]:
        return self._store.section("auth_codes")

    def _prune(self) -> None:
        now = time.time()
        for section in (self._access(), self._refresh(), self._codes()):
            expired = [
                key
                for key, value in section.items()
                if value.get("expires_at") and value["expires_at"] < now
            ]
            for key in expired:
                section.pop(key, None)
        for rid, pending in list(self._pending.items()):
            if pending["created_at"] + PENDING_TTL < now:
                self._pending.pop(rid, None)

    # -- client registration --------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._clients().get(client_id)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            logger.warning("stored client %s is unreadable; dropping", client_id)
            self._clients().pop(client_id, None)
            self._store.flush()
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        self._clients()[client_info.client_id] = client_info.model_dump(mode="json")
        self._store.flush()
        logger.info(
            "registered OAuth client %s (redirect_uris=%s)",
            client_info.client_id,
            [str(u) for u in (client_info.redirect_uris or [])],
        )

    # -- authorization ---------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """코드를 바로 발급하지 않고 패스프레이즈 입력 화면으로 보낸다."""
        if client.client_id is None or client.client_id not in self._clients():
            raise AuthorizeError(
                error="unauthorized_client",
                error_description="Client is not registered.",
            )

        self._prune()
        rid = secrets.token_urlsafe(24)
        self._pending[rid] = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "state": params.state,
            "scopes": list(params.scopes or []),
            "code_challenge": params.code_challenge,
            "created_at": time.time(),
        }
        logger.info("authorization request %s queued for client %s", rid, client.client_id)
        return f"{str(self.base_url).rstrip('/')}{LOGIN_PATH}?rid={rid}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = self._codes().get(self._digest(authorization_code))
        if record is None:
            return None
        if record["client_id"] != client.client_id:
            return None
        if record["expires_at"] < time.time():
            self._codes().pop(self._digest(authorization_code), None)
            self._store.flush()
            return None
        return AuthorizationCode(
            code=authorization_code,
            client_id=record["client_id"],
            redirect_uri=AnyHttpUrl(record["redirect_uri"]),
            redirect_uri_provided_explicitly=record["redirect_uri_provided_explicitly"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            code_challenge=record["code_challenge"],
        )

    # -- token issuance ---------------------------------------------------

    def _issue_pair(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = time.time()
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)

        self._access()[self._digest(access)] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + self._access_ttl,
        }
        self._refresh()[self._digest(refresh)] = {
            "client_id": client_id,
            "scopes": scopes,
            "expires_at": now + self._refresh_ttl,
        }
        self._prune()
        self._store.flush()

        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self._access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        key = self._digest(authorization_code.code)
        if key not in self._codes():
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        self._codes().pop(key, None)  # 1회용
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        logger.info("issued access token to client %s", client.client_id)
        return self._issue_pair(client.client_id, list(authorization_code.scopes))

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._refresh().get(self._digest(refresh_token))
        if record is None or record["client_id"] != client.client_id:
            return None
        if record["expires_at"] < time.time():
            self._refresh().pop(self._digest(refresh_token), None)
            self._store.flush()
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        granted = set(refresh_token.scopes)
        if not set(scopes).issubset(granted):
            raise TokenError("invalid_scope", "Requested scopes exceed the granted set.")
        # OAuth 2.1: public client 의 리프레시 토큰은 회전시킨다.
        self._refresh().pop(self._digest(refresh_token.token), None)
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        return self._issue_pair(client.client_id, scopes or list(granted))

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self._access().get(self._digest(token))
        if record is None:
            return None
        if record["expires_at"] < time.time():
            self._access().pop(self._digest(token), None)
            self._store.flush()
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=int(record["expires_at"]),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        key = self._digest(token.token)
        self._access().pop(key, None)
        self._refresh().pop(key, None)
        self._store.flush()

    # -- login UI ---------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        routes = super().get_routes(mcp_path)
        routes.append(
            Route(LOGIN_PATH, endpoint=self._login_endpoint, methods=["GET", "POST"])
        )
        return routes

    async def _login_endpoint(self, request: Request) -> Response:
        if request.method == "GET":
            rid = request.query_params.get("rid", "")
            if rid not in self._pending:
                return HTMLResponse(self._page(error="만료되었거나 알 수 없는 요청입니다. 커넥터에서 다시 연결을 시도하세요.", rid=None), status_code=400)
            return HTMLResponse(self._page(rid=rid))

        form = await request.form()
        rid = str(form.get("rid", ""))
        supplied = str(form.get("passphrase", ""))

        wait = self._lockout_remaining()
        if wait > 0:
            return HTMLResponse(
                self._page(rid=rid, error=f"시도가 너무 많습니다. {int(wait)}초 후 다시 시도하세요."),
                status_code=429,
            )

        pending = self._pending.get(rid)
        if pending is None:
            return HTMLResponse(
                self._page(rid=None, error="만료되었거나 알 수 없는 요청입니다."), status_code=400
            )

        if not hmac.compare_digest(supplied.encode(), self._passphrase):
            self._register_failure()
            logger.warning("failed passphrase attempt for authorization request %s", rid)
            return HTMLResponse(
                self._page(rid=rid, error="패스프레이즈가 올바르지 않습니다."), status_code=401
            )

        self._failures = 0
        self._pending.pop(rid, None)

        code = secrets.token_urlsafe(32)
        self._codes()[self._digest(code)] = {
            "client_id": pending["client_id"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "scopes": pending["scopes"],
            "code_challenge": pending["code_challenge"],
            "expires_at": time.time() + AUTH_CODE_TTL,
        }
        self._store.flush()
        logger.info("authorization granted for client %s", pending["client_id"])

        target = construct_redirect_uri(
            pending["redirect_uri"], code=code, state=pending["state"]
        )
        return RedirectResponse(target, status_code=302)

    def _lockout_remaining(self) -> float:
        return max(0.0, self._locked_until - time.time())

    def _register_failure(self) -> None:
        self._failures += 1
        index = min(self._failures, len(_LOCKOUT_STEPS) - 1)
        self._locked_until = time.time() + _LOCKOUT_STEPS[index]

    def _page(self, *, rid: str | None, error: str | None = None) -> str:
        error_block = (
            f'<p class="err">{html.escape(error)}</p>' if error else ""
        )
        form_block = (
            f"""
            <form method="post" autocomplete="off">
              <input type="hidden" name="rid" value="{html.escape(rid)}">
              <label for="pp">패스프레이즈</label>
              <input id="pp" name="passphrase" type="password" autofocus required>
              <button type="submit">연결 승인</button>
            </form>
            """
            if rid
            else ""
        )
        return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(self._server_label)} 연결 승인</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: Canvas; color: CanvasText; }}
  .card {{ width: min(92vw, 26rem); padding: 2rem; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
          border-radius: 14px; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .35rem; }}
  p.sub {{ margin: 0 0 1.5rem; opacity: .7; font-size: .875rem; line-height: 1.5; }}
  label {{ display: block; font-size: .8rem; margin-bottom: .4rem; opacity: .8; }}
  input {{ width: 100%; box-sizing: border-box; padding: .65rem .75rem; font-size: 1rem;
          border-radius: 8px; border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
          background: Canvas; color: CanvasText; }}
  button {{ margin-top: 1rem; width: 100%; padding: .7rem; font-size: .95rem; font-weight: 600;
           border: 0; border-radius: 8px; background: CanvasText; color: Canvas; cursor: pointer; }}
  .err {{ color: #d33; font-size: .85rem; margin: 0 0 1rem; }}
</style></head>
<body><div class="card">
  <h1>{html.escape(self._server_label)}</h1>
  <p class="sub">{html.escape(self._page_subtitle)}</p>
  {error_block}{form_block}
</div></body></html>"""

"""A self-contained OAuth 2.1 authorization server gated by a passphrase.

Why this exists:
    Until 2026-08 Claude.ai custom connectors could not send a static
    `Authorization: Bearer` header; the connector settings exposed only OAuth
    Client ID/Secret. Clients that can send headers now are better served by
    StaticBearerVerifier (bearer.py); this provider remains for clients that
    cannot, and for when short-lived, rotating tokens are wanted. A
    browser-login gateway does not work either, because Claude.ai's MCP client
    is a server-side HTTP client that never completes an interstitial login.
    The supported path is OAuth 2.1 + Dynamic Client Registration, and most
    examples delegate that to an external IdP. This module is the authorization
    server itself, so no third party is involved.

Design:
    FastMCP ships an InMemoryOAuthProvider, but it approves every authorization
    request -- anyone who learns the URL gets a token. This subclasses
    OAuthProvider directly so that `authorize()` redirects to a passphrase form
    instead of minting a code.

Security properties:
    - Authorization codes are issued only after a correct passphrase entry.
    - Tokens are never persisted in plaintext; only an HMAC-SHA256 digest is.
    - Passphrase comparison is constant time (`hmac.compare_digest`).
    - Failed attempts back off exponentially.
    - PKCE (S256) is enforced by the MCP SDK's authorize/token handlers.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

from .bearer import MIN_TOKEN_LENGTH

from .store import JsonStore

logger = logging.getLogger(__name__)

AUTH_CODE_TTL = 300
PENDING_TTL = 600
LOGIN_PATH = "/auth/login"

# Dynamic client registration is unauthenticated by design (the MCP spec needs
# it), so the store must not grow without bound. Eviction is least-recently-used
# and never touches a client that still holds a live token, so flooding the
# endpoint cannot dislodge a connector that is actually in use.
MAX_CLIENTS = 50

# Response headers for the login page. It carries a passphrase form, so it must
# not be framable and should not leak its URL to third parties.
#
# `form-action` needs care: it governs the whole navigation started by the form
# submission, redirects included. Submitting the passphrase makes the server
# answer 302 to the client's redirect_uri (e.g. claude.ai), so a bare
# `form-action 'self'` makes the browser block that redirect and the page hangs
# on a spinner forever. The allowed origin is therefore computed per request
# from the pending authorization, rather than hardcoded or dropped entirely.
_BASE_PAGE_HEADERS = {
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_CSP_TEMPLATE = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
    "frame-ancestors 'none'; base-uri 'none'"
)


def _page_headers(redirect_uri: str | None = None) -> dict[str, str]:
    """Headers for the login page, allowing a redirect back to this client only."""
    allowed = "'self'"
    if redirect_uri:
        parsed = urlparse(redirect_uri)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            allowed = f"'self' {parsed.scheme}://{parsed.netloc}"
    return {
        "Content-Security-Policy": _CSP_TEMPLATE.format(form_action=allowed),
        **_BASE_PAGE_HEADERS,
    }

# Brute-force defence: a wrong guess costs the caller this many seconds.
# Applied to the failing response only, never to a correct passphrase.
_BACKOFF_STEPS = [0, 0, 1, 3, 10, 30, 60, 60]


class PassphraseOAuthProvider(OAuthProvider):
    """An authorization server that accepts DCR but requires a human passphrase.

    `static_bearer_token`, when given, is accepted as well, so one server can
    serve clients that send a fixed header (Claude) and clients that only speak
    OAuth (ChatGPT, Gemini) at the same time.
    """

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
        page_subtitle: str = "Enter the passphrase to connect.",
        required_scopes: list[str] | None = None,
        static_bearer_token: str | None = None,
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

        if static_bearer_token is not None:
            if len(static_bearer_token) < MIN_TOKEN_LENGTH:
                raise ValueError(
                    f"static_bearer_token must be at least {MIN_TOKEN_LENGTH} characters"
                )
            if static_bearer_token in (passphrase, signing_secret):
                raise ValueError("static_bearer_token must differ from the passphrase and signing secret")

        self._passphrase = passphrase.encode()
        self._secret = signing_secret.encode()
        self._static_bearer = static_bearer_token.encode() if static_bearer_token else None
        self._store = JsonStore(store_path)
        self._access_ttl = access_token_ttl
        self._refresh_ttl = refresh_token_ttl
        self._server_label = server_label
        self._page_subtitle = page_subtitle

        # rid -> pending authorization request awaiting passphrase entry
        self._pending: dict[str, dict[str, Any]] = {}
        self._failures = 0

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

    def _active_client_ids(self) -> set[str]:
        """Clients holding a live access or refresh token."""
        active: set[str] = set()
        for section in (self._access(), self._refresh()):
            for record in section.values():
                cid = record.get("client_id")
                if cid:
                    active.add(cid)
        return active

    def _evict_clients(self, clients: dict[str, Any]) -> None:
        """Trim the client store, least recently used first.

        Clients with a live token are never evicted: an unauthenticated flood of
        registrations must not be able to disconnect a working connector.
        """
        if len(clients) <= MAX_CLIENTS:
            return
        protected = self._active_client_ids()
        evictable = [
            (raw.get("_last_used") or raw.get("_registered_at", 0.0), cid)
            for cid, raw in clients.items()
            if cid not in protected
        ]
        evictable.sort()
        for _, cid in evictable[: len(clients) - MAX_CLIENTS]:
            clients.pop(cid, None)
        logger.info(
            "client store trimmed to %d entries (%d protected by live tokens)",
            len(clients),
            len(protected),
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._clients().get(client_id)
        if raw is None:
            return None
        raw["_last_used"] = time.time()
        try:
            return OAuthClientInformationFull.model_validate(
                {k: v for k, v in raw.items() if not k.startswith("_")}
            )
        except Exception:
            logger.warning("stored client %s is unreadable; dropping", client_id)
            self._clients().pop(client_id, None)
            self._store.flush()
            return None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise ValueError("client_id is required for client registration")
        record = client_info.model_dump(mode="json")
        record["_registered_at"] = time.time()
        clients = self._clients()
        clients[client_info.client_id] = record

        self._evict_clients(clients)
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
        """Redirect to the passphrase form rather than issuing a code directly."""
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
        self._codes().pop(key, None)  # single use
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
        # OAuth 2.1: rotate refresh tokens issued to public clients.
        self._refresh().pop(self._digest(refresh_token.token), None)
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")
        logger.info("refreshed access token for client %s", client.client_id)
        return self._issue_pair(client.client_id, scopes or list(granted))

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._static_bearer is not None and hmac.compare_digest(token.encode(), self._static_bearer):
            return AccessToken(
                token=token,
                client_id="static-bearer",
                scopes=list(self.required_scopes or []),
                expires_at=None,
            )
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
                return HTMLResponse(
                    self._page(
                        rid=None,
                        error="This request has expired or is unknown. "
                        "Start the connection again from your client.",
                    ),
                    status_code=400,
                    headers=_page_headers(),
                )
            pending = self._pending[rid]
            return HTMLResponse(
                self._page(rid=rid),
                headers=_page_headers(pending["redirect_uri"]),
            )

        form = await request.form()
        rid = str(form.get("rid", ""))
        supplied = str(form.get("passphrase", ""))

        pending = self._pending.get(rid)
        if pending is None:
            return HTMLResponse(
                self._page(rid=None, error="This request has expired or is unknown."),
                status_code=400,
                headers=_page_headers(),
            )

        # Verify first, then throttle. Rejecting up front would let anyone lock
        # the owner out simply by failing repeatedly; here a correct passphrase
        # always succeeds and only wrong guesses pay the delay.
        if not hmac.compare_digest(supplied.encode(), self._passphrase):
            delay = self._register_failure()
            logger.warning(
                "failed passphrase attempt for authorization request %s "
                "(delaying response %.0fs)",
                rid,
                delay,
            )
            if delay:
                await asyncio.sleep(delay)
            return HTMLResponse(
                self._page(rid=rid, error="Incorrect passphrase."),
                status_code=401,
                headers=_page_headers(pending["redirect_uri"]),
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

    def _register_failure(self) -> float:
        """Count a failed attempt and return how long to delay the response."""
        self._failures += 1
        index = min(self._failures, len(_BACKOFF_STEPS) - 1)
        return _BACKOFF_STEPS[index]

    def _page(self, *, rid: str | None, error: str | None = None) -> str:
        error_block = f'<p class="err">{html.escape(error)}</p>' if error else ""
        form_block = (
            f"""
            <form method="post" autocomplete="off">
              <input type="hidden" name="rid" value="{html.escape(rid)}">
              <label for="pp">Passphrase</label>
              <input id="pp" name="passphrase" type="password" autofocus required>
              <button type="submit">Approve</button>
            </form>
            """
            if rid
            else ""
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html.escape(self._server_label)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0;
         background: Canvas; color: CanvasText; }}
  .card {{ width: min(92vw, 26rem); padding: 2rem;
          border: 1px solid color-mix(in srgb, CanvasText 18%, transparent);
          border-radius: 14px; }}
  h1 {{ font-size: 1.15rem; margin: 0 0 .35rem; }}
  p.sub {{ margin: 0 0 1.5rem; opacity: .7; font-size: .875rem; line-height: 1.5; }}
  label {{ display: block; font-size: .8rem; margin-bottom: .4rem; opacity: .8; }}
  input {{ width: 100%; box-sizing: border-box; padding: .65rem .75rem; font-size: 1rem;
          border-radius: 8px;
          border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
          background: Canvas; color: CanvasText; }}
  button {{ margin-top: 1rem; width: 100%; padding: .7rem; font-size: .95rem;
           font-weight: 600; border: 0; border-radius: 8px;
           background: CanvasText; color: Canvas; cursor: pointer; }}
  .err {{ color: #d33; font-size: .85rem; margin: 0 0 1rem; }}
</style></head>
<body><div class="card">
  <h1>{html.escape(self._server_label)}</h1>
  <p class="sub">{html.escape(self._page_subtitle)}</p>
  {error_block}{form_block}
</div></body></html>"""

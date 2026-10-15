"""A fixed bearer-token check for FastMCP, for clients that send a static header.

Claude custom connectors can send fixed request headers (since 2026-08). A
connector set to "No sign-in" with the header `Authorization: Bearer <token>`
reaches the server with no OAuth flow at all, so the public login page, client
registration and token endpoints that PassphraseOAuthProvider exposes are not
needed. The only unauthenticated routes left are the ones the server adds itself,
such as a liveness probe.

Trade-off against PassphraseOAuthProvider: the token does not expire. Rotate it by
changing the server's configuration and re-adding the connector. Use a long random
value; guessing then is not a practical attack, so failed attempts are not
throttled.
"""

from __future__ import annotations

import hmac

from fastmcp.server.auth import AccessToken, TokenVerifier

MIN_TOKEN_LENGTH = 32


class StaticBearerVerifier(TokenVerifier):
    """Accept exactly one bearer token, compared in constant time."""

    def __init__(self, token: str, *, client_id: str = "static-bearer") -> None:
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"bearer token must be at least {MIN_TOKEN_LENGTH} characters")
        super().__init__()
        self._token = token.encode()
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._token):
            return None
        return AccessToken(token=token, client_id=self._client_id, scopes=[], expires_at=None)

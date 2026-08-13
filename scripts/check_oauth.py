#!/usr/bin/env python
"""Self-check for the built-in OAuth 2.1 flow (packages/localauth).

Server-agnostic and credential-free: it stands up a bare FastMCP server that
carries only the auth provider, so it verifies localauth rather than any
particular MCP server. Reproduces the exact path Claude.ai takes:
DCR -> authorize -> passphrase -> code -> token -> MCP call.

    .venv/bin/python scripts/check_oauth.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import uvicorn
from fastmcp import FastMCP
from starlette.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages"))

from localauth import PassphraseOAuthProvider  # noqa: E402

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

# Ephemeral, used only by this process. Injected into the temporary server the
# script starts; unrelated to .env or any real deployment. Generating it avoids
# tripping secret scanners.
PASSPHRASE = "ephemeral-check-" + secrets.token_urlsafe(24)

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"

_failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global _failures
    if not condition:
        _failures += 1
    print(f"{PASS if condition else FAIL}  {label}{(' — ' + detail) if detail else ''}")


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


async def run_checks() -> None:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
        # 0. wait for the server to come up
        for _ in range(100):
            try:
                await http.get(f"{BASE}/healthz")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.1)

        # 1. unauthenticated MCP access must be rejected
        anon = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )
        check(
            "unauthenticated /mcp access is rejected",
            anon.status_code == 401,
            f"HTTP {anon.status_code}",
        )
        check(
            "401 carries WWW-Authenticate (required by the MCP spec)",
            "www-authenticate" in {k.lower() for k in anon.headers},
            anon.headers.get("www-authenticate", "(none)"),
        )

        # 2. protected resource metadata
        meta = await http.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
        check("protected-resource metadata served", meta.status_code == 200)

        as_meta = await http.get(f"{BASE}/.well-known/oauth-authorization-server")
        check("authorization-server metadata served", as_meta.status_code == 200)
        reg_endpoint = as_meta.json().get("registration_endpoint", "")
        check("DCR supported (registration_endpoint advertised)", bool(reg_endpoint), reg_endpoint)

        # 3. dynamic client registration, as Claude.ai does it
        redirect_uri = "https://claude.ai/api/mcp/auth_callback"
        reg = await http.post(
            f"{BASE}/register",
            json={
                "client_name": "Claude",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_method": "none",
            },
        )
        check("dynamic client registration succeeds", reg.status_code in (200, 201), f"HTTP {reg.status_code}")
        client_id = reg.json().get("client_id", "")
        check("client_id issued", bool(client_id))

        # 4. authorize redirects to the passphrase page
        verifier, challenge = pkce()
        authz = await http.get(
            f"{BASE}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "st-123",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        located = authz.headers.get("location", "")
        check(
            "authorize redirects to the passphrase page",
            authz.status_code in (302, 307) and "/auth/login" in located,
            f"HTTP {authz.status_code} → {located[:60]}",
        )
        rid = parse_qs(urlparse(located).query).get("rid", [""])[0]
        check("authorization request id issued", bool(rid))

        login_page = await http.get(f"{BASE}/auth/login", params={"rid": rid})
        check("login page renders", login_page.status_code == 200)

        # 5. a wrong passphrase is rejected
        bad = await http.post(
            f"{BASE}/auth/login", data={"rid": rid, "passphrase": "wrong-passphrase"}
        )
        check("wrong passphrase rejected", bad.status_code == 401, f"HTTP {bad.status_code}")

        # 6. the correct passphrase yields an authorization code
        good = await http.post(
            f"{BASE}/auth/login", data={"rid": rid, "passphrase": PASSPHRASE}
        )
        cb = good.headers.get("location", "")
        params = parse_qs(urlparse(cb).query)
        code = params.get("code", [""])[0]
        check(
            "correct passphrase returns a code to the callback",
            good.status_code == 302 and bool(code) and cb.startswith(redirect_uri),
            f"HTTP {good.status_code}",
        )
        check("state is preserved", params.get("state", [""])[0] == "st-123")

        # 7. a mismatched PKCE verifier must be rejected
        bad_token = await http.post(
            f"{BASE}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": secrets.token_urlsafe(64),
            },
        )
        check("mismatched PKCE verifier rejected", bad_token.status_code >= 400, f"HTTP {bad_token.status_code}")

        # that attempt may have consumed the code, so authorize again
        verifier, challenge = pkce()
        authz = await http.get(
            f"{BASE}/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "st-456",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        rid = parse_qs(urlparse(authz.headers["location"]).query)["rid"][0]
        good = await http.post(
            f"{BASE}/auth/login", data={"rid": rid, "passphrase": PASSPHRASE}
        )
        code = parse_qs(urlparse(good.headers["location"]).query)["code"][0]

        # 8. token exchange
        tok = await http.post(
            f"{BASE}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        check("authorization code exchanges for an access token", tok.status_code == 200, f"HTTP {tok.status_code} {tok.text[:120]}")
        payload = tok.json() if tok.status_code == 200 else {}
        access = payload.get("access_token", "")
        refresh = payload.get("refresh_token", "")
        check("access_token issued", bool(access))
        check("refresh_token issued", bool(refresh))

        # 9. authorization code replay is blocked
        replay = await http.post(
            f"{BASE}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        check("authorization code replay blocked", replay.status_code >= 400, f"HTTP {replay.status_code}")

        # 10. call MCP with the token
        authed = await http.post(
            f"{BASE}/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "check", "version": "0"},
                },
            },
            headers={
                "authorization": f"Bearer {access}",
                "accept": "application/json, text/event-stream",
            },
        )
        check("valid bearer token grants MCP access", authed.status_code == 200, f"HTTP {authed.status_code}")

        # 11. a forged token is rejected
        forged = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "authorization": f"Bearer {secrets.token_urlsafe(32)}",
                "accept": "application/json, text/event-stream",
            },
        )
        check("forged token rejected", forged.status_code == 401, f"HTTP {forged.status_code}")

        # 12. refresh token rotation
        refreshed = await http.post(
            f"{BASE}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        check("refresh token yields new tokens", refreshed.status_code == 200, f"HTTP {refreshed.status_code}")
        if refreshed.status_code == 200:
            check("refresh token rotated", refreshed.json().get("refresh_token") != refresh)


def main() -> int:
    tmp = tempfile.mkdtemp()

    # A bare FastMCP server carrying only the auth provider. This check is about
    # localauth, so it must not depend on any particular MCP server.
    auth = PassphraseOAuthProvider(
        base_url=BASE,
        passphrase=PASSPHRASE,
        signing_secret="signing-secret-for-local-check-only",
        store_path=Path(tmp) / "oauth.json",
        server_label="Check server",
    )
    mcp: FastMCP = FastMCP(name="auth-check", auth=auth)

    @mcp.tool
    async def ping() -> str:
        """Trivial tool so the server exposes something."""
        return "pong"

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001, ANN202
        return JSONResponse({"status": "ok"})

    app = mcp.http_app(path="/mcp", transport="http")
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)

    async def orchestrate() -> None:
        task = asyncio.create_task(server.serve())
        try:
            await run_checks()
        finally:
            server.should_exit = True
            await task

    print(f"\nBuilt-in OAuth 2.1 flow check ({BASE})\n" + "-" * 52)
    asyncio.run(orchestrate())
    print("-" * 52)
    if _failures:
        print(f"{_failures} failed")
        return 1
    print("all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

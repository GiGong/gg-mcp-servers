#!/usr/bin/env python
"""내장 OAuth 2.1 플로우 자체 점검.

KIS 자격증명 없이도 돌아간다. 서버를 루프백에 띄우고 Claude.ai 가 밟는
경로(DCR → authorize → 패스프레이즈 → code → token → MCP 호출)를 그대로 재현한다.

    PYTHONPATH=src .venv/bin/python scripts/check_oauth.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import httpx
import uvicorn

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"

# 이 프로세스에서만 쓰고 버리는 값이다. 스크립트가 띄우는 임시 서버에 주입되며
# .env 나 실제 배포와는 무관하다. 하드코딩하지 않아야 시크릿 스캐너에도 걸리지 않는다.
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
        # 0. 서버가 뜰 때까지 대기
        for _ in range(100):
            try:
                await http.get(f"{BASE}/healthz")
                break
            except httpx.ConnectError:
                await asyncio.sleep(0.1)

        # 1. 인증 없이 MCP 접근 → 거부되어야 한다
        anon = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )
        check(
            "인증 없이 /mcp 접근이 거부됨",
            anon.status_code == 401,
            f"HTTP {anon.status_code}",
        )
        check(
            "401 응답에 WWW-Authenticate 헤더 포함 (MCP 스펙 요구사항)",
            "www-authenticate" in {k.lower() for k in anon.headers},
            anon.headers.get("www-authenticate", "(없음)"),
        )

        # 2. 보호 리소스 메타데이터
        meta = await http.get(f"{BASE}/.well-known/oauth-protected-resource/mcp")
        check("protected-resource 메타데이터 제공", meta.status_code == 200)

        as_meta = await http.get(f"{BASE}/.well-known/oauth-authorization-server")
        check("authorization-server 메타데이터 제공", as_meta.status_code == 200)
        reg_endpoint = as_meta.json().get("registration_endpoint", "")
        check("DCR 지원 (registration_endpoint 광고)", bool(reg_endpoint), reg_endpoint)

        # 3. 동적 클라이언트 등록 (Claude.ai 가 하는 것)
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
        check("동적 클라이언트 등록 성공", reg.status_code in (200, 201), f"HTTP {reg.status_code}")
        client_id = reg.json().get("client_id", "")
        check("client_id 발급", bool(client_id))

        # 4. authorize → 패스프레이즈 화면으로 리다이렉트
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
            "authorize 가 패스프레이즈 화면으로 리다이렉트",
            authz.status_code in (302, 307) and "/auth/login" in located,
            f"HTTP {authz.status_code} → {located[:60]}",
        )
        rid = parse_qs(urlparse(located).query).get("rid", [""])[0]
        check("인가 요청 rid 발급", bool(rid))

        login_page = await http.get(f"{BASE}/auth/login", params={"rid": rid})
        check("로그인 페이지 렌더링", login_page.status_code == 200)

        # 5. 틀린 패스프레이즈는 거부
        bad = await http.post(
            f"{BASE}/auth/login", data={"rid": rid, "passphrase": "wrong-passphrase"}
        )
        check("틀린 패스프레이즈 거부", bad.status_code == 401, f"HTTP {bad.status_code}")

        # 6. 맞는 패스프레이즈 → 인가 코드
        good = await http.post(
            f"{BASE}/auth/login", data={"rid": rid, "passphrase": PASSPHRASE}
        )
        cb = good.headers.get("location", "")
        params = parse_qs(urlparse(cb).query)
        code = params.get("code", [""])[0]
        check(
            "올바른 패스프레이즈 → 콜백으로 code 반환",
            good.status_code == 302 and bool(code) and cb.startswith(redirect_uri),
            f"HTTP {good.status_code}",
        )
        check("state 값 보존", params.get("state", [""])[0] == "st-123")

        # 7. 잘못된 PKCE verifier 는 거부되어야 한다
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
        check("잘못된 PKCE verifier 거부", bad_token.status_code >= 400, f"HTTP {bad_token.status_code}")

        # 위 시도로 코드가 소모됐을 수 있으니 인가를 다시 받는다
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

        # 8. 토큰 교환
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
        check("인가 코드 → 액세스 토큰 교환", tok.status_code == 200, f"HTTP {tok.status_code} {tok.text[:120]}")
        payload = tok.json() if tok.status_code == 200 else {}
        access = payload.get("access_token", "")
        refresh = payload.get("refresh_token", "")
        check("access_token 발급", bool(access))
        check("refresh_token 발급", bool(refresh))

        # 9. 코드 재사용 차단
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
        check("인가 코드 재사용 차단", replay.status_code >= 400, f"HTTP {replay.status_code}")

        # 10. 토큰으로 MCP 호출
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
        check("유효한 Bearer 토큰으로 MCP 접근 허용", authed.status_code == 200, f"HTTP {authed.status_code}")

        # 11. 위조 토큰 거부
        forged = await http.post(
            f"{BASE}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={
                "authorization": f"Bearer {secrets.token_urlsafe(32)}",
                "accept": "application/json, text/event-stream",
            },
        )
        check("위조 토큰 거부", forged.status_code == 401, f"HTTP {forged.status_code}")

        # 12. 리프레시 토큰 회전
        refreshed = await http.post(
            f"{BASE}/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh,
                "client_id": client_id,
            },
        )
        check("리프레시 토큰으로 재발급", refreshed.status_code == 200, f"HTTP {refreshed.status_code}")
        if refreshed.status_code == 200:
            check("리프레시 토큰 회전됨", refreshed.json().get("refresh_token") != refresh)


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ.update(
        {
            "KIS_ENV": "real",
            "KIS_APP_KEY": "DUMMY-KEY-FOR-OAUTH-CHECK",
            "KIS_APP_SECRET": "DUMMY-SECRET-FOR-OAUTH-CHECK",
            "AUTH_MODE": "oauth",
            "MCP_PUBLIC_URL": BASE,
            "MCP_PATH": "/mcp",
            "OAUTH_PASSPHRASE": PASSPHRASE,
            "OAUTH_SIGNING_SECRET": "signing-secret-for-local-check-only",
            "OAUTH_STORE": f"{tmp}/oauth.json",
            "KIS_TOKEN_CACHE": f"{tmp}/token.json",
            "LOG_LEVEL": "WARNING",
            "LOG_FILE": "",
        }
    )

    from kis_mcp.config import load_settings
    from kis_mcp.server import build_server

    settings = load_settings(env_file="/nonexistent")
    mcp, _client = build_server(settings)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001, ANN202
        from starlette.responses import JSONResponse

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

    print(f"\n내장 OAuth 2.1 플로우 점검 ({BASE})\n" + "-" * 52)
    asyncio.run(orchestrate())
    print("-" * 52)
    if _failures:
        print(f"실패 {_failures}건")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())

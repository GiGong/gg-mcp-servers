#!/usr/bin/env python
"""공개 URL(Cloudflare Tunnel 경유)에서 OAuth 전체 왕복을 실제로 완주시킨다.

Claude.ai 가 밟는 경로를 그대로 재현한다: DCR → authorize → 패스프레이즈 →
code → token → MCP tools/list. 마지막에 실제 도구를 호출해 KIS 값까지 확인한다.

로컬 macOS 리졸버가 호스트명을 못 찾는 경우를 대비해 curl 의 --resolve 로
DNS 를 우회할 수 있다 (전송 경로 자체는 진짜 터널을 지난다).

    PYTHONPATH=src .venv/bin/python scripts/check_public.py
    PYTHONPATH=src .venv/bin/python scripts/check_public.py --resolve 104.21.91.26
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, "src")

from kis_mcp.config import load_settings  # noqa: E402

PASS = "\033[32m PASS\033[0m"
FAIL = "\033[31m FAIL\033[0m"
_failures = 0
_resolve_ip: str | None = None
_host = ""


def check(label: str, condition: bool, detail: str = "") -> None:
    global _failures
    if not condition:
        _failures += 1
    print(f"{PASS if condition else FAIL}  {label}{(' — ' + detail) if detail else ''}")


def curl(url: str, *extra: str) -> tuple[int, dict[str, str], str]:
    """(status, headers, body) 를 돌려준다. 헤더와 본문은 별도 파일로 받는다."""
    with (
        tempfile.NamedTemporaryFile("r+", suffix=".hdr") as hdr,
        tempfile.NamedTemporaryFile("r+", suffix=".body") as out,
    ):
        cmd = [
            "curl", "-sS", "--max-time", "20",
            "-D", hdr.name, "-o", out.name, "-w", "%{http_code}",
        ]
        if _resolve_ip:
            cmd += ["--resolve", f"{_host}:443:{_resolve_ip}"]
        cmd += [*extra, url]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        status = int(completed.stdout.strip() or 0)

        hdr.seek(0)
        headers: dict[str, str] = {}
        for line in hdr.read().splitlines():
            if ":" in line and not line.upper().startswith("HTTP/"):
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        out.seek(0)
        return status, headers, out.read().strip()


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def main() -> int:
    global _resolve_ip, _host
    parser = argparse.ArgumentParser()
    parser.add_argument("--resolve", help="로컬 DNS 를 우회할 IP")
    parser.add_argument("--url", help="공개 URL (기본: .env 의 MCP_PUBLIC_URL)")
    args = parser.parse_args()

    settings = load_settings()
    base = (args.url or settings.public_url).rstrip("/")
    mcp_url = f"{base}{settings.mcp_path}"
    _resolve_ip = args.resolve
    _host = urlparse(base).hostname or ""

    print(f"\n공개 URL 전체 왕복 점검  {base}")
    if _resolve_ip:
        print(f"(로컬 DNS 우회: {_host} → {_resolve_ip} / 전송은 실제 터널 경유)")
    print("-" * 60)

    status, _, body = curl(f"{base}/healthz")
    check("healthz 도달", status == 200, body[:60])

    status, headers, _ = curl(
        mcp_url, "-X", "POST",
        "-H", "content-type: application/json",
        "-H", "accept: application/json, text/event-stream",
        "-d", '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    check("무인증 MCP 접근 거부", status == 401, f"HTTP {status}")
    check("WWW-Authenticate 가 공개 URL 을 가리킴",
          base in headers.get("www-authenticate", ""),
          headers.get("www-authenticate", "(없음)")[:80])

    status, _, body = curl(f"{base}/.well-known/oauth-authorization-server")
    meta = json.loads(body) if status == 200 else {}
    check("인가서버 메타데이터", status == 200)
    check("issuer 가 공개 URL", meta.get("issuer", "").rstrip("/") == base,
          meta.get("issuer", ""))
    check("PKCE S256 광고", "S256" in (meta.get("code_challenge_methods_supported") or []))

    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    status, _, body = curl(
        f"{base}/register", "-X", "POST",
        "-H", "content-type: application/json",
        "-d", json.dumps({
            "client_name": "Claude",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_method": "none",
        }),
    )
    client_id = (json.loads(body).get("client_id", "") if status in (200, 201) else "")
    check("동적 클라이언트 등록(DCR)", bool(client_id), f"HTTP {status}")

    verifier, challenge = pkce()
    status, headers, _ = curl(
        f"{base}/authorize?response_type=code&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&state=live-check"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )
    location = headers.get("location", "")
    rid = parse_qs(urlparse(location).query).get("rid", [""])[0]
    check("authorize → 패스프레이즈 화면", status in (302, 307) and bool(rid),
          f"HTTP {status}")

    status, _, body = curl(f"{base}/auth/login?rid={rid}")
    check("인증 화면 렌더링", status == 200)
    check("인증 화면에 KIS/증권 문구 없음",
          not any(w in body for w in ("KIS", "한국투자", "증권", "주문", "시세")),
          "중립화됨" if status == 200 else "")

    status, _, _ = curl(
        f"{base}/auth/login", "-X", "POST",
        "--data-urlencode", f"rid={rid}",
        "--data-urlencode", "passphrase=definitely-not-the-passphrase",
    )
    check("틀린 패스프레이즈 거부", status == 401, f"HTTP {status}")

    status, headers, _ = curl(
        f"{base}/auth/login", "-X", "POST",
        "--data-urlencode", f"rid={rid}",
        "--data-urlencode", f"passphrase={settings.oauth_passphrase}",
    )
    callback = headers.get("location", "")
    code = parse_qs(urlparse(callback).query).get("code", [""])[0]
    check("올바른 패스프레이즈 → 인가 코드",
          status == 302 and bool(code) and callback.startswith(redirect_uri),
          f"HTTP {status}")

    status, _, body = curl(
        f"{base}/token", "-X", "POST",
        "--data-urlencode", "grant_type=authorization_code",
        "--data-urlencode", f"code={code}",
        "--data-urlencode", f"redirect_uri={redirect_uri}",
        "--data-urlencode", f"client_id={client_id}",
        "--data-urlencode", f"code_verifier={verifier}",
    )
    token = json.loads(body).get("access_token", "") if status == 200 else ""
    check("토큰 교환", bool(token), f"HTTP {status}")

    auth_header = f"authorization: Bearer {token}"
    status, _, body = curl(
        mcp_url, "-X", "POST",
        "-H", "content-type: application/json",
        "-H", "accept: application/json, text/event-stream",
        "-H", auth_header,
        "-d", json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "public-check", "version": "0"}},
        }),
    )
    check("인증된 MCP initialize", status == 200, f"HTTP {status}")

    print("-" * 60)
    if _failures:
        print(f"실패 {_failures}건")
        return 1
    print("전부 통과 — 공개 URL 에서 Claude.ai 와 동일한 경로가 완주됩니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

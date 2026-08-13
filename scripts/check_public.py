#!/usr/bin/env python
"""Complete a full OAuth round trip against a server's public URL (via the tunnel).

Reproduces the path Claude.ai takes: DCR -> authorize -> passphrase -> code ->
token -> MCP initialize.

If the local macOS resolver cannot resolve the hostname, curl's --resolve can
bypass DNS; the transport still goes through the real tunnel.

    .venv/bin/python scripts/check_public.py --env servers/kis/.env
    .venv/bin/python scripts/check_public.py --env servers/kis/.env --resolve 1.2.3.4
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse

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
    """Return (status, headers, body). Headers and body are captured separately."""
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


def read_env(path: Path) -> dict[str, str]:
    """Minimal .env reader. Avoids depending on any particular server package."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"error: cannot read {path}: {exc}", file=sys.stderr)
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


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
    parser.add_argument(
        "--env",
        default="servers/kis/.env",
        help="Path to the server's .env (default: servers/kis/.env)",
    )
    parser.add_argument("--resolve", help="IP to bypass local DNS with")
    parser.add_argument("--url", help="Public URL (overrides MCP_PUBLIC_URL)")
    args = parser.parse_args()

    env = read_env(Path(args.env))
    base = (args.url or env.get("MCP_PUBLIC_URL", "")).rstrip("/")
    if not base:
        print(f"error: MCP_PUBLIC_URL not found in {args.env}", file=sys.stderr)
        return 1
    passphrase = env.get("OAUTH_PASSPHRASE", "")
    mcp_url = f"{base}{env.get('MCP_PATH', '/mcp')}"
    _resolve_ip = args.resolve
    _host = urlparse(base).hostname or ""

    print(f"\nPublic URL round-trip check  {base}")
    if _resolve_ip:
        print(f"(bypassing local DNS: {_host} -> {_resolve_ip}; transport still uses the tunnel)")
    print("-" * 60)

    status, _, body = curl(f"{base}/healthz")
    check("healthz reachable", status == 200, body[:60])

    status, headers, _ = curl(
        mcp_url, "-X", "POST",
        "-H", "content-type: application/json",
        "-H", "accept: application/json, text/event-stream",
        "-d", '{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )
    check("unauthenticated MCP access rejected", status == 401, f"HTTP {status}")
    check("WWW-Authenticate points at the public URL",
          base in headers.get("www-authenticate", ""),
          headers.get("www-authenticate", "(none)")[:80])

    status, _, body = curl(f"{base}/.well-known/oauth-authorization-server")
    meta = json.loads(body) if status == 200 else {}
    check("authorization-server metadata", status == 200)
    check("issuer matches the public URL", meta.get("issuer", "").rstrip("/") == base,
          meta.get("issuer", ""))
    check("PKCE S256 advertised", "S256" in (meta.get("code_challenge_methods_supported") or []))

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
    check("dynamic client registration (DCR)", bool(client_id), f"HTTP {status}")

    verifier, challenge = pkce()
    status, headers, _ = curl(
        f"{base}/authorize?response_type=code&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&state=live-check"
        f"&code_challenge={challenge}&code_challenge_method=S256"
    )
    location = headers.get("location", "")
    rid = parse_qs(urlparse(location).query).get("rid", [""])[0]
    check("authorize reaches the passphrase page", status in (302, 307) and bool(rid),
          f"HTTP {status}")

    status, _, body = curl(f"{base}/auth/login?rid={rid}")
    check("login page renders", status == 200)
    # Word boundaries matter: a substring match would flag the CSS ("border"
    # contains "order").
    leaks = [
        w for w in ("KIS", "Korea Investment", "stock", "order", "quote",
                    "trading", "broker")
        if re.search(rf"\b{re.escape(w)}\b", body, re.I)
    ]
    check("login page reveals nothing about the service",
          not leaks,
          "neutral" if not leaks else f"leaked: {', '.join(leaks)}")

    status, _, _ = curl(
        f"{base}/auth/login", "-X", "POST",
        "--data-urlencode", f"rid={rid}",
        "--data-urlencode", "passphrase=definitely-not-the-passphrase",
    )
    check("wrong passphrase rejected", status == 401, f"HTTP {status}")

    status, headers, _ = curl(
        f"{base}/auth/login", "-X", "POST",
        "--data-urlencode", f"rid={rid}",
        "--data-urlencode", f"passphrase={passphrase}",
    )
    callback = headers.get("location", "")
    code = parse_qs(urlparse(callback).query).get("code", [""])[0]
    check("correct passphrase yields an authorization code",
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
    check("token exchange", bool(token), f"HTTP {status}")

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
    check("authenticated MCP initialize", status == 200, f"HTTP {status}")

    print("-" * 60)
    if _failures:
        print(f"{_failures} failed")
        return 1
    print("all passed - the public URL completes the same path Claude.ai uses.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

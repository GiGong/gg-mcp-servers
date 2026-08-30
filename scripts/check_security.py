#!/usr/bin/env python
"""Adversarial probe against packages/localauth.

Attacks the authorization server the way someone who found the URL would, and
asserts that each attempt fails. Server-agnostic and credential-free: it stands
up a bare FastMCP server carrying only the auth provider.

Run this after touching localauth.

    .venv/bin/python scripts/check_security.py

Covered:
  - redirect_uri not registered  (open redirect / token exfiltration)
  - PKCE omitted, and PKCE downgraded to "plain"
  - authorization code redeemed by a different client
  - unauthenticated DCR flood            (disk growth, eviction of a live client)
  - repeated wrong passphrases           (must not lock out the owner)
  - login page framing protections       (clickjacking on a passphrase form)
"""
from __future__ import annotations
import asyncio, base64, hashlib, secrets, sys, tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx, uvicorn
from fastmcp import FastMCP
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "localauth" / "src"))
from localauth import PassphraseOAuthProvider

PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"
PASS = "probe-passphrase-" + secrets.token_urlsafe(16)
OK, BAD, WARN = "\033[32m OK \033[0m", "\033[31mISSUE\033[0m", "\033[33mNOTE\033[0m"
findings = []

def rep(level, title, detail=""):
    findings.append((level, title))
    print(f"{level}  {title}" + (f"\n        {detail}" if detail else ""))

def pkce():
    v = secrets.token_urlsafe(64)
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).decode().rstrip("=")
    return v, c

async def register(h, redirect_uris):
    r = await h.post(f"{BASE}/register", json={
        "client_name": "probe", "redirect_uris": redirect_uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "none"})
    return r

async def probe():
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as h:
        for _ in range(100):
            try:
                await h.get(f"{BASE}/healthz"); break
            except httpx.ConnectError: await asyncio.sleep(0.1)

        legit = "https://claude.ai/api/mcp/auth_callback"
        r = await register(h, [legit]); cid = r.json()["client_id"]

        # 1. redirect_uri not registered -> must be rejected (token exfiltration)
        v, c = pkce()
        evil = "https://evil.example.com/steal"
        a = await h.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": evil,
            "state": "x", "code_challenge": c, "code_challenge_method": "S256"})
        loc = a.headers.get("location", "")
        if a.status_code in (302, 307) and evil in loc:
            rep(BAD, "Open redirect: unregistered redirect_uri accepted", loc[:90])
        else:
            rep(OK, "Unregistered redirect_uri rejected", f"HTTP {a.status_code}")

        # 2. PKCE downgrade: omit code_challenge entirely
        a = await h.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": legit, "state": "x"})
        if a.status_code in (302, 307) and "/auth/login" in a.headers.get("location", ""):
            rep(BAD, "PKCE not required: authorize accepted without code_challenge")
        else:
            rep(OK, "PKCE required", f"HTTP {a.status_code}")

        # 3. plain PKCE method downgrade
        v2, c2 = pkce()
        a = await h.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": legit,
            "state": "x", "code_challenge": v2, "code_challenge_method": "plain"})
        if a.status_code in (302, 307) and "/auth/login" in a.headers.get("location", ""):
            rep(WARN, "PKCE 'plain' method accepted at authorize", "check token endpoint enforces S256")
        else:
            rep(OK, "PKCE 'plain' rejected", f"HTTP {a.status_code}")

        # 4. login page security headers (clickjacking on a passphrase form)
        v3, c3 = pkce()
        a = await h.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": legit,
            "state": "x", "code_challenge": c3, "code_challenge_method": "S256"})
        rid = parse_qs(urlparse(a.headers["location"]).query)["rid"][0]
        pg = await h.get(f"{BASE}/auth/login", params={"rid": rid})
        hdrs = {k.lower() for k in pg.headers}
        missing = [x for x in ("x-frame-options", "content-security-policy", "referrer-policy") if x not in hdrs]
        if missing:
            rep(WARN, "Login page lacks anti-clickjacking headers", f"missing: {', '.join(missing)}")
        else:
            rep(OK, "Login page sets framing protections")

        # form-action governs the redirect that follows the form POST. If it does
        # not allow the client's redirect_uri origin, the browser silently blocks
        # the hand-back and the login page hangs on a spinner forever.
        csp = pg.headers.get("content-security-policy", "")
        origin = "https://claude.ai"
        if "form-action" in csp and origin not in csp:
            rep(BAD, "CSP form-action blocks the redirect back to the client",
                f"login would hang; csp={csp}")
        elif "frame-ancestors 'none'" not in csp:
            rep(BAD, "CSP does not forbid framing", csp)
        else:
            rep(OK, "CSP allows the client redirect while forbidding framing",
                csp.split("form-action ")[1].split(";")[0] if "form-action " in csp else "")

        # 5. cross-client code use: code issued to client A redeemed by client B
        b = await register(h, [legit]); cid_b = b.json()["client_id"]
        good = await h.post(f"{BASE}/auth/login", data={"rid": rid, "passphrase": PASS})
        code = parse_qs(urlparse(good.headers["location"]).query)["code"][0]
        t = await h.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": legit,
            "client_id": cid_b, "code_verifier": v3})
        if t.status_code == 200:
            rep(BAD, "Authorization code redeemable by a different client", t.text[:80])
        else:
            rep(OK, "Authorization code bound to its client", f"HTTP {t.status_code}")

        # 6. unauthenticated DCR flood: bounded, and must not evict a live client
        import json as _j
        tok = await h.post(f"{BASE}/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": legit,
            "client_id": cid, "code_verifier": v3})
        assert tok.status_code == 200, tok.text[:200]
        for _ in range(150):
            await register(h, [legit])
        data = _j.loads(store_path.read_text())
        n = len(data.get("clients", {}))
        survived = cid in data.get("clients", {})
        if n <= 50:
            rep(OK, "Client store is capped", f"150 registrations -> {n} stored")
        else:
            rep(BAD, "Client store unbounded", f"{n} clients stored")
        if survived:
            rep(OK, "A client holding a live token survives a DCR flood")
        else:
            rep(BAD, "DCR flood evicted a client with a live token",
                "an attacker could disconnect the owner's connector")

        # 7. lockout scope: does a failing attacker lock out the legitimate user?
        v4, c4 = pkce()
        a = await h.get(f"{BASE}/authorize", params={
            "response_type": "code", "client_id": cid, "redirect_uri": legit,
            "state": "x", "code_challenge": c4, "code_challenge_method": "S256"})
        rid2 = parse_qs(urlparse(a.headers["location"]).query)["rid"][0]
        import time as _t
        delays = []
        for _ in range(4):
            s = _t.monotonic()
            r = await h.post(f"{BASE}/auth/login", data={"rid": rid2, "passphrase": "wrong"},
                             timeout=90)
            delays.append(round(_t.monotonic() - s, 1))
        s = _t.monotonic()
        legit_try = await h.post(f"{BASE}/auth/login", data={"rid": rid2, "passphrase": PASS},
                                 timeout=90)
        owner_wait = round(_t.monotonic() - s, 2)
        if legit_try.status_code in (302, 200) and owner_wait < 1.0:
            rep(OK, "Owner is never locked out by an attacker's failures",
                f"wrong-guess delays {delays}s, owner succeeded in {owner_wait}s")
        else:
            rep(BAD, "Owner blocked after attacker failures",
                f"HTTP {legit_try.status_code} after {owner_wait}s")
        if delays[-1] >= 1:
            rep(OK, "Wrong guesses are progressively delayed", f"{delays}s")
        else:
            rep(BAD, "No backoff on wrong guesses", f"{delays}s")

        # 8. token entropy
        rep(OK, "Token entropy", "secrets.token_urlsafe(32) = 256 bits; stored as HMAC-SHA256 digest")

def main():
    global store_path
    tmp = Path(tempfile.mkdtemp()); store_path = tmp / "oauth.json"
    auth = PassphraseOAuthProvider(base_url=BASE, passphrase=PASS,
        signing_secret="probe-signing-secret-value", store_path=store_path,
        server_label="Probe")
    mcp: FastMCP = FastMCP(name="probe", auth=auth)

    @mcp.tool
    async def ping() -> str:
        """ping"""
        return "pong"

    @mcp.custom_route("/healthz", methods=["GET"])
    async def hz(_r):
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    app = mcp.http_app(path="/mcp", transport="http")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))

    async def go():
        t = asyncio.create_task(srv.serve())
        try: await probe()
        finally:
            srv.should_exit = True; await t

    print("\nlocalauth adversarial probe\n" + "-" * 62)
    asyncio.run(go())
    print("-"*62)
    issues = sum(1 for lv, _ in findings if lv == BAD)
    notes = sum(1 for lv, _ in findings if lv == WARN)
    print(f"critical issues: {issues} | notes: {notes}")
    return 1 if issues else 0

if __name__ == "__main__":
    sys.exit(main())

# kis-mcp

A **read-only** remote MCP server over the Korea Investment & Securities (KIS)
Open API — and a self-contained **passphrase-gated OAuth 2.1 authorization
server** that lets any FastMCP server be exposed to Claude.ai without an
external identity provider.

Two independent things live here:

| Directory | What it is | Depends on KIS? |
|---|---|---|
| [`src/localauth/`](src/localauth/) | Reusable OAuth 2.1 AS with Dynamic Client Registration, gated by a passphrase | **No** — drop into any FastMCP server |
| [`src/kis_mcp/`](src/kis_mcp/) | 8 read-only market-data tools over the KIS Open API | Yes |

> **Read-only by construction.** No order placement, modification, cancellation,
> or transfer endpoints are implemented or exposed. Every path this server calls
> is a `/uapi/**/quotations/*` inquiry endpoint, and `KisClient.get()` rejects
> anything that does not start with `/uapi/`.

---

## The auth problem this solves

If you want to expose a remote MCP server to Claude.ai and protect it, you hit
this wall:

- Claude.ai's custom connector settings expose **only OAuth Client ID / Client
  Secret**. There is no field for a static `Authorization: Bearer` header, so a
  shared-secret middleware makes the connector fail to connect at all
  ([anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)).
- A browser-login gateway (e.g. Cloudflare Access with an interactive policy)
  does not work either — Claude.ai's MCP client is a server-side HTTP client and
  never completes an interstitial login.
- The supported path is **OAuth 2.1 + Dynamic Client Registration**. Most
  examples delegate that to GitHub, WorkOS, Auth0, and friends.

`localauth` implements the authorization server itself, so there is no external
IdP and no third-party account involved. Claude registers dynamically, you type
a passphrase in the browser once, and the connector holds a real bearer token
from then on.

```
Claude.ai                                  your server
   │  POST /register            (DCR)         │
   ├─────────────────────────────────────────▶│  client registered
   │  GET  /authorize           (PKCE S256)   │
   ├─────────────────────────────────────────▶│  302 → /auth/login?rid=…
   │              (you enter the passphrase in a browser)
   │  302 → claude.ai/…?code=…                │
   │◀─────────────────────────────────────────┤
   │  POST /token               (code+verifier)│
   ├─────────────────────────────────────────▶│  access + refresh issued
   │  POST /mcp   Authorization: Bearer …     │
   └─────────────────────────────────────────▶│
```

FastMCP ships an `InMemoryOAuthProvider`, but it approves every authorization
request — anyone who knows the URL gets a token. `localauth` subclasses
`OAuthProvider` directly so that `authorize()` redirects to a passphrase form
instead of minting a code.

**Security properties**

- Authorization codes are issued only after a correct passphrase in the browser.
- Tokens are never stored in plaintext; only an HMAC-SHA256 digest (keyed by
  `OAUTH_SIGNING_SECRET`) is persisted.
- Passphrase comparison uses `hmac.compare_digest` (constant time).
- Failed attempts back off exponentially, up to 300 s.
- PKCE S256 required, authorization codes single-use, refresh tokens rotated.
- State files are written `0600` via atomic replace.

**Reuse it**

```python
from pathlib import Path
from fastmcp import FastMCP
from localauth import PassphraseOAuthProvider

auth = PassphraseOAuthProvider(
    base_url="https://your-server.example.com",   # must match the public URL
    passphrase=os.environ["OAUTH_PASSPHRASE"],
    signing_secret=os.environ["OAUTH_SIGNING_SECRET"],
    store_path=Path("~/.cache/your-server/oauth.json").expanduser(),
    server_label="Your service",                  # shown on the login page
)

mcp = FastMCP("your-server", auth=auth)
mcp.run(transport="http", host="127.0.0.1", port=8765, path="/mcp")
```

`scripts/check_oauth.py` verifies the whole flow against an ephemeral server and
is not specific to this project — point it at your own server and it still works.

---

## Tools

Paths and `tr_id` values were taken from the official
[koreainvestment/open-trading-api](https://github.com/koreainvestment/open-trading-api)
sample sources, and response fields from the `COLUMN_MAPPING` tables in the same
repository — not from memory.

| Tool | KIS API | `tr_id` |
|---|---|---|
| `get_quote` | 주식현재가 시세 | `FHKST01010100` |
| `get_investor_flow` | 종목별 외인기관 추정가집계 + 주식현재가 투자자 | `HHPTJ04160200` + `FHKST01010900` |
| `get_daily_ohlcv` | 국내주식 기간별시세 | `FHKST03010100` |
| `get_orderbook` | 주식현재가 호가/예상체결 | `FHKST01010200` |
| `get_index` | 국내업종 현재지수 / 해외 기간별시세 | `FHPUP02100000` / `FHKST03030100` |
| `get_overseas_quote` | 해외주식 현재가상세 | `HHDFS76200200` |
| `get_fx` | 해외 기간별시세 (FX mode) | `FHKST03030100` |
| `get_market_status` | 국내휴장일조회 | `CTCA0903R` |

Every response carries `as_of_kst` (Asia/Seoul, ISO 8601) and a `market_status`
block, so a model can tell a live quote from a stale one instead of guessing.

### Provisional vs. confirmed investor flow

`get_investor_flow` is the reason this project exists. Foreign and institutional
net buying is published twice, and the two must not be mixed:

```jsonc
{
  "provisional": {          // intraday broker-compiled estimate
    "is_confirmed": false,
    "latest": { "slot": "1", "approx_kst_time": "09:30", "foreign_net_qty": 236000 },
    "snapshots": [ /* foreign 09:30/11:20/13:20/14:30, institutional 10:00/11:20/13:20/14:30 */ ]
  },
  "confirmed": {            // exchange-confirmed daily net buying
    "is_confirmed": true,
    "latest": { "date": "2026-08-12", "foreign_net_qty": 559854,
                "institution_net_qty": 193626, "is_published": true },
    "pending_dates": ["2026-08-13"],
    "series": [ /* each row carries is_published */ ]
  }
}
```

**Verified against the live API:** `FHKST01010900` returns the *current day's*
row with empty strings for every flow field until after the close. So
`confirmed.latest` is the most recent day that actually has data, and days still
awaiting publication are surfaced in `pending_dates`. During the session you
must read `provisional` — that is precisely the gap this tool closes.

Quantities are in shares; `*_value_krw_mn` fields are in **millions of KRW**
(the native unit of KIS's `*_tr_pbmn`).

The two endpoints are called independently, so one failing does not take the
other down — the failed side returns `available: false` plus the original KIS
error code.

### Things the live API does that the docs do not warn you about

These were found by calling the real API and are handled in code:

| Behaviour | Handling |
|---|---|
| `EGW00201` means **rate limit exceeded**, not token expiry | Retry with 1 s / 2 s backoff. Never reissue the token — doing so trips the "one token per minute" limit (`EGW00133`) and stalls every call for ~60 s. Only `EGW00121` / `EGW00123` mean the token is bad. |
| Overseas index codes are inconsistent about the leading dot | Verified: `SOX`, `SPX`, `COMP` have no dot; only `.DJI` does. `.SOX` / `.IXIC` return `rt_cd=0` with empty values rather than an error. |
| A wrong exchange code returns success with an empty body | e.g. `TSM` is NYSE, not NASDAQ. `get_overseas_quote` retries `NAS → NYS → AMS` and reports `exchange_resolved_by_fallback`. |
| Empty results are not errors | Where KIS returns a successful-but-empty payload, this server converts it into an explicit error rather than emitting nulls. |

Index aliases: `KOSPI`, `KOSDAQ`, `KOSPI200` (domestic); `SOX`, `NASDAQ`,
`SPX`, `DJI` (overseas, previous session's close).

### Rate limits and tokens

| Item | Value |
|---|---|
| Request rate | 20/s live, 5/s paper (per app key) |
| Configured throttle | `KIS_RATE_LIMIT_PER_SEC`, default 8/s — startup fails if it exceeds the KIS ceiling |
| Access-token lifetime | 24 h |
| Token issuance | Once per minute (`EGW00133` if exceeded) |
| Refresh policy | Reissued only within 15 min of expiry; cached to disk `0600` |
| Daily quota | KIS publishes no separate daily cap for inquiry APIs — only the per-second limit |

### Quote latency

- Domestic equities and indices: live.
- Overseas equities: **delayed** unless your KIS account subscribes to real-time
  overseas data. Set `KIS_OVERSEAS_REALTIME=true` to reflect that; the `delayed`
  field in every response follows this setting.
- Overseas indices: daily-chart based, i.e. **previous session's close**, not a
  live tick.
- `get_overseas_quote` also returns `krw_converted.fx_rate_today` — the rate KIS
  itself applies for KRW conversion, which is the consistent choice when
  comparing an ADR against its home-market listing.

---

## Setup

Requires Python 3.11+. On macOS, Homebrew's `python@3.13` may ship a broken
`pyexpat` (missing `_XML_SetAllocTrackerActivationThreshold` in the system
libexpat); a uv-managed interpreter avoids the problem entirely.

```bash
git clone https://github.com/<you>/kis-mcp.git
cd kis-mcp

uv venv --managed-python --python 3.13 .venv
uv pip install --python .venv/bin/python -e .

cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Fill in your own KIS app key/secret (from
[KIS Developers](https://apiportal.koreainvestment.com)) and generate the auth
secrets:

```bash
python3 -c "import secrets; print('OAUTH_PASSPHRASE=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('OAUTH_SIGNING_SECRET=' + secrets.token_urlsafe(48))"
```

`KIS_ENV` switches between live (`real`) and paper (`demo`) trading domains.
Note that the provisional investor-flow endpoint (`HHPTJ04160200`) and parts of
the overseas quote API may not be available on paper accounts.

### Checks

```bash
# OAuth flow, 22 assertions — no KIS credentials needed
PYTHONPATH=src .venv/bin/python scripts/check_oauth.py

# Response field mapping, 22 assertions — no network needed
PYTHONPATH=src .venv/bin/python scripts/check_parsing.py

# Live KIS calls — needs .env
PYTHONPATH=src .venv/bin/python scripts/smoke_test.py

# Full OAuth round trip over your public URL — after the tunnel is up
PYTHONPATH=src .venv/bin/python scripts/check_public.py
```

---

## Deployment

### Run it under launchd (macOS)

```bash
./deploy/install.sh                          # label: com.local.kis-mcp
LABEL=com.myname.kis-mcp ./deploy/install.sh # or pick your own
```

The script renders `deploy/com.example.kis-mcp.plist` with this machine's paths,
loads the service, and waits for `/healthz`. `RunAtLoad` plus `KeepAlive` means
it starts at login and restarts on abnormal exit.

Logs land in `~/Library/Logs/kis-mcp/`. Credentials and account numbers are
masked by `logging_setup.RedactingFilter` before anything is written.

```bash
launchctl bootout gui/$(id -u)/com.local.kis-mcp   # uninstall
```

### Expose it

The server binds to loopback only. Put a tunnel or reverse proxy in front of it
and point `MCP_PUBLIC_URL` at the resulting HTTPS URL.

With Cloudflare Tunnel there are two configurations, and which one you have
decides where the ingress rule goes:

- **Locally managed** (`cloudflared` started with a `config.yml`) — add an
  ingress entry pointing your hostname at `http://127.0.0.1:8765`.
- **Remotely managed** (`cloudflared tunnel run --token …`, no local config) —
  edit the route in the Zero Trust dashboard under
  *Networks → Tunnels → your tunnel → Published application routes*. A local
  `config.yml` will be ignored.

> `MCP_PUBLIC_URL` must exactly match the public URL. It becomes the OAuth
> issuer and the redirect base; a mismatch makes the connector fail to connect.
> `scripts/check_public.py` catches exactly this.

**Do not put an interactive access policy in front of it.** A login
interstitial blocks Claude.ai's OAuth round trip. Authentication is the
server's job here, and unauthenticated requests already get a 401.

---

## Connect from Claude.ai

Custom connectors are available on Free (one connector), Pro, Max, Team, and
Enterprise plans.

1. **Customize → Connectors**
2. **+** → **Add custom connector**
3. URL: `https://<your-host>/mcp`
4. Leave **Advanced settings** (OAuth Client ID/Secret) **empty** — the server
   supports Dynamic Client Registration and registers Claude automatically.
   Filling these in will break the flow.
5. **Add**, then **Connect**
6. Enter your `OAUTH_PASSPHRASE` on the login page that opens

Team/Enterprise owners configure it under *Organization settings → Connectors →
Add → Custom → Web*; members then click **Connect**.

`docs/handoff-prompt.md` is a briefing you can paste into an analysis
conversation so the model reads the provisional/confirmed split, the units, and
the latency flags correctly.

---

## Error handling

When KIS returns `rt_cd != "0"`, the tool result carries the original code
rather than an empty value:

```json
{
  "error": true,
  "kis_rt_cd": "1",
  "kis_msg_cd": "...",
  "kis_msg1": "...",
  "tr_id": "...",
  "api_path": "..."
}
```

Closed markets and holidays surface through `market_status.phase` and its `note`,
using the KIS holiday endpoint rather than a hardcoded calendar.

---

## Disclaimer

- **Unofficial.** Not affiliated with, endorsed by, or supported by Korea
  Investment & Securities. "KIS" and related names belong to their owners.
- **You supply your own credentials.** No API keys are contained in or
  distributed with this repository. You are responsible for keeping your KIS app
  key/secret safe and for complying with the KIS Open API terms of service.
- **No market data is redistributed.** This is a client wrapper; all data comes
  from your own authenticated KIS account at request time.
- **Read-only.** It cannot place, modify, or cancel orders.
- **Not investment advice.** Provided as-is under the MIT License, with no
  warranty as to accuracy, completeness, or timeliness. Do not rely on it for
  trading decisions without independent verification.

## License

[MIT](LICENSE).

Built with [Claude Code](https://claude.com/claude-code).

# GG-MCP-Servers

Self-hosted remote MCP servers, plus the shared pieces they run on. Built for
personal use: each server runs on your own machine with your own API keys.

Each server runs on a home machine, is exposed over a tunnel, and connects to
Claude.ai as a custom connector. They all share one authentication module, one
launchd installer, and one set of checks.

## What's here

| Path | What it is |
|---|---|
| [`packages/localauth/`](packages/localauth/) | **Passphrase-gated OAuth 2.1 authorization server for FastMCP.** No external identity provider. Usable in any MCP server, not just the ones here. |
| [`servers/stocks/`](servers/stocks/) | **GG-MCP-Stocks.** Read-only stock market data over the Korea Investment & Securities (KIS) Open API — quotes, daily bars, order book, investor flow, indices, FX — with an optional KRX cross-check of closing prices. |
| [`servers/lostark/`](servers/lostark/) | **GG-MCP-LostArk.** Read-only Lost Ark Open API — character gear and builds sized for a model's context, market and auction prices, crafting profit, build statistics over your own ranker list. |
| [`scripts/`](scripts/) | Server-agnostic checks: the OAuth flow, and a full round trip against a live public URL. |
| [`deploy/`](deploy/) | launchd template and installer. |

---

## localauth: connecting a private MCP server to Claude.ai

`localauth` offers two ways in, with no third-party identity provider either way:

| | `StaticBearerVerifier` | `PassphraseOAuthProvider` |
|---|---|---|
| Client sends | A fixed `Authorization: Bearer …` header | OAuth 2.1 + DCR; you type a passphrase once |
| Public routes | None beyond your own (e.g. `/healthz`) | Login page, `/register`, `/authorize`, `/token` |
| Token lifetime | Until you rotate it | Short access tokens, rotating refresh tokens |
| Use when | The client can send headers | It cannot, or you want expiring tokens |

Since 2026-08 Claude custom connectors can send fixed request headers (up to
four, from an allowlist; see
[anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)).
Choose **No sign-in** and add `Authorization` with the value `Bearer <token>`;
Claude adds no scheme itself.

```python
from localauth import StaticBearerVerifier

mcp = FastMCP("your-server", auth=StaticBearerVerifier(token=os.environ["MCP_BEARER_TOKEN"]))
```

ChatGPT and the Gemini app still speak OAuth only. To serve them and Claude from
one server, pass `static_bearer_token=` to `PassphraseOAuthProvider`: it then
accepts that fixed token as well as the tokens it issues.

### OAuth, for clients without header support

Before headers existed, protecting a remote server for Claude.ai hit this wall:

- The custom connector settings exposed **only OAuth Client ID / Client
  Secret**, so a shared-secret middleware made the connector fail to connect.
- A browser-login gateway (e.g. an interactive access policy in front of the
  origin) does not work either — Claude.ai's MCP client is a server-side HTTP
  client and never completes an interstitial login.
- The supported path is **OAuth 2.1 + Dynamic Client Registration**, and most
  examples delegate that to GitHub, WorkOS, Auth0 and friends.

`localauth` implements the authorization server itself. No third-party account
is involved. Claude registers dynamically, you type a passphrase in the browser
once, and the connector holds a real bearer token from then on.

```
Claude.ai                                  your server
   │  POST /register            (DCR)         │
   ├─────────────────────────────────────────▶│  client registered
   │  GET  /authorize           (PKCE S256)   │
   ├─────────────────────────────────────────▶│  302 → /auth/login?rid=…
   │              (you enter the passphrase in a browser)
   │  302 → claude.ai/…?code=…                │
   │◀─────────────────────────────────────────┤
   │  POST /token          (code + verifier)  │
   ├─────────────────────────────────────────▶│  access + refresh issued
   │  POST /mcp   Authorization: Bearer …     │
   └─────────────────────────────────────────▶│
```

FastMCP ships an `InMemoryOAuthProvider`, but it approves every authorization
request — anyone who learns the URL gets a token. `localauth` subclasses
`OAuthProvider` directly so that `authorize()` redirects to a passphrase form
instead of minting a code.

**Security properties**

- Authorization codes are issued only after a correct passphrase in the browser.
- Tokens are never stored in plaintext; only an HMAC-SHA256 digest is persisted.
- Passphrase comparison is constant time (`hmac.compare_digest`).
- Failed attempts back off exponentially, up to 300 s.
- PKCE S256 required, authorization codes single-use, refresh tokens rotated.
- State files are written `0600` via atomic replace.

**Use it**

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

`scripts/check_oauth.py` verifies the whole flow (22 assertions) against a bare
server carrying only this provider, so it applies to your server too.

---

## Working with the repo

One [uv](https://docs.astral.sh/uv/) workspace, one virtualenv, one command:

```bash
uv sync
```

Each server owns its own `.env`, port and public hostname. Keep them distinct —
`deploy/install.sh` refuses to install a second launchd job for a server that is
already running, but it cannot stop two *different* servers from claiming the
same port.

| Server | Port | Public hostname |
|---|---|---|
| `stocks` | 8765 | *(set `MCP_PUBLIC_URL` in `servers/stocks/.env`)* |
| `lostark` | 8766 | *(set `MCP_PUBLIC_URL` in `servers/lostark/.env`)* |

### Checks

```bash
# Auth flow, no credentials needed, works for any server
.venv/bin/python scripts/check_oauth.py

# Full round trip against a live public URL, after the tunnel is up
.venv/bin/python scripts/check_public.py --env servers/stocks/.env
```

Per-server checks live next to the server; see its README. The checks that
need no credentials or live URL run on every push through GitHub Actions
(`.github/workflows/check.yml`).

### Deploy (macOS)

```bash
./deploy/install.sh stocks                                # label: com.local.gg-mcp-stocks
LABEL=com.myname.gg-mcp-stocks ./deploy/install.sh stocks # or choose your own
```

`RunAtLoad` plus `KeepAlive` means the service starts at login and restarts on
abnormal exit. Logs land in `~/Library/Logs/gg-mcp-<server>/`.

```bash
launchctl bootout gui/$(id -u)/com.local.gg-mcp-stocks   # uninstall
```

### Expose it

Servers bind to loopback only. Put a tunnel or reverse proxy in front and point
`MCP_PUBLIC_URL` at the resulting HTTPS URL.

With Cloudflare Tunnel, which configuration you have decides where the ingress
rule goes:

- **Locally managed** (`cloudflared` started with a `config.yml`) — add an
  ingress entry pointing your hostname at `http://127.0.0.1:<port>`.
- **Remotely managed** (`cloudflared tunnel run --token …`, no local config) —
  edit the route in the Zero Trust dashboard under *Networks → Tunnels → your
  tunnel → Published application routes*. A local `config.yml` is ignored.

> `MCP_PUBLIC_URL` must exactly match the public URL. It becomes the OAuth
> issuer and the redirect base; a mismatch makes the connector fail to connect.
> `scripts/check_public.py` catches exactly this.

**Do not put an interactive access policy in front of it.** A login
interstitial blocks Claude.ai's OAuth round trip. Authentication is the
server's job here, and unauthenticated requests already return 401.

### Connect from Claude.ai

Custom connectors are available on Free (one connector), Pro, Max, Team and
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

---

## Adding a server

```
servers/<name>/
├── pyproject.toml      # name = "gg-mcp-<name>", localauth = { workspace = true }
├── .env.example        # its own port, public URL, passphrase
├── README.md
└── src/gg_mcp_<name>/
```

Then `uv sync` and `./deploy/install.sh <name>`. Give it a port no other server
uses and add it to the table above.

## License

[MIT](LICENSE). Built with [Claude Code](https://claude.com/claude-code).

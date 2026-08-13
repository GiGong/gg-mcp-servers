# kis-mcp

A **read-only** MCP server over the Korea Investment & Securities (KIS) Open API.

Shared setup — the workspace, authentication, launchd, tunnelling and connecting
from Claude.ai — is documented in the [repository README](../../README.md). This
page covers what is specific to KIS.

> **Read-only by construction.** No order placement, modification, cancellation
> or transfer endpoints are implemented or exposed. Every path this server calls
> is a `/uapi/**/quotations/*` inquiry endpoint, and `KisClient.get()` rejects
> anything that does not start with `/uapi/`.

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

## Provisional vs. confirmed investor flow

`get_investor_flow` is the reason this server exists. Foreign and institutional
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

## Things the live API does that the docs do not warn you about

Found by calling the real API, and handled in code:

| Behaviour | Handling |
|---|---|
| `EGW00201` means **rate limit exceeded**, not token expiry | Retry with 1 s / 2 s backoff. Never reissue the token — doing so trips the "one token per minute" limit (`EGW00133`) and stalls every call for ~60 s. Only `EGW00121` / `EGW00123` mean the token is bad. |
| Overseas index codes are inconsistent about the leading dot | Verified: `SOX`, `SPX`, `COMP` have no dot; only `.DJI` does. `.SOX` / `.IXIC` return `rt_cd=0` with empty values rather than an error. |
| A wrong exchange code returns success with an empty body | e.g. `TSM` is NYSE, not NASDAQ. `get_overseas_quote` retries `NAS → NYS → AMS` and reports `exchange_resolved_by_fallback`. |
| Empty results are not errors | Where KIS returns a successful-but-empty payload, this server converts it into an explicit error rather than emitting nulls. |

Index aliases: `KOSPI`, `KOSDAQ`, `KOSPI200` (domestic); `SOX`, `NASDAQ`,
`SPX`, `DJI` (overseas, previous session's close). Korean aliases such as
`코스피` and `필반` also resolve.

## Rate limits and tokens

| Item | Value |
|---|---|
| Request rate | 20/s live, 5/s paper (per app key) |
| Configured throttle | `KIS_RATE_LIMIT_PER_SEC`, default 8/s — startup fails if it exceeds the KIS ceiling |
| Access-token lifetime | 24 h |
| Token issuance | Once per minute (`EGW00133` if exceeded) |
| Refresh policy | Reissued only within 15 min of expiry; cached to disk `0600` |
| Daily quota | KIS publishes no separate daily cap for inquiry APIs — only the per-second limit |

## Quote latency

- Domestic equities and indices: live.
- Overseas equities: **delayed** unless your KIS account subscribes to real-time
  overseas data. Set `KIS_OVERSEAS_REALTIME=true` to reflect that; the `delayed`
  field in every response follows this setting.
- Overseas indices: daily-chart based, i.e. **previous session's close**, not a
  live tick.
- `get_overseas_quote` also returns `krw_converted.fx_rate_today` — the rate KIS
  itself applies for KRW conversion, which is the consistent choice when
  comparing an ADR against its home-market listing.

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

## Setup

```bash
cp servers/kis/.env.example servers/kis/.env
chmod 600 servers/kis/.env
$EDITOR servers/kis/.env
```

Fill in your own KIS app key/secret (from
[KIS Developers](https://apiportal.koreainvestment.com)) and generate the auth
secrets:

```bash
python3 -c "import secrets; print('OAUTH_PASSPHRASE=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('OAUTH_SIGNING_SECRET=' + secrets.token_urlsafe(48))"
```

`KIS_ENV` switches between live (`real`) and paper (`demo`) trading domains.
The provisional investor-flow endpoint (`HHPTJ04160200`) and parts of the
overseas quote API may not be available on paper accounts.

Then, from the repository root:

```bash
uv sync
./deploy/install.sh kis
```

### Checks

```bash
# Response field mapping, 22 assertions, no network needed
.venv/bin/python servers/kis/check_parsing.py

# Live KIS calls, needs servers/kis/.env
.venv/bin/python servers/kis/smoke_test.py
```

`handoff-prompt.md` is a briefing you can paste into an analysis conversation so
the model reads the provisional/confirmed split, the units and the latency flags
correctly.

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

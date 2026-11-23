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
| `get_quote` | 주식현재가 시세 + 체결 (+ `close`, below) | `FHKST01010100` + `FHKST01010300` |
| `get_regular_close` | 국내주식 기간별시세 (unadjusted) + 당일시간대별체결 | `FHKST03010100` + `FHPST01060000` |
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

## Regular-session close vs. latest trade

Prices read on the KRX market code (J) keep changing after the 15:30 close until
20:00 as after-hours trades print, and the current day's daily-chart row follows
them until the next trading day appears. A model that reads the current price at
16:30 as "today's close" is wrong. So the close is its own object, built by the
same code in both tools:

- `get_regular_close(symbol, trade_date)` returns it for any trading day. This
  is the one to score predictions against.
- `get_quote` returns it as `close` for the latest session, next to
  `latest_trade` (the newest trade, with its own time and session).
  `price.current` is unchanged, i.e. the raw KIS value, and is never a close.

```jsonc
"close": {
  "trade_date": "2026-11-03", "venue": "KRX", "session": "REGULAR",
  "price": 1398000, "trade_time_kst": "2026-11-03T15:30:24+09:00",
  "status": "PROVISIONAL",
  "source": { "kind": "kis_closing_auction_print", "tr_id": "FHPST01060000", ... },
  "checks": [ { "source": "krx_openapi", "result": "NOT_PUBLISHED" } ],
  "independent_verified": false,
  "next_check": "Settles from the KIS daily chart once the next trading day appears there."
},
"latest_trade": {
  "price": 1396000, "as_of_kst": "2026-11-03T16:28:07+09:00",
  "session": "AFTER_HOURS", "venue": "KRX", "feed_code": "J", ...
}
```

`status` says how the price was obtained; `checks` says whether anyone else
agrees. They are separate on purpose, so a new source adds a check rather than a
new status.

| `status` | Meaning | For scoring |
|---|---|---|
| `FINAL` | Unadjusted KIS daily-chart close, read once a later trading day exists in the chart (before that, the row can still move) | Score finally, unless a check says `MISMATCH` |
| `PROVISIONAL` | Today's closing auction print, accepted only if nothing traded in the auction window, the tick page reaches back past it, and everything after the close printed at one price | Provisional score only |
| `PENDING` | Not available yet; `next_check` says what to wait for | Ask again later |
| `NO_TRADE` | Settled day with zero volume; the official price is kept | Nothing to score |
| `NOT_TRADING_DAY` | Exchange closed that day | Nothing to score |
| `ERROR` | `error.retryable` says whether to try again | Report it |

Prices are unadjusted so a later split or rights issue cannot rewrite a close
that was already scored. Verified on the live API: finished days' J daily close
matched the 15:30 print, the next day's base price and KRX's official close on
every day checked (2026-10-30, 11-02, 11-03, 11-16, 11-17).

The tick time carries no date, so `latest_trade.as_of_kst` is filled only when
today's daily row exists with volume (proof that the newest trade is today's).
Otherwise it stays `null` with the raw `trade_time_raw`. After-hours trades on
the J feed are KRX's own after-hours sessions: KRX's official daily volume equals
the J feed's final volume including them (checked 2026-11-03, 11-17, 11-18).
A trade in the ten minutes after the close that is not the identified closing
print is `UNKNOWN`.

`price.prev_close` is a deprecated alias of `price.base_price`. It always was
the exchange base price (`stck_sdpr`), which differs from the previous close on
ex-rights, ex-dividend and listing days.

### KRX cross-check (optional)

KRX publishes each day's official close through the
[KRX Open API](https://openapi.krx.co.kr), but only the next day, so it can
confirm a close and never supply one. Everything works without it.

To turn it on, set `KRX_AUTH_KEY` and apply for **유가증권 일별매매정보**
(and **코스닥 일별매매정보** for KOSDAQ symbols) on the KRX site; each dataset
is approved separately. The check then appears in `close.checks`:

| `result` | Meaning |
|---|---|
| `MATCH` / `MISMATCH` | KRX's close (`krx_price`) agrees / disagrees. Close price only, not volume: KRX's volume includes after-hours trades |
| `NOT_CONFIGURED` | No `KRX_AUTH_KEY` |
| `NOT_PUBLISHED` | KRX has not published that day yet (asked again after 10 minutes) |
| `NOT_AUTHORIZED` | The key works but the symbol's dataset is not approved |
| `UNSUPPORTED` | Not in any approved dataset (e.g. ETFs) |
| `ERROR` | Network failure or a rejected key |

`independent_verified` is true only when a non-KIS source matches and none
disagrees. One call returns a whole market for a day and is cached, so the
10,000-calls-per-day limit is not a concern.

## Known limitations

- **Days with shifted hours are listed by hand.** `SPECIAL_HOURS` in
  `market.py` holds only the 2026-11-19 college entrance exam day. Add each
  year's exam day and any late-opening first trading day yourself; until then
  those days are judged on normal hours. The KIS holiday endpoint reports only
  open or closed.
- **Provisional flow publication times are known for normal days only.** On a
  shifted-hours day `approx_kst_time` is null; on 2026-11-19 slots appeared
  roughly an hour later than usual, give or take fifteen minutes.
- **Confirmed flow revisions are flagged, not tracked.** The newest day's
  figures have been seen to change by the next morning (2026-11-03, 11-18) and
  are marked `preliminary`; the server does not store the earlier values or
  report the difference.
- **KRX checks cover KOSPI and KOSDAQ shares.** ETFs and other products report
  `UNSUPPORTED`.

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
- **After the regular close (15:30 KST), the price is not the close.** Prices read
  on the KRX market code (J) keep changing until 20:00 as after-hours trades
  print, and KIS folds them into the current price, change, volume and the
  current day's daily close. Verified against the
  live API: on 2026-11-03 SK hynix closed the regular session at 1,398,000, yet
  `stck_prpr` read 1,396,000 at 16:28 and 1,399,000 at 16:45. In the
  `after_hours` and `closed` phases, `market_status.note` says so. For the close,
  see [Regular-session close vs. latest trade](#regular-session-close-vs-latest-trade).
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
using the KIS holiday endpoint rather than a hardcoded calendar. That endpoint
reports only whether a day trades, not its hours, so days with shifted hours
(such as the college entrance exam day) are listed by hand in `SPECIAL_HOURS` in
`market.py`.

---

## Setup

```bash
cp servers/kis/.env.example servers/kis/.env
chmod 600 servers/kis/.env
$EDITOR servers/kis/.env
```

Fill in your own KIS app key/secret (from
[KIS Developers](https://apiportal.koreainvestment.com)) and generate the auth
secrets. The example sets `AUTH_MODE=both`: Claude connects with a fixed
`Authorization: Bearer <MCP_BEARER_TOKEN>` header, and clients that only speak
OAuth (ChatGPT, the Gemini app) with the passphrase. `bearer` or `oauth` keeps
just one of the two; `.env.example` explains each.

```bash
python3 -c "import secrets; print('MCP_BEARER_TOKEN=' + secrets.token_urlsafe(32))"
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
# Response field mapping and close logic, no network needed
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

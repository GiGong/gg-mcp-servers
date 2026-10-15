# lostark-mcp

A **read-only** MCP server over the official
[Lost Ark Open API](https://developer-lostark.game.onstove.com) (Korean service).
It is built for talking about a character with a model: where to grow next,
which build others run, what an upgrade costs on the auction house, and whether
crafting pays.

Shared setup — the workspace, authentication, launchd, tunnelling and connecting
from Claude.ai — is documented in the [repository README](../../README.md). This
page covers what is specific to Lost Ark.

> **Read-only.** The Open API offers nothing that changes game state, and the
> client only allows the inquiry paths listed below. The only thing this server
> writes is a local snapshot database, and only the `collect` command writes it.

---

## Tools

Paths and models were taken from the developer portal's Swagger documents
(`/swagger-doc/endpoints/{category}`) and its changelog (V10.0.0), then checked
against live responses — not from memory.

| Tool | API | What it returns |
|---|---|---|
| `character_overview` | `GET /armories/characters/{name}` | One-screen summary, about 2 KB |
| `character_detail` | same call, from cache | Chosen sections at `brief` / `standard` / `full` depth |
| `character_roster` | `GET /characters/{name}/siblings` | Every character on the account |
| `market_search` | `POST /markets/items` | Market listings with per-unit prices |
| `market_prices` | `POST /markets/items` | Prices by exact name, or a saved preset |
| `market_history` | `GET /markets/items/{id}` | 14 days of daily averages, with a summary |
| `auction_search` | `POST /auctions/items` | Listings, with option filters by name |
| `auction_gem_board` | `POST /auctions/items` | Lowest buyout per gem level and kind |
| `auction_options` | `GET /auctions/options` | Categories, option names and values, skills |
| `calc_craft_profit` | market prices | Crafting vs. buying vs. selling the materials |
| `stats_builds` | local snapshots | Build statistics over your ranker list |

Not wrapped: news, events, alarms and the content calendar (out of scope), and
`POST /markets/trades`, which returns the same rows as `/markets/items` minus the
current price. The API has no guild, ranking or character-search endpoint.

## Controlling context size

One full armory response is about 500 KB, nearly all tooltip markup (skills
130 KB, ark grid 90 KB, gems 80 KB, gear 60 KB). Passing it through is not an
option, so the character is fetched once, cached, and every view is cut from
that copy:

- `character_overview` — about 2 KB.
- `character_detail(sections=[...], detail=..., match=[...])` — choose sections
  finer than the API's own (`equipment`, `accessories`, `bracelet`,
  `ability_stone`, ...), filter by slot or name, and pick the depth:
  - `brief`: one line per item
  - `standard`: parsed fields (refine, advanced refine, quality, polish effects,
    ark passive points, stone engravings with penalty flag, tripods, runes,
    gem targets, ark grid options with active flags)
  - `full`: standard plus the tooltip as plain-text lines
- Drill-down calls reuse the cached character and cost **no quota**.
- Responses over 40 000 characters fall back from `full` to `standard`, then
  trim lists, and say what was cut.

## Cache and refresh

| Data | TTL |
|---|---|
| Character | 3 min |
| Roster | 5 min |
| Market search | 1 min |
| Market history | 10 min |
| Auction search | 30 s |
| Gem board | 2 min |
| Option catalogs | 24 h |

Every tool takes `refresh=true`. A refresh within 10 s of the last fetch is
served from cache and flagged `refresh_suppressed`. Every response carries
`cache.age_sec` and most carry `quota.remaining`.

## Things the live API does that the docs do not warn you about

Found by calling the real API (2026-10-13), and handled in code:

| Behaviour | Handling |
|---|---|
| A missing character returns **200** with `null` (siblings: `[]`), not 404 | Converted to `kind: not_found` |
| Searches without `CategoryCode` return 400 with an empty body | Categories resolved by name; name-only look-ups try likely categories in turn |
| Tooltip `Element_NNN` numbering differs per item, slots can be null, and one field can be `'106000'` on one item and `63000` on the next | Parsed by element type and label text, never by number; every number coerced |
| Markup appears outside tooltips too (gem names) | Every string is cleaned |
| `CombatPower`, `ItemAvgLevel` are strings like `"1,726.66"`; roster and profile item levels can differ by 0.01 | Parsed to numbers; profile is authoritative |
| Market prices and history averages are **per bundle** (`BundleCount` 1 or 100) | `unit_price` / `unit_avg_price` added |
| Empty result pages return `Items: null` | Treated as empty |
| Auction `BuyPrice` is null on bid-only listings | Hidden by default (`buyout_only`), and counted |
| Auction `Level` on gems is the equip level (1640); the gem level is only in the name | Parsed from the name |
| Option values are codes (0.40% is sent as `40`) | Display values converted via the catalog's value table |
| An option's `MinValue` or `MaxValue` **alone is ignored**; the filter applies only when both are sent | The open side is filled with the widest accepted value |
| Some polish options apply to one slot only (공격력 % is earring-only) | Rejected with the slots it does apply to |
| Ability stone penalty engravings are distinguished only by colour (`#FE2E2E`) | Read from the raw markup |
| Skill tooltips end with a "next level" preview | Cut |
| Timestamps are KST without an offset | `+09:00` attached |
| 광휘 gems are character-bound and never listed | Gem board uses 겁화 / 작열 |

Not yet observed: the response body during maintenance (documented as 503;
handled as `kind: maintenance` with a 60 s pause), and how soon an in-game change
reaches the armory.

## Rate limit

| Item | Value |
|---|---|
| API quota | 100 requests per minute per key (`X-RateLimit-*` headers) |
| Server throttle | `LOSTARK_RATE_LIMIT_PER_MIN`, default 90; startup fails above 100 |
| 429 handling | Waits for the reset if it is within `LOSTARK_MAX_WAIT_SEC`, else returns `rate_limited` with `retry_after_sec` |
| Collector | Its own 50/min share, and pauses while the API reports 30 or fewer left, so a running server keeps headroom |

The throttle combines a local sliding window with the server's own counter, so
other processes spending the same key are accounted for.

## Ranker statistics

There is no ranking API, so statistics come from names you list yourself:

1. Put character names in `servers/lostark/local/rankers.txt`, one per line
   (`#` starts a comment). `local/` is gitignored.
2. Run the collector. It snapshots each character's parsed sections into
   `local/snapshots.sqlite3`, skipping names collected in the last 20 hours:

   ```bash
   .venv/bin/lostark-mcp collect             # all due names
   .venv/bin/lostark-mcp collect --dry-run   # just report what is due
   ```

3. Ask through `stats_builds(character_class=..., aspect=...)`. Aspects:
   `summary`, `build`, `arkpassive`, `engravings`, `skills`, `gems`, `stats`,
   `accessories`, `bracelet`, `cards`, `arkgrid`, `equipment`. Every response
   reports the sample size and snapshot age.

## Game data the API does not have

Recipes, crafting fees and the market sale fee live in `data/`:

| File | Holds | State |
|---|---|---|
| `data/market_presets.toml` | Saved market searches (category codes from the catalog) | Ready |
| `data/recipes.toml` | Crafting recipes | **Empty on purpose** — copy from the in-game crafting window |
| `data/fees.toml` | Market sale fee | 5%, **unverified** |

`calc_craft_profit` also accepts every value inline and reports which numbers
were assumed or unverified.

---

## Setup

```bash
cp servers/lostark/.env.example servers/lostark/.env
chmod 600 servers/lostark/.env
$EDITOR servers/lostark/.env
```

Fill in your JWT and generate the connector token (`AUTH_MODE=bearer`, the
default in the example):

```bash
python3 -c "import secrets; print('MCP_BEARER_TOKEN=' + secrets.token_urlsafe(32))"
```

In Claude, add a custom connector with the URL `https://<your-host>/mcp`, choose
**No sign-in**, and add the request header `Authorization` with the value
`Bearer <MCP_BEARER_TOKEN>`. Register the exact `/mcp` URL: a redirect to another
host drops the header.

For clients that cannot send headers, set `AUTH_MODE=oauth` and generate
`OAUTH_PASSPHRASE` (`token_urlsafe(32)`) and `OAUTH_SIGNING_SECRET`
(`token_urlsafe(48)`) instead; you then type the passphrase in a browser once.

Then, from the repository root:

```bash
uv sync
./deploy/install.sh lostark
```

### Checks

```bash
# Parsing and arithmetic against masked fixtures, no network
.venv/bin/python servers/lostark/check_parsing.py

# Every tool against the live API (about 15 requests)
.venv/bin/python servers/lostark/smoke_test.py <character name>
```

`fixtures/` holds recorded responses with character, guild, server and estate
names masked and large sections trimmed.

`handoff-prompt.md` is a briefing you can paste into a conversation so the
model reads bundles, unverified game data and sample sizes correctly.

---

## Disclaimer

- **Unofficial.** Not affiliated with, endorsed by, or supported by Smilegate.
  Lost Ark is a trademark of Smilegate.
- **You supply your own key.** No API key is contained in or distributed with
  this repository. You are responsible for keeping it safe and for complying
  with the Lost Ark Open API terms of use.
- **No game data is redistributed.** This is a client wrapper; data comes from
  your own key at request time. The fixtures are trimmed, masked samples for
  tests.
- Provided as-is under the MIT License, with no warranty as to accuracy or
  timeliness.

# kis-mcp

한국투자증권(KIS) Open API 를 감싼 **조회 전용** 원격 MCP 서버.
Streamable HTTP 로 서비스하고, Cloudflare Tunnel 로 노출해 Claude.ai 커스텀 커넥터에 연결한다.

> **읽기 전용.** 주문 실행·정정·취소·이체 계열 KIS API 는 구현되어 있지 않으며 MCP 도구로도
> 노출하지 않는다. 서버가 호출하는 경로는 전부 `/uapi/**/quotations/*` 조회 계열이고,
> `KisClient.get()` 이 `/uapi/` 접두사를 강제 검증한다.

---

## 1. 도구

| 도구 | KIS API | tr_id | 비고 |
|---|---|---|---|
| `get_quote` | 주식현재가 시세 | `FHKST01010100` | 현재가·등락률·OHLC·거래량·시총·PER/PBR |
| `get_investor_flow` | 종목별 외인기관 추정가집계 + 주식현재가 투자자 | `HHPTJ04160200` + `FHKST01010900` | **잠정/확정 분리 반환** |
| `get_daily_ohlcv` | 국내주식 기간별시세 | `FHKST03010100` | 기본 30거래일, 최대 100 |
| `get_orderbook` | 주식현재가 호가/예상체결 | `FHKST01010200` | 10단계 + 총잔량 + 매수/매도 비율 |
| `get_index` | 국내업종 현재지수 / 해외 기간별시세 | `FHPUP02100000` / `FHKST03030100` | KOSPI·KOSDAQ·KOSPI200, SOX·NASDAQ·SPX·DJI |
| `get_overseas_quote` | 해외주식 현재가상세 | `HHDFS76200200` | **원환산 가격 + 적용 환율 동시 반환** |
| `get_fx` | 해외 기간별시세(환율 모드) | `FHKST03030100` | 실패 시 원환산 적용환율로 대체 |
| `get_market_status` | 국내휴장일조회 | `CTCA0903R` | 정규장/장마감/휴장 판정 |

모든 응답에 `as_of_kst`(한국시간 ISO8601)와 `market_status`가 포함된다.

### `get_investor_flow` 의 잠정/확정 구분

```jsonc
{
  "provisional": {          // 장중 추정 가집계 — 증권사 집계 입력값
    "is_confirmed": false,
    "latest": { "slot": "1", "approx_kst_time": "09:30", "foreign_net_qty": 236000 },
    "snapshots": [ ... ]    // 외국인 09:30/11:20/13:20/14:30, 기관 10:00/11:20/13:20/14:30
  },
  "confirmed": {            // 거래소 확정 일별 순매수
    "is_confirmed": true,
    "latest": { "date": "2026-08-12", "foreign_net_qty": 559854,
                "institution_net_qty": 193626, "is_published": true },
    "pending_dates": ["2026-08-13"],   // 아직 확정치가 안 나온 날
    "series": [ ... ]                  // 각 행에 is_published 플래그
  }
}
```

**실호출로 확인한 중요한 동작**: 확정치 API(`FHKST01010900`)는 **당일 행을 빈 문자열로**
돌려준다. 확정 수급은 장 마감 후에 채워진다. 따라서 `confirmed.latest` 는
"확정치가 나온 가장 최근 거래일"이고, 미발표 일자는 `pending_dates` 로 분리해 노출한다.
장중 당일 수급은 `provisional` 을 봐야 한다 — 이 도구의 존재 이유가 여기에 있다.

순매수 **수량 단위는 주**, `*_value_krw_mn` 는 **백만원** 단위다 (KIS `*_tr_pbmn` 원본 단위).

두 API 는 독립적으로 호출되므로 **한쪽이 실패해도 다른 쪽은 정상 반환**된다.
실패한 쪽에는 `available: false` 와 KIS 원본 에러코드가 담긴다.

### 해외 지수 코드 (실호출로 확인)

접두 `.` 유무가 지수마다 다르므로 추측하면 안 된다. 확인된 값:

| 별칭 | KIS 코드 | 확인된 이름 |
|---|---|---|
| `SOX`, `필반` | `SOX` | 필라델피아 반도체지수 |
| `NASDAQ`, `COMP` | `COMP` | 나스닥 종합 |
| `SPX`, `S&P500` | `SPX` | S&P500 |
| `DJI`, `다우` | `.DJI` | 다우존스 산업지수 |

`.SOX`, `.IXIC`, `.SPX`, `SOXX` 는 **오류 없이 빈 값**을 돌려준다. 서버는 이 경우를
감지해 명시적 오류로 바꿔 반환한다.

### 해외 거래소 자동 판별

KIS 는 거래소 코드가 틀려도 `rt_cd=0` 에 **빈 output** 을 준다 (예: TSM 을 `NAS` 로 조회).
`get_overseas_quote` 는 값이 비면 `NAS → NYS → AMS` 순으로 재시도하고,
폴백으로 찾은 경우 `exchange_resolved_by_fallback: true` 를 붙인다.

---

## 2. 설치

```bash
cd /path/to/kis-mcp

# uv 관리 CPython 3.13 기반 venv (Homebrew python@3.13 은 pyexpat 이 깨져 있어 사용 불가)
uv venv --managed-python --python 3.13 .venv
uv pip install --python .venv/bin/python -e .

cp .env.example .env
chmod 600 .env
$EDITOR .env      # KIS_APP_KEY / KIS_APP_SECRET / OAUTH_* / MCP_PUBLIC_URL
```

시크릿 생성:

```bash
python3 -c "import secrets; print('OAUTH_PASSPHRASE=' + secrets.token_urlsafe(32))"
python3 -c "import secrets; print('OAUTH_SIGNING_SECRET=' + secrets.token_urlsafe(48))"
```

### 검증

```bash
# 인증 플로우 자체 점검 (KIS 자격증명 불필요)
PYTHONPATH=src .venv/bin/python scripts/check_oauth.py

# 응답 필드 매핑 회귀 테스트 (네트워크 불필요)
PYTHONPATH=src .venv/bin/python scripts/check_parsing.py

# KIS 실호출 (.env 필요)
PYTHONPATH=src .venv/bin/python scripts/smoke_test.py

# 공개 URL 에서 Claude.ai 와 동일한 OAuth 왕복 완주 (터널 구성 후)
PYTHONPATH=src .venv/bin/python scripts/check_public.py
#   로컬 리졸버가 호스트명을 못 찾을 때는 DNS 만 우회한다 (전송은 진짜 터널 경유)
PYTHONPATH=src .venv/bin/python scripts/check_public.py --resolve 104.21.91.26
```

---

## 3. 인증 — 왜 Bearer 미들웨어가 아닌가

Claude.ai 커스텀 커넥터의 고급 설정에는 **OAuth Client ID / Client Secret 칸만** 있다.
임의의 `Authorization: Bearer` 헤더를 넣을 방법이 없으므로, 정적 Bearer 토큰 미들웨어를
붙이면 커넥터 연결 자체가 실패한다. Cloudflare Access 의 브라우저 로그인 정책도 마찬가지로
Claude.ai 의 서버 사이드 HTTP 클라이언트는 통과하지 못한다.

따라서 이 서버는 **자체 OAuth 2.1 인가서버**를 내장한다 (`src/localauth/`).

```
Claude.ai                          kis-mcp
   │  POST /register (DCR)            │
   ├─────────────────────────────────▶│  클라이언트 등록
   │  GET /authorize (PKCE S256)      │
   ├─────────────────────────────────▶│  302 → /auth/login?rid=...
   │            (브라우저에서 패스프레이즈 입력)
   │  302 → claude.ai/...?code=...    │
   │◀─────────────────────────────────┤
   │  POST /token (code + verifier)   │
   ├─────────────────────────────────▶│  access + refresh 발급
   │  POST /mcp  Authorization: Bearer │
   └─────────────────────────────────▶│
```

보안 속성:

- 인가 코드는 **브라우저에서 패스프레이즈를 맞게 입력해야만** 발급된다.
- 액세스/리프레시 토큰은 평문 저장하지 않는다. `OAUTH_SIGNING_SECRET` 을 키로 한
  HMAC-SHA256 해시만 디스크에 남는다.
- 패스프레이즈 비교는 `hmac.compare_digest` 상수 시간 비교.
- 실패 시도는 지수 백오프(최대 300초)로 제한.
- PKCE S256 강제, 인가 코드 1회용, 리프레시 토큰 회전.
- 상태 파일은 `0600`.

### 다른 로컬 MCP 서버에서 재사용

`src/localauth/` 는 kis-mcp 에 의존하지 않는다. 그대로 복사하거나 경로를 추가해서 쓴다.

```python
from localauth import PassphraseOAuthProvider

auth = PassphraseOAuthProvider(
    base_url="https://other-mcp.example.com",
    passphrase=os.environ["OAUTH_PASSPHRASE"],
    signing_secret=os.environ["OAUTH_SIGNING_SECRET"],
    store_path=Path("~/.cache/other-mcp/oauth.json").expanduser(),
    server_label="Other MCP",
)
mcp = FastMCP("other-mcp", auth=auth)
```

---

## 4. launchd 등록

```bash
mkdir -p ~/Library/Logs/kis-mcp
cp deploy/com.example.kis-mcp.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.kis-mcp.plist
launchctl enable gui/$(id -u)/com.example.kis-mcp
launchctl kickstart -k gui/$(id -u)/com.example.kis-mcp

# 확인
launchctl print gui/$(id -u)/com.example.kis-mcp | head -20
curl -s http://127.0.0.1:8765/healthz
```

`RunAtLoad` + `KeepAlive`(비정상 종료 시 재시작)로 **재부팅 후 자동 복구**된다.

로그: `~/Library/Logs/kis-mcp/` (stdout/stderr + 애플리케이션 로그 5MB×5 로테이션).
자격증명·계좌번호는 `logging_setup.RedactingFilter` 가 마스킹한다.

해제:

```bash
launchctl bootout gui/$(id -u)/com.example.kis-mcp
```

---

## 5. Cloudflare Tunnel

이 맥미니의 터널은 **원격 관리형(remotely-managed)** 이다.
`/Library/LaunchDaemons/com.cloudflare.cloudflared.plist` 가 `cloudflared tunnel run --token …`
으로 기동하고 있으며 로컬 `config.yml` 이 없다. 따라서 **ingress 는 Zero Trust 대시보드에서
편집**한다. (로컬 파일을 만들어도 반영되지 않는다.)

1. <https://one.dash.cloudflare.com> → **Networks → Tunnels**
2. 이 맥미니의 터널 선택 → **Configure → Published application routes**
3. **Add a published application route**
   - Subdomain: `mcp`
   - Domain: `mcp.example.com`
   - Type: `HTTP`
   - URL: `127.0.0.1:8765`
4. 저장 후 `.env` 의 `MCP_PUBLIC_URL` 을 `https://mcp.example.com` 로 맞추고 서비스 재시작.

> `MCP_PUBLIC_URL` 이 실제 공개 URL 과 다르면 OAuth 메타데이터의 issuer 와 리다이렉트가
> 어긋나 커넥터 연결이 실패한다. 정확히 일치시킬 것.

**Cloudflare Access 정책은 걸지 말 것.** 로그인 인터스티셜이 Claude.ai 의 OAuth 왕복을
가로막는다. 인증은 서버 내장 OAuth 가 담당한다.

터널 토큰이 plist 에 평문으로 저장되어 있다. 유출이 우려되면 대시보드에서 로테이트한다.

---

## 6. Claude.ai 커스텀 커넥터 연결

Pro / Max / Team / Enterprise 및 Free(1개 제한) 플랜에서 사용 가능하다.

**개인(Pro/Max)**

1. Claude.ai → **Customize → Connectors**
2. **+** → **Add custom connector**
3. URL: `https://mcp.example.com/mcp`
4. **Add** — Advanced settings 의 OAuth Client ID/Secret 은 **비워 둔다**
   (서버가 동적 클라이언트 등록을 지원하므로 자동 등록된다)
5. **Connect** 클릭 → 브라우저에 패스프레이즈 화면 → `OAUTH_PASSPHRASE` 입력
6. 승인되면 커넥터가 **Connected** 로 바뀌고 도구 8개가 노출된다

**Team / Enterprise (관리자)**

1. **Organization settings → Connectors → Add**
2. **Custom** 위에 마우스를 올리고 **Web** 선택
3. URL 입력 → **Add**
4. 구성원은 **Customize → Connectors** 에서 **Connect**

### 연결 확인용 테스트 질의

1. **"SK하이닉스 현재가와 오늘 외국인 순매수 알려줘"**
   → `get_quote(000660)` + `get_investor_flow(000660)`. 잠정/확정 구분이 응답에 드러나는지 확인.
2. **"SK하이닉스 최근 30거래일 일봉에서 종가 기준 최고·최저와 그 날짜를 뽑아줘"**
   → `get_daily_ohlcv(000660, 30)`. 정렬과 날짜 범위 확인.
3. **"SKHY ADR 가격에 원달러 환율 적용해서 SK하이닉스 본주 대비 괴리율 계산해줘"**
   → `get_overseas_quote(SKHY)` + `get_quote(000660)`. `krw_converted.fx_rate_today` 로
   한 번에 계산되는지 확인.

---

## 7. 운영 메모

**KIS 유량·토큰**

| 항목 | 값 |
|---|---|
| 유량 한도 | 실전 20건/초, 모의 5건/초 (앱키 단위) |
| 설정 스로틀 | `KIS_RATE_LIMIT_PER_SEC` (기본 8/초) — 한도 초과 설정 시 기동 거부 |
| 접근토큰 유효기간 | 24시간 |
| 토큰 재발급 제한 | 1분 1회 (초과 시 `EGW00133`) |
| 갱신 정책 | 만료 15분 전에만 재발급, 파일 캐시(`0600`) |
| 일일 총 호출 한도 | KIS 가 조회계 API 에 대해 공표한 별도 일일 상한 없음 (초당 제한만 존재) |

**KIS 에러코드 처리**

| 코드 | 의미 | 서버 동작 |
|---|---|---|
| `EGW00121` / `EGW00123` | 토큰 무효 / 만료 | 토큰 재발급 후 재시도 |
| `EGW00133` | 토큰 발급 1분 1회 초과 | 남은 시간만큼 대기 후 발급 |
| `EGW00201` | **호출 유량 초과** | 1초·2초 백오프 후 재시도 (**토큰 재발급 안 함**) |

> `EGW00201` 을 토큰 만료로 오인하면 불필요한 토큰 재발급 → 1분 대기 루프에 빠진다.
> 초기 구현에서 실제로 발생했고 실호출 검증에서 잡아 수정했다.

**시세 지연**

- 국내주식/지수: KIS REST 조회 API 기준 실시간(체결 반영).
- 해외주식: 기본 **지연 시세**(미국 통상 15분). KIS 실시간 시세를 신청했다면
  `.env` 의 `KIS_OVERSEAS_REALTIME=true` 로 바꾼다. 응답의 `delayed` 필드에 반영된다.
- 해외지수(SOX 등): 일별 차트 API 기반이라 **직전 정규장 종가** 기준. 장중 틱이 아니다.
- 환율: 일별 시세 기준. `get_overseas_quote` 의 `krw_converted.fx_rate_today` 는
  KIS 가 원화 환산에 실제 적용하는 환율이라 ADR 괴리율 계산에는 이쪽이 정합적이다.

**에러 처리**

KIS 가 `rt_cd != "0"` 을 반환하면 `KisError` 로 올라와 도구 결과에 그대로 담긴다.

```json
{ "error": true, "kis_rt_cd": "1", "kis_msg_cd": "...", "kis_msg1": "...",
  "tr_id": "...", "api_path": "..." }
```

빈 값으로 뭉개지 않는다. 휴장일·장마감은 `market_status.phase` 와 `note` 로 드러난다.

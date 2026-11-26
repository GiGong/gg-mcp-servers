# GG-MCP-Servers

[English](README.md) | 한국어

> 영어 README를 옮긴 문서입니다. 두 문서의 내용이 다르면 영어판을 따릅니다.

직접 운영하는 원격 MCP 서버와, 그 서버들이 함께 쓰는 부품을 모은 저장소입니다.
개인 사용 용도로 만들었습니다. 각 서버는 여러분의 컴퓨터에서, 여러분의 API 키로
돌아갑니다.

각 서버는 집에 있는 컴퓨터에서 실행되고, 터널로 외부에 공개되며, Claude.ai에
커스텀 커넥터로 연결됩니다. 인증 모듈 하나, launchd 설치 스크립트 하나, 점검
스크립트 한 묶음을 모든 서버가 함께 씁니다.

## 구성

| 경로 | 내용 |
|---|---|
| [`packages/localauth/`](packages/localauth/) | **FastMCP용 패스프레이즈 기반 OAuth 2.1 인가 서버.** 외부 ID 제공자(IdP)가 필요 없습니다. 여기 있는 서버뿐 아니라 어떤 MCP 서버에도 쓸 수 있습니다. |
| [`servers/stocks/`](servers/stocks/) | **GG-MCP-Stocks.** 한국투자증권(KIS) Open API로 주식 시장 데이터를 읽기 전용으로 조회합니다. 현재가, 일봉, 호가, 투자자별 매매동향, 지수, 환율을 다루고, 원하면 KRX 데이터로 종가를 교차 검증합니다. |
| [`servers/lostark/`](servers/lostark/) | **GG-MCP-LostArk.** 로스트아크 Open API를 읽기 전용으로 조회합니다. 모델 컨텍스트 크기에 맞춘 캐릭터 장비와 빌드, 거래소와 경매장 시세, 제작 손익, 직접 고른 랭커 목록으로 만든 빌드 통계를 제공합니다. |
| [`scripts/`](scripts/) | 서버와 상관없이 쓰는 점검: OAuth 흐름, 실제 공개 URL을 거치는 전체 왕복. |
| [`deploy/`](deploy/) | launchd 템플릿과 설치 스크립트. |

---

## localauth: 비공개 MCP 서버를 Claude.ai에 연결하기

`localauth`는 두 가지 연결 방식을 제공합니다. 어느 쪽도 외부 ID 제공자를 쓰지
않습니다.

| | `StaticBearerVerifier` | `PassphraseOAuthProvider` |
|---|---|---|
| 클라이언트가 보내는 것 | 고정된 `Authorization: Bearer …` 헤더 | OAuth 2.1 + DCR. 패스프레이즈를 한 번 입력 |
| 공개되는 경로 | 직접 만든 경로 외에는 없음 (예: `/healthz`) | 로그인 페이지, `/register`, `/authorize`, `/token` |
| 토큰 유효 기간 | 직접 교체할 때까지 | 짧은 액세스 토큰, 교체 발급되는 리프레시 토큰 |
| 언제 쓰나 | 클라이언트가 헤더를 보낼 수 있을 때 | 보낼 수 없거나, 만료되는 토큰을 원할 때 |

2026년 8월부터 Claude 커스텀 커넥터가 고정 요청 헤더를 보낼 수 있습니다(허용
목록 안에서 최대 4개,
[anthropics/claude-ai-mcp#112](https://github.com/anthropics/claude-ai-mcp/issues/112)
참고). **No sign-in**을 고르고 `Authorization` 헤더에 `Bearer <token>` 값을
넣으세요. Claude가 스킴을 따로 붙이지 않습니다.

```python
from localauth import StaticBearerVerifier

mcp = FastMCP("your-server", auth=StaticBearerVerifier(token=os.environ["MCP_BEARER_TOKEN"]))
```

ChatGPT와 Gemini 앱은 아직 OAuth만 지원합니다. 이들과 Claude를 한 서버에서 함께
받으려면 `PassphraseOAuthProvider`에 `static_bearer_token=`을 넘기세요. 그러면
자기가 발급한 토큰과 함께 이 고정 토큰도 받습니다.

### 헤더를 못 보내는 클라이언트를 위한 OAuth

헤더 기능이 생기기 전에는, Claude.ai용 원격 서버를 보호하려 하면 이런 벽에
부딪혔습니다.

- 커스텀 커넥터 설정에 **OAuth Client ID / Client Secret만** 있어서, 공유
  비밀값을 확인하는 미들웨어를 두면 커넥터가 연결되지 않았습니다.
- 브라우저 로그인 게이트웨이(예: 원본 서버 앞의 대화형 접근 정책)도 통하지
  않습니다. Claude.ai의 MCP 클라이언트는 서버에서 도는 HTTP 클라이언트라서 중간
  로그인 화면을 통과하지 못합니다.
- 공식 지원 경로는 **OAuth 2.1 + 동적 클라이언트 등록(DCR)**이고, 대부분의
  예제는 이를 GitHub, WorkOS, Auth0 같은 서비스에 맡깁니다.

`localauth`는 인가 서버를 직접 구현합니다. 외부 계정이 전혀 필요 없습니다.
Claude가 동적으로 등록하고, 여러분이 브라우저에서 패스프레이즈를 한 번 입력하면,
그때부터 커넥터가 실제 bearer 토큰을 갖고 접속합니다.

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

FastMCP에는 `InMemoryOAuthProvider`가 들어 있지만, 모든 인가 요청을 승인합니다.
URL만 알면 누구나 토큰을 받습니다. `localauth`는 `OAuthProvider`를 직접 상속해서,
`authorize()`가 코드를 발급하는 대신 패스프레이즈 입력 화면으로 보내게 합니다.

**보안 특성**

- 브라우저에서 패스프레이즈를 맞게 입력해야만 인가 코드가 발급됩니다.
- 토큰은 평문으로 저장하지 않고, HMAC-SHA256 다이제스트만 남깁니다.
- 패스프레이즈는 상수 시간으로 비교합니다(`hmac.compare_digest`).
- 실패할수록 대기 시간이 지수적으로 늘어나며, 최대 300초입니다.
- PKCE S256 필수, 인가 코드는 한 번만 사용, 리프레시 토큰은 교체 발급합니다.
- 상태 파일은 원자적 교체로 `0600` 권한을 주어 씁니다.

**설치**

`localauth`는 PyPI에 올라가 있지 않습니다. 이 저장소에서 바로 설치하세요.

```bash
uv add "localauth @ git+https://github.com/GiGong/gg-mcp-servers#subdirectory=packages/localauth"
pip install "localauth @ git+https://github.com/GiGong/gg-mcp-servers#subdirectory=packages/localauth"
```

버전을 고정하려면 `#subdirectory` 앞에 `@<commit>`을 붙이세요.

개인 프로젝트입니다. OAuth 흐름 점검과 공격 점검(`scripts/check_oauth.py`,
`scripts/check_security.py`)이 push마다 돌지만, 외부 보안 검토는 받지 않았습니다.
중요한 서비스 앞에 두기 전에 코드를 직접 읽어 보세요.

**사용법**

```python
from pathlib import Path
from fastmcp import FastMCP
from localauth import PassphraseOAuthProvider

auth = PassphraseOAuthProvider(
    base_url="https://your-server.example.com",   # 공개 URL과 같아야 함
    passphrase=os.environ["OAUTH_PASSPHRASE"],
    signing_secret=os.environ["OAUTH_SIGNING_SECRET"],
    store_path=Path("~/.cache/your-server/oauth.json").expanduser(),
    server_label="Your service",                  # 로그인 페이지에 표시됨
)

mcp = FastMCP("your-server", auth=auth)
mcp.run(transport="http", host="127.0.0.1", port=8765, path="/mcp")
```

`scripts/check_oauth.py`는 이 프로바이더만 붙인 빈 서버를 상대로 전체 흐름(22개
검증)을 확인합니다. 그래서 여러분의 서버에도 그대로 적용됩니다.

---

## 저장소 사용법

[uv](https://docs.astral.sh/uv/) 워크스페이스 하나, 가상환경 하나, 명령 하나로
끝납니다.

```bash
uv sync
```

서버마다 자기 `.env`, 포트, 공개 호스트 이름을 따로 갖습니다. 서로 겹치지 않게
하세요. `deploy/install.sh`는 이미 실행 중인 서버에 launchd 작업을 하나 더
설치하는 것은 막지만, *서로 다른* 두 서버가 같은 포트를 쓰는 것까지는 막지
못합니다.

| 서버 | 포트 | 공개 호스트 이름 |
|---|---|---|
| `stocks` | 8765 | *(`servers/stocks/.env`의 `MCP_PUBLIC_URL`에 설정)* |
| `lostark` | 8766 | *(`servers/lostark/.env`의 `MCP_PUBLIC_URL`에 설정)* |

### 점검

```bash
# 인증 흐름. 자격증명 없이 어떤 서버에든 쓸 수 있음
.venv/bin/python scripts/check_oauth.py

# 터널을 연 뒤, 실제 공개 URL을 거치는 전체 왕복
.venv/bin/python scripts/check_public.py --env servers/stocks/.env
```

서버별 점검은 각 서버 폴더에 있습니다. 해당 서버의 README를 보세요. 자격증명이나
실제 URL이 필요 없는 점검은 GitHub Actions(`.github/workflows/check.yml`)로 push마다
돕니다.

### 배포 (macOS)

```bash
./deploy/install.sh stocks                                # 라벨: com.local.gg-mcp-stocks
LABEL=com.myname.gg-mcp-stocks ./deploy/install.sh stocks # 직접 정할 수도 있음
```

`RunAtLoad`와 `KeepAlive` 덕분에 로그인하면 서비스가 시작되고, 비정상 종료하면
다시 시작됩니다. 로그는 `~/Library/Logs/gg-mcp-<server>/`에 쌓입니다.

```bash
launchctl bootout gui/$(id -u)/com.local.gg-mcp-stocks   # 제거
```

### 외부에 공개하기

서버는 루프백 주소에만 바인딩합니다. 앞에 터널이나 리버스 프록시를 두고,
`MCP_PUBLIC_URL`을 그렇게 생긴 HTTPS 주소로 설정하세요.

Cloudflare Tunnel을 쓴다면, 구성 방식에 따라 ingress 규칙을 넣는 곳이 다릅니다.

- **로컬 관리형** (`config.yml`로 `cloudflared` 실행): 호스트 이름이
  `http://127.0.0.1:<port>`를 가리키도록 ingress 항목을 추가합니다.
- **원격 관리형** (`cloudflared tunnel run --token …`, 로컬 설정 없음): Zero Trust
  대시보드의 *Networks → Tunnels → 내 터널 → Published application routes*에서
  경로를 편집합니다. 로컬 `config.yml`은 무시됩니다.

> `MCP_PUBLIC_URL`은 공개 URL과 정확히 같아야 합니다. 이 값이 OAuth 발급자(issuer)와
> 리디렉션 기준 주소가 되기 때문에, 다르면 커넥터가 연결되지 않습니다.
> `scripts/check_public.py`가 바로 이 문제를 잡아냅니다.

**앞에 대화형 접근 정책을 두지 마세요.** 중간 로그인 화면이 Claude.ai의 OAuth
왕복을 막습니다. 여기서는 인증을 서버가 맡고, 인증되지 않은 요청에는 이미 401을
돌려줍니다.

### Claude.ai에서 연결하기

커스텀 커넥터는 Free(커넥터 1개), Pro, Max, Team, Enterprise 플랜에서 쓸 수
있습니다. 아래 메뉴 이름은 영어 화면 기준입니다.

1. **Customize → Connectors**
2. **+** → **Add custom connector**
3. URL: `https://<your-host>/mcp`
4. **Advanced settings**(OAuth Client ID/Secret)는 **비워 두세요**. 서버가 동적
   클라이언트 등록을 지원해서 Claude를 자동으로 등록합니다. 값을 넣으면 흐름이
   깨집니다.
5. **Add**를 누른 뒤 **Connect**
6. 열리는 로그인 페이지에 `OAUTH_PASSPHRASE` 입력

Team/Enterprise에서는 소유자가 *Organization settings → Connectors → Add → Custom
→ Web*에서 설정하고, 멤버는 **Connect**만 누르면 됩니다.

---

## 서버 추가하기

```
servers/<name>/
├── pyproject.toml      # name = "gg-mcp-<name>", localauth = { workspace = true }
├── .env.example        # 서버별 포트, 공개 URL, 패스프레이즈
├── README.md
└── src/gg_mcp_<name>/
```

그다음 `uv sync`와 `./deploy/install.sh <name>`을 실행하세요. 다른 서버와 겹치지
않는 포트를 주고, 위 표에 추가하세요.

## 라이선스

[MIT](LICENSE). [Claude Code](https://claude.com/claude-code)로 만들었습니다.

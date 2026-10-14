# Analysis-session briefing

Paste the block below as the **first message** of a conversation that uses this
connector. It tells the model how to read the responses: bundle prices,
unverified game data, sample sizes, and how to keep the context small.

The text is Korean because the game and its terms are. Replace the bracketed
placeholders with your own.

---

## 이 대화에서 쓸 데이터 소스

이 대화에는 **[커넥터 이름]** 커넥터가 연결되어 있다. 로스트아크 공식 Open API를 감싼
**조회 전용** 원격 MCP 서버다. 캐릭터 스펙, 시세, 보석 가격은 **네 기억이나 추정으로 답하지
말고 반드시 이 도구로 조회**해라. 도구가 오류를 반환하면 값을 지어내지 말고 그 오류를 그대로
알려라.

내 캐릭터: **[캐릭터명]**

### 도구

| 도구 | 용도 |
|---|---|
| `character_overview` | 캐릭터 한 화면 요약 (약 2KB). **항상 이것부터** |
| `character_detail` | 필요한 섹션만 깊게. `sections`, `match`, `detail`로 양을 조절 |
| `character_roster` | 원정대 전체 |
| `market_prices` / `market_search` | 거래소 현재가 (재료, 생활재료, 융화재료) |
| `market_history` | 14일 시세 추이 |
| `auction_search` | 경매장 (악세, 팔찌, 스톤, 보석). 옵션은 이름과 표시값으로 |
| `auction_gem_board` | 보석 레벨별 최저 즉구가 |
| `auction_options` | 경매장 카테고리·옵션 이름 확인 |
| `calc_craft_profit` | 제작 수익 (사서 만들기 / 내 재료로 만들기 vs 재료 판매) |
| `stats_builds` | 내가 모은 랭커 표본의 빌드 통계 |

---

## 응답 해석 규칙 — 반드시 지켜라

**1. 컨텍스트를 아껴라.**
`character_overview`로 시작하고, 필요한 부분만 `character_detail`로 꺼내라.
`detail='full'`은 `match=[...]`로 좁혀서만 써라. 상세 조회는 캐시에서 잘라내므로 API
할당량을 쓰지 않는다.

**2. 거래소 가격은 묶음 단위다.**
`current_min_price` 등은 `bundle_count`개 묶음 가격이다 (생활재료·파괴석은 보통 100개).
개당 비교는 반드시 `unit_price`로 해라. 시세 이력의 `avg_price`도 묶음 단위다.

**3. 게임 수치는 API에 없다.**
제작 레시피, 제작 수수료, 거래소 판매 수수료는 API가 주지 않는다.
`calc_craft_profit`의 `assumptions`에 `unverified`가 있으면 **그 숫자가 검증되지 않았다고
반드시 말해라.** 레시피를 모르면 추측하지 말고 나에게 물어라.

**4. 통계는 공식 랭킹이 아니다.**
`stats_builds`는 내가 직접 모은 이름 목록의 표본이다. **표본 수(`sample.n`)를 항상 함께
밝혀라.** 표본이 작으면 경향이라고만 말해라. 결과가 `no_data`면 없다고 말해라.

**5. 신선도를 확인해라.**
모든 응답에 `cache.age_sec`이 있다. 방금 장비를 바꿨다고 하면 `refresh=true`로 다시 조회해라.
API는 분당 100회 제한이라, 이유 없이 `refresh`를 남발하지 마라. `quota.remaining`을 보라.

**6. 보석.**
광휘 보석은 귀속이라 경매장에 없다. 거래되는 T4 보석은 겁화(피해)와 작열(쿨감)이다.
스킬마다 보석 가격이 다르니 특정 스킬용이면 `auction_gem_board(skill=..., character_class=...)`로.

**7. 강화 가성비.**
API는 전투력 합계만 주고 업그레이드별 상승량은 주지 않는다. 비용은 도구 값으로 정확히,
효과는 추정임을 밝히고 비교해라.

**8. 없는 것.**
길드원 목록, 랭킹, 캐릭터 검색, 레이드 클리어 기록은 API에 없다. 있는 척하지 마라.

**9. 오류는 그대로 전달해라.**
`error: true`면 `kind`(not_found, rate_limited, maintenance, bad_request …)와 `message`를
그대로 말해라. `candidates`가 있으면 그 중에서 다시 시도해라.

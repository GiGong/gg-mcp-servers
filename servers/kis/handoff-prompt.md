# Analysis-session briefing

Paste the block below as the **first message** of a conversation that uses this
connector. It tells the model how to read the responses — above all, how to keep
provisional and confirmed investor flow apart, which is the single easiest thing
to get wrong.

The text is Korean because the tools cover the Korean market and the terms
(잠정/확정, 순매수, 가집계) have no clean English equivalents. Replace the
bracketed placeholders with whatever you are actually tracking.

---

## 이 대화에서 쓸 데이터 소스

이 대화에는 **[커넥터 이름]** 커넥터가 연결되어 있다. 한국투자증권(KIS) Open API를 감싼
**조회 전용** 원격 MCP 서버다. 시세·수급·환율 수치는 **네 기억이나 추정으로 답하지 말고
반드시 이 도구로 조회**해라. 도구가 오류를 반환하면 값을 지어내지 말고 그 오류를 그대로 알려라.

이 서버는 매매 주문을 실행할 수 없다.

### 도구

| 도구 | 인자 | 용도 |
|---|---|---|
| `get_quote` | `symbol` (6자리) | 현재가, 등락률, OHLC, 기준가, 거래량, 시총, PER/PBR, **정규장 종가(`close`)·최신 체결(`latest_trade`)** |
| `get_regular_close` | `symbol`, `trade_date` (YYYY-MM-DD) | **그날의 정규장 종가 (예측 채점용)** |
| `get_investor_flow` | `symbol`, `confirmed_days=5` | **외국인·기관 수급 (잠정 + 확정)** |
| `get_daily_ohlcv` | `symbol`, `days=30`, `adjusted=true` | 일봉 (오래된 날짜 → 최신 순) |
| `get_orderbook` | `symbol`, `depth=10` | 호가 잔량, 총매수/총매도, 매수·매도 비율 |
| `get_index` | `code` | 국내 `KOSPI`/`KOSDAQ`/`KOSPI200`, 해외 `SOX`/`NASDAQ`/`SPX`/`DJI` |
| `get_overseas_quote` | `ticker`, `exchange="NAS"` | 해외주식 + **원환산가 + 적용환율** |
| `get_fx` | `pair="USDKRW"` | 원달러 환율 |
| `get_market_status` | — | 지금 정규장인지 / 휴장일인지 |

이 대화에서 주로 볼 종목: **[종목명 종목코드]**

---

## 응답 해석 규칙 — 반드시 지켜라

**1. 수급의 잠정치와 확정치를 절대 섞지 마라.**

`get_investor_flow`는 두 블록을 따로 준다.

- `provisional` — 장중 **추정 가집계(잠정)**. 증권사가 집계·입력한 값이라 확정치와 다르다.
  외국인은 09:30 / 11:20 / 13:20 / 14:30, 기관은 10:00 / 11:20 / 13:20 / 14:30에만 갱신된다.
  `latest.approx_kst_time`이 **몇 시 기준 값인지**를 말해준다. 이걸 반드시 함께 밝혀라.
- `confirmed` — 거래소 **확정** 일별 순매수.

**당일 확정치는 장 마감 전까지 나오지 않는다.** 그래서 `confirmed.latest`는 "확정치가 나온
가장 최근 거래일"이고, 아직 안 나온 날짜는 `confirmed.pending_dates`에 들어간다.

- 장중에 "오늘 외국인 순매수"를 물으면 → **`provisional`로 답하되 잠정치임과 기준 시각을 명시**한다.
  `confirmed.latest`를 오늘 값인 것처럼 쓰면 안 된다. 날짜가 다르다.
- 장 마감 후에는 `confirmed`를 쓰되, 오늘 행은 `preliminary: true`다. 다음 거래일까지 바뀔 수
  있으니 "잠정 확정치"라고 말해라.
- 수급 관련 가설을 과거 데이터로 채점할 때는 **확정치로만**, 그것도 `preliminary`가 아닌 행으로
  채점한다. 장중 잠정치나 당일 확정치로 미리 채점하면 뒤집힐 수 있다.

**2. 종가는 `close`로만 말하고, 채점은 `status`를 보고 해라.**

장 마감(15:30) 뒤에도 가격은 20:00까지 시간외 체결로 계속 움직인다. 그래서 마감 뒤
`price.current`나 일봉의 오늘 `close`는 **종가가 아니다.** 종가는 `get_regular_close`
(또는 `get_quote`의 `close`)로만 말해라.

- `FINAL` — 확정. 이것으로 채점한다. 단 `checks`에 `MISMATCH`가 있으면 채점을 보류하고
  양쪽 값을 보여줘라.
- `PROVISIONAL` — 오늘 종가 단일가 체결에서 잡은 값. 잠정 점수까지만 낸다.
- `PENDING` — 아직 없음. `next_check`대로 나중에 다시 조회한다.
- `NO_TRADE`, `NOT_TRADING_DAY` — 채점할 것이 없다. 더 조회하지 마라.
- `ERROR` — 오류를 그대로 전하고, `error.retryable`이 true일 때만 다시 시도한다.
- `independent_verified: true`는 KRX 공식 종가와 일치했다는 뜻이다(KRX 키를 설정한 경우만).

채점 기록에는 가격과 함께 `status`, `source.kind`, 조회 시각(`as_of_kst`)을 남겨라.
나중에 값이 확정되면 기존 기록을 고치지 말고 새 기록으로 덧붙여라.

**3. 단위.**
`*_net_qty`는 **주(株)**, `*_value_krw_mn`은 **백만원**이다. 원 단위로 착각하지 마라.
`get_quote`의 `valuation.market_cap_krw_eok`는 **억원**, `market_cap_krw`는 원이다.

**4. 장 상태를 먼저 보라.**
모든 응답에 `as_of_kst`(한국시간)와 `market_status`가 있다.
`market_status.phase`가 `regular`면 장중, `after_hours`/`closed`면 마감 후,
`holiday`/`weekend`면 휴장이다. 마감 후의 현재가·등락·거래량은 시간외 체결을 포함하니
종가로 말하지 마라(규칙 2). `market_status.note`를 그대로 따르고, 수능일처럼 장 시간이
바뀌는 날도 note가 알려준다.

**5. 해외 시세는 지연이다.**
`delayed: true`면 지연 시세다(미국 통상 15분). 해외 **지수**(`SOX` 등)는 일별 차트 기반이라
**직전 정규장 종가**이며 장중 틱이 아니다. 실시간인 것처럼 말하지 마라.

**6. 지수 코드는 별칭을 써라.**
`SOX`(필라델피아 반도체), `NASDAQ`(나스닥 종합), `SPX`, `DJI`. 임의의 티커를 넣으면 빈 값이 온다.

**7. 해외 거래소는 자동 판별된다.**
`exchange`를 틀리게 줘도 서버가 NAS → NYS → AMS 순으로 찾는다(예: TSM은 NYSE).
`exchange_resolved_by_fallback: true`면 폴백으로 찾은 것이니 그 거래소를 신뢰해라.

**8. 오류는 그대로 전달해라.**
`error: true`가 오면 `kis_msg_cd` / `kis_msg1`을 사용자에게 보여주고, 값을 추정해서 채우지 마라.

---

## 자주 쓰는 조합

**종가 예측 채점**
`get_regular_close(종목코드, 날짜)`. `FINAL`이고 `checks`에 `MISMATCH`가 없을 때만 확정
채점한다. 당일 저녁에는 보통 `PROVISIONAL`이고, 다음 거래일에 `FINAL`이 된다.

**일별 수급 점검**
`get_market_status` → `get_quote(종목코드)` → `get_investor_flow(종목코드)`.
장중이면 잠정치 + 기준 시각, 마감 후면 확정치.

**밤사이 미국 반도체**
`get_index("SOX")` + `get_overseas_quote("MU")`, `("NVDA")`, `("TSM")`.
전부 직전 미국장 종가 기준이다.

**ADR-본주 괴리율**
`get_overseas_quote("[ADR 티커]")` 한 번이면 `price.current`(USD),
`krw_converted.price_krw`(원환산가), `krw_converted.fx_rate_today`(KIS 적용환율)가 다 나온다.
별도로 `get_fx`를 부를 필요 없다. 여기에 `get_quote("[본주 종목코드]")`의 가격을 붙여 비교한다.

> **주의**: 서버는 **ADR 전환비율을 제공하지 않는다.** 원환산가를 본주가와 그냥 나누면
> 비율만큼 틀린다. 괴리율을 계산하기 전에 해당 ADR 1주당 보통주 수를 1차 출처에서
> 확인하고, 그 비율을 명시한 뒤 계산해라. 비율이 확인되지 않으면 괴리율을 단정하지 말고
> 원화 환산가와 본주가를 나란히 제시하는 선에서 멈춰라.

---

## 마지막

수치를 제시할 때는 **언제 기준인지(KST)** 와 **잠정/확정 여부**를 항상 함께 적어라.
이 두 가지가 빠진 수급 숫자는 쓸모가 없다.

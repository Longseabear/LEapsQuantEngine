---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: kr-domestic-4401
display_name: kr-core-compass
status: live_active
market_scope: domestic
currency: KRW
updated_at: 2026-05-28
---

# kr-core-compass

## ABSTRACT

kr-core-compass는 국장 전체 베타를 무조건 따라가기보다 KOSPI 200 계열 ETF, 유동성 높은 대형주, 현금성 ETF를 섞어 현재 시장이 감당할 만한 위험만 천천히 싣는 코어 allocator다. 상승 추세가 분명할 때는 광범위한 시장 ETF와 대형주 쪽 노출을 늘리고, 중립 구간에서는 방어 자산을 앞세워 속도를 낮추며, 충격 구간에서는 현금성 ETF와 짧은 inverse hedge로 전환한다. 빠른 매매 시스템이 아니라 계좌의 국장 노출을 관리하는 전략이므로, 작은 흔들림에 바로 패닉 판단을 내리지 않고 확인된 리스크 신호와 주문 체결 상태를 함께 보며 움직인다.

## Cadence

- runtime cycle: every 300 seconds in `live_multi_sleeve`
- universe / selection: startup only, with held and open-order symbols forced into the live universe
- alpha model: daily at 09:05 Asia/Seoul, confirmed daily inputs
- targeting / portfolio construction: daily target cadence with active insights and carried holdings
- rebalance / order tracking: every live cycle, with pending-order accounting and anti-oscillation guards

## Recent Judgment Rationale

2026-05-27 최신 live artifact 기준으로 판단은 `risk_on`이다. 목표 포트폴리오는 약 95.0% gross target을 유지하고, 실제 계좌는 총평가액 약 1,431만원 중 현금 약 184만원으로 현금 비중 12.8%, 주식/ETF 노출 87.2% 상태다. unpriced target과 universe mismatch는 현재 0건이며, 과거 가격 누락으로 예산이 묶이던 문제는 해소된 것으로 본다. 남은 현금은 방어 전환이라기보다 3% cash reserve, target carry-forward, 전량 1주 단위 미세 델타, 그리고 `min_order_notional=50,000원 + equity 50bp` 및 수량 필터가 결합된 대기 상태로 해석한다. 따라서 현재 주문 후보 0건은 추가매수 의지가 사라진 것이 아니라, risk-on 노출을 이미 상당 부분 실은 뒤 의미 있는 리밸런싱 단위가 다시 생길 때까지 기다리는 상태다.

## Strategy Shape

```text
KRX ETF and liquid large-cap universe
  -> market-regime classification
  -> risk / defensive / hedge insights
  -> volatility-aware percentage targets
  -> long-only risk clamps
  -> domestic limit order intents
```

The universe mixes broad KRX ETFs, sector ETFs, liquid large caps, defensive
cash-like ETFs, and a small inverse hedge. The strategy is broader and more
opportunistic than a pure low-volatility sleeve, but it still avoids small,
illiquid, story-driven positions.

## Signal Family

The core market proxy is `KRX:069500`. Regime classification uses confirmed
daily trend, momentum, drawdown, volatility-adjusted one-day return, and
liquidity features. Hard shock is not triggered by an ordinary fixed-percent
pullback alone; it requires an extreme absolute drop, roughly a 2.5-sigma
downside daily return measured against 20-day realized return volatility with
trend or drawdown damage, or a deep drawdown with weak trend. Risk assets are
ranked by momentum, trend quality, volatility control, drawdown behavior,
liquidity, and their role in the core universe.

Regimes:

- `strong_risk_on`: high risk budget for ETFs and liquid large caps.
- `risk_on`: normal risk budget.
- `neutral`: reduced risk budget with larger defensive allocation.
- `risk_off`: flatten risk assets and hold defensive ETFs.
- `shock`: defensive-dominant allocation plus a small inverse hedge.

## Portfolio Policy

The alpha emits `UP` insights for desired risk or defensive holdings and `FLAT`
insights for risk assets that should be exited in weak regimes. Portfolio
construction converts active insights into percentage targets, caps ordinary
risk positions at 10%, core ETF exposure at 18%, defensive sleeves at 30%, and
the inverse hedge at 6%. Gross exposure increases are ramped, and whole-share
flooring avoids entries that are too small to survive quantity rounding.

The inverse hedge is explicitly short-lived: it is opened only in `shock`, uses
a one-day insight horizon, and receives a `FLAT` exit signal as soon as the
market is no longer classified as shock.

## Risk And Execution

The strategy is KRW-only and long-only at the account level. Risk limits cap
single-name exposure, total exposure, and cash buffer usage, and reject stale or
invalid entry snapshots. Execution emits KRX limit order intents with day
time-in-force, bounded slice size, buy/sell session windows, drift thresholds,
and replacement limits. Broker submission and lifecycle reconciliation remain
owned by the engine runtime, not by the strategy.

ETF buy hard guards were temporary APBK3026 protection, not part of the long-run
strategy. They were removed after KIS gateway restart and live validation on
2026-05-22 confirmed KRX-routed ETF orders were accepted.

## Live Operations

`kr-core-compass` is the operator-facing strategy name. The stable system ID
remains `kr-domestic-4401` so runtime artifacts, broker routing, account
ownership, and order lineage stay continuous.

Status is `live_active` because `kr-domestic-4401` is active in
`live_multi_sleeve`, has current live reports, and has completed real broker
submission and fill reconciliation through the `kis-domestic-4401` route.

On 2026-05-22 at 15:14-15:15 KST, the sleeve validated the APBK3026 fix and ETF
guard removal with real KRX ETF orders. The first validation order was
`KRX:069500` 6 shares, accepted with KIS message `APBK0013`. The resumed live
cycle then submitted and reconciled additional ETF fills:

| Symbol | Quantity | Average / Fill Price |
| --- | ---: | ---: |
| `KRX:069500` | 6 | 123,155 |
| `KRX:102110` | 5 | 123,195 |
| `KRX:278530` | 12 | 44,495 |
| `KRX:315930` | 6 | 90,850 |
| `KRX:337140` | 13 | 41,350 |

Latest live status after the resume showed no open tickets and no attention
flag. The operator-facing watch item remains turnover control, not ETF routing.

## Operator Notes

Latest research snapshots after the intraday replay daily-indicator fix:

| Window | Data | Return | MDD | Turnover |
| --- | --- | ---: | ---: | ---: |
| 2021-05-24 to 2026-05-21 | Daily | +79.31% | 17.34% | 132.24x |
| 2024-08-06 to 2026-05-21 | 60m | +77.14% | 11.69% | 76.39x |
| 2026-04-23 to 2026-05-21 | 1m | +4.80% | 10.31% | 6.56x |

Next improvement priority is target hysteresis through a practical no-trade
band first, then regime persistence, then an explicit turnover cap. The current
execution and portfolio guards already reduce quantity oscillation, but a
no-trade band is the cleanest next layer because it preserves deliberate
rebalancing while suppressing small target changes caused by score noise.

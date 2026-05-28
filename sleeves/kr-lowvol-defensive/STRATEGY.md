---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: kr-lowvol-defensive
display_name: KR Low-Vol Defensive
status: live_active
market_scope: domestic
currency: KRW
updated_at: 2026-05-28
---

# KR Low-Vol Defensive

## ABSTRACT

이 슬리브는 국장에서 급등 테마나 개인 수급 쏠림을 따라가기보다, 변동성이 낮고 사업 안정성, 밸류에이션, 배당, 완만한 추세가 함께 받쳐주는 종목을 천천히 담는 방어형 주식 전략이다. 핵심은 "가장 강한 종목을 쫓는 것"이 아니라 "깨질 확률이 높은 종목을 피하면서 안정적인 복리 구간에 머무는 것"이다. 그래서 급등락, 갭, 거래대금 폭증, 과열된 개인 매수, lottery-like 수익률 구조를 강하게 벌점 처리하고, 주간 단위로 목표 포트폴리오를 만들되 5분마다 현재 가격과 현금 변화를 반영해 기존 목표와의 드리프트를 추적한다. 평상시에는 현금을 과하게 놀리지 않고 저변동 바스켓에 배분하지만, 선택된 종목군 자체가 불안정하거나 혼잡해지면 노출을 낮춘다.

## Cadence

- runtime cycle: 5 minutes while the live KRX sleeve is scheduled (`cycle_interval_seconds = 300`)
- universe / selection: static KRX defensive coarse universe with active low-vol selection up to 40 symbols; fine universe refresh is disabled; held and open-order symbols remain operationally included
- alpha model: daily at 08:50 Asia/Seoul, using confirmed daily indicators
- targeting / portfolio construction: weekly at `week_start_at 08:55 Asia/Seoul`
- rebalance / order tracking: every scheduled 5 minute cycle reuses the latest target and checks drift against current cash, holdings, prices, and open orders
- execution window: buy orders are allowed from 09:05 to 14:50 Asia/Seoul

## Recent Judgment Rationale

2026-05-28 점검 기준으로 목표 비중과 보유 수량 사이에 의미 있는 신규 진입이 남아 있는데도 주문 후보가 0건으로 줄어드는 원인은 50만 원 최소 주문금액이 계좌 규모 대비 높았기 때문으로 확인했다. 사실: 최소 주문 기준을 15만 원 정액과 sleeve 목표가치의 200bp 중 큰 값으로 완화해, 050890처럼 약 40만 원대 신규 진입은 살아나고 005290 +2주, 055550 +1주 같은 10만 원대 소액 튐은 계속 걸러지도록 했다. 추정: 남는 현금은 앞으로도 2% 현금 버퍼, 정수 주식 라운딩, reused-target churn guard 때문에 일부 유지될 수 있지만, 실제 목표 비중과 보유가 의미 있게 어긋난 리밸런싱은 후보로 올라오는 쪽이 맞다.

## Strategy Shape

```text
KRX defensive universe
  -> low-volatility / anti-lottery active selection
  -> daily defensive UP insights
  -> inverse-volatility target weights
  -> long-only risk controls
  -> patient limit order intents
```

The sleeve is live-active in the multi-sleeve runtime and trades Korean domestic
stocks in KRW. It is intentionally separate from `semiconduct-kor` and from the
more aggressive LEaps momentum/growth sleeve.

## Signal Family

The strategy favors:

- low 20/60/120 day realized volatility
- smooth 20/60 day trend rather than explosive momentum
- sufficient liquidity
- quality support from ROE and debt profile
- value support from PER/PBR
- dividend yield support

It penalizes or rejects:

- extreme normalized volatility
- weak medium-term momentum with high volatility
- falling-knife drawdowns
- large gaps and wide intraday ranges
- one-day upside spikes with high volume
- turnover shocks and volume-ratio spikes
- real retail crowding fields when available
- proxy crowding from volume, return, and turnover behavior when real crowding
  data is absent

The active alpha emits UP insights only. It does not create orders, does not
short, and does not directly decide broker action.

## Portfolio Policy

The portfolio model selects up to 15 active UP insights and allocates by
inverse volatility, then adjusts weights with quality, value, dividend, lottery,
crowding, and turnover-shock information.

Gross exposure is regime-scaled:

- calm basket: up to 100% gross before the 2% cash reserve
- normal defensive basket: up to 95% gross before reserve
- unstable or crowded basket: around 88% gross before reserve
- high average heat can force the sleeve back toward defensive exposure

The live target mode is patch-style. Missing symbols are not automatically sold
just because a weekly target omits them, but positions can be reduced or exited
when there is no active UP insight, when an active but unselected signal is too
stale, or when a held position has been flat for roughly two weeks.

## Drift Rebalancing

Weekly portfolio construction creates the target basket and target percentages.
Between weekly refreshes, the runtime keeps reusing the latest target. On each 5
minute cycle, order sizing recalculates the quantity needed from current
prices, sleeve cash, holdings, and open tickets.

This means newly added cash can be deployed before the next weekly portfolio
refresh if the existing target has enough underweight drift. Small noise is
filtered by an effective minimum order notional of max(KRW 150,000, 200 bps of
target portfolio value), one-share minimum quantity delta, and reused-target
churn guards.

## Risk And Execution

Risk is long-only with a 2% cash buffer, 23% max position cap, and 98% max total
exposure cap. Fresh snapshots are required for entries, and invalid snapshots
are rejected.

Execution is patient limit execution:

- day limit orders
- 6 bps limit offset
- max KRW 1,000,000 per slice
- up to 4 slices
- stale order age around 20 minutes
- bounded cancel/replace behavior with 70 bps price-drift trigger

Sells are allowed, but only as the result of target reduction, stale-position
exit, risk control, or operator/runtime lifecycle handling. They are not a
separate short or fast timing strategy.

## Operator Notes

Use this sleeve as a low-noise Korea equity sleeve, not as a theme chaser. If
cash remains high, first check whether the latest target gross exposure,
minimum order notional, stale target state, or orderability window explains it.
If turnover rises, check whether a fresh weekly portfolio target is fighting
with reused-target drift tracking.

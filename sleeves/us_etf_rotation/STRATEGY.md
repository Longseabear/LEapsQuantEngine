---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: us_etf_rotation
display_name: US ETF Rotation
status: live_active
market_scope: overseas
currency: USD
updated_at: 2026-05-28
---

# US ETF Rotation

## ABSTRACT

US ETF Rotation은 미국 ETF 전체를 무조건 사서 버티는 전략이 아니라, 시장이 위험을 받아들이는 구간에는 강한 섹터와 스타일 ETF에 집중하고 신호가 약해질 때는 방어 ETF나 현금 여유로 속도를 낮추는 회전 전략이다. 핵심은 변동성이 낮은 ETF만 고르는 것이 아니라 추세, 절대모멘텀, 리스크온/오프, 유동성, 단기 과열과 건전한 눌림, 포지션 상한을 함께 보면서 5분 단위로 과격하지 않게 목표 비중을 조정하는 것이다. 그래서 상승장 참여와 잦은 매매 억제를 동시에 노리되, 급변장에서는 FLAT 신호와 트레일링 스톱, 리스크 캡으로 손실 확대를 막는 성격을 가진다.

## Cadence

- runtime cycle: 5 minutes (`cycle_interval_seconds = 300`)
- universe / selection: once per day, up to 8 active ETFs plus operational symbols
- alpha model: every sleeve cycle, using confirmed daily indicator inputs
- targeting / portfolio construction: every 5 minutes, top 4 target basket
- rebalance / order tracking: risk and execution run each framework cycle; order runtime owns tickets, fills, expiry, and reconciliation

## Recent Judgment Rationale

2026-05-28 00:02 KST 기준 최근 판단은 canary ETF가 risk-on을 유지하는 동안 `SMH`, `XLE`, `XLK`, `QQQ` 중심의 top basket을 보유하는 것이다. 사실: 최신 live target은 `SMH`만 7주에서 6주로 낮고 `XLE`, `XLK`, `QQQ`는 현재 보유수량 유지이며, gross exposure는 약 86%, 현금은 약 2.2k USD, 주문 후보와 미체결 티켓은 0건이다. 추정: 현금이 남는 주된 이유는 현금화 신호가 아니라 PPO 목표 gross, 25% 단일 ETF 상한, 2% cash buffer, whole-share rounding이 합쳐진 결과이며, 리밸런싱은 이제 단순 주식 수 문턱보다 목표 비중 이탈이 의미 있게 튀었는지를 우선 본다.

## Strategy Shape

The sleeve is USD-only and trades liquid US ETFs through the overseas broker
route. It does not trade US single stocks, Korean stocks, or KRW instruments.

```text
US ETF universe
  -> daily ETF selection
  -> DAA pullback and volatility trailing-stop insights
  -> PPO-wrapper portfolio construction with deterministic fallback
  -> long-only risk clamps and cycle buy caps
  -> day limit order intents through the KIS overseas route
```

Offensive ETFs include broad equity, growth, semiconductor, technology,
financials, health care, energy, and industrials exposure. Defensive ETFs
include Treasuries, gold, minimum volatility, staples, and utilities.

## Score And Regime

The main score favors ETFs with positive medium-term and long-term momentum,
price above trend, enough liquidity, acceptable realized volatility, and a
useful short-term pullback inside an uptrend. Volatility is a penalty, not the
main selection rule.

Risk-on/risk-off is decided by the canary ETFs `SPY`, `QQQ`, and `IWM`. The
sleeve is risk-on when at least two of the three are above trend and have
positive medium-term momentum. In risk-off, offensive entries are blocked and
defensive ETFs can be preferred.

The main DAA pullback alpha emits UP insights for up to 4 selected ETFs and
FLAT insights for unselected symbols. The trailing-stop alpha emits FLAT when
price breaches a volatility-aware high-watermark stop. Same-timestamp FLAT
signals must take priority over UP signals when resolving targets.

## Portfolio Policy

The active portfolio model is the PPO allocator wrapper using the
`daa_pullback_c18` profile. It currently runs in `risk_softmax` mode with a
top-4 basket, long-only exposure, 25% maximum position weight, and exposure
levels from 0% to 95%.

The portfolio model emits target percentages only. The engine owns
cash-constrained sizing, whole-share rounding, and order lifecycle effects.
Target resolution is `patch` mode, so a sparse target batch does not imply a
complete liquidation unless an explicit FLAT or zero target is present.

Portfolio blending is enabled over 300 orderable-session minutes with an 8%
target drift threshold. This keeps fresh capital, model changes, and target
adjustments from becoming a single abrupt trade wave.

## Risk And Execution

Live risk posture:

- long only
- max position: 25%
- max total exposure: 98%
- cash buffer: 2%
- cycle buy cap: the smaller of 10,000 USD and 65% of USD equity
- fresh snapshot required for new entries

Execution emits day limit order intents with no price offset. The execution
model does not touch broker APIs directly; broker submission, fills,
cancellations, expiry, and reconciliation are handled by the order runtime.

## Operator Notes

This sleeve is `live_active` in the current live multi-sleeve control file and
owns USD capital separately from domestic sleeves. Operator reports should show
it independently from KRW sleeves.

For meaningful strategy changes, compare long, one-month, one-week, and
single-day minute replay results. Short-window tests must include warmup. If a
minute feed is absent, report "local minute replay feed unavailable" rather
than treating the runtime CLI as unsupported.

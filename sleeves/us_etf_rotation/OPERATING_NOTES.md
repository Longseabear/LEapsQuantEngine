# us_etf_rotation operating notes

These notes capture the practical rules learned while bringing the
`us_etf_rotation` sleeve into live operation.

## Current stance

The sleeve should remain a general US ETF rotation sleeve before it tries to
be a single-stock strategy. Its edge is not one heroic ETF pick. The edge is
the combination of:

- score calculation
- rebalance cadence
- risk-on/risk-off filtering
- cash handling
- regime detection
- order churn control

The active live model is the DAA pullback variant:

```text
alpha:      alphas/daa_pullback.py
alpha_id:   us_etf_rotation_daa_pullback
portfolio:  portfolios/rl_ppo_constructor.py
model_name: daa_pullback_c18
mode:       risk_softmax
top_k:      4
gross:      0.95
max_pos:    0.25
temp:       0.80
resolver:   patch
```

## What the model is trying to do

The model is a diversified adaptive allocation style ETF rotation model.

It uses SPY, QQQ, and IWM as canaries. When at least two canaries are above
trend and have positive medium-term momentum, the sleeve is risk-on and can
own offensive ETFs. Otherwise it should prefer defensive assets.

Offensive candidates are ranked by composite momentum, trend confirmation,
liquidity, volatility penalty, and a small pullback adjustment. The pullback
component should help only when an ETF is in an uptrend but has cooled off
over the short window. It should not turn the sleeve into a falling-knife
mean-reversion system.

## Important live lessons

Daily one-bar backtests can be misleading for this sleeve. A one-day daily
backtest that starts from cash buys and marks on the same bar, so it can show
a loss that is almost entirely fees and slippage. Use minute replay for
single-day diagnostics.

Minute replay exposed the old model's main flaw: under a five-minute live
portfolio cadence, `etf_rotation.py + attention_ppo + rl_weights + top_k=8`
created excessive target churn. On 2026-05-14, the old model created 209
orders in the minute replay and lost heavily to friction. The DAA pullback
model created only 3 orders on the same feed.

Sparse target batches must not be treated as a complete liquidation list for
this sleeve. Keep portfolio target resolution in `patch` mode unless the
portfolio model intentionally emits a complete target set. The previous
`missing_target_zero` behavior caused unintended exits when a held symbol was
omitted from a sparse batch.

The sleeve benefits from a larger capital base. With small capital, high
priced ETFs such as SMH create whole-share rounding problems and leave more
cash idle. Additional capital improves target tracking and reduces friction
as a percentage of equity.

## Rebalance and execution rules

The alpha may run every cycle, but the portfolio model should only create
fresh actionable targets at the configured five-minute cadence. Risk and
execution still run each cycle so exits, stale-ticket handling, and order
lifecycle maintenance stay responsive.

The live sleeve runtime itself should run every five minutes:

```text
worker.cycle_interval_seconds: 300
universe.active.cadence: once_per_day
alpha cadence: every_cycle inside the sleeve cycle
portfolio.rebalance.cadence: every_5_minutes
```

This matches the ETF model's confirmed-daily input surface and avoids
collecting the same live quotes every minute when the portfolio can only
create fresh targets every five minutes. Open-ticket supervision still runs on
the outer live-loop tick, so broker lifecycle maintenance is not delayed by the
ETF model cadence.

Keep churn guards enabled:

```text
min_order_notional: 200
min_quantity_delta: 2
reused_target_churn_guard: true
```

The sleeve should avoid replacing or re-sending orders on every quote tick.
Execution policy should handle order urgency and order lifecycle rules, while
the broker/order runtime owns actual ticket state.

## Risk rules

Default live risk posture:

```text
gross exposure target: 0.95 risk-on model target, capped by risk/cash/rounding
max position:          0.25
cash buffer:           0.02
cycle buy cap:         min(10000 USD, 65% of USD equity)
long only:             true
```

This is intentionally not a full-send strategy. It should participate in
risk-on markets, but still keep enough cash to absorb rounding, slippage, and
model error. Capital deployment limits belong in the sleeve risk model, not as
the primary broker-submit guard.

If performance is good, increase sleeve capital before increasing leverage or
adding single stocks. The first scaling step should be more capital in the
same ETF sleeve, not a broader universe.

## Adding single stocks

Do not mix single stocks directly into this ETF sleeve without a separate
research pass. Single stocks have different gap risk, earnings risk, news
risk, and volatility. They should not be scored on the same surface as broad
ETFs unless that surface is explicitly redesigned.

If single stocks are added later, prefer a separate sleeve such as
`us_equity_alpha` with its own universe, alpha, risk model, and backtests.
Start it as a small satellite sleeve instead of diluting the ETF rotation
contract.

## Backtest checklist

For every meaningful change, compare:

- long: one year or more
- short: one month
- ultra short: one week
- single day: minute replay when local minute feed exists

Use daily backtests for broad research and minute replays for live-cadence
diagnostics.

When testing short windows, always include warmup:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily `
  configs/runtime/us_etf_rotation_sleeve.json `
  --sleeve-id us_etf_rotation `
  --start <start-date> `
  --end <end-date> `
  --warmup-start <warmup-date> `
  --cash 5000 `
  --currency USD `
  --source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --summary-only
```

For minute replay:

```powershell
py -3 -m leaps_quant_engine.cli download-us-minute-feed `
  configs/runtime/us_etf_rotation_sleeve.json `
  --sleeve-id us_etf_rotation `
  --output data/replay/us_etf_rotation_<date>_minute.csv `
  --start <date> `
  --end <date> `
  --provider yfinance `
  --overwrite `
  --summary-only

py -3 -m leaps_quant_engine.cli runtime-backtest-minute `
  configs/runtime/us_etf_rotation_sleeve.json `
  --sleeve-id us_etf_rotation `
  --minute-feed data/replay/us_etf_rotation_<date>_minute.csv `
  --start <date>T09:30:00 `
  --end <date>T16:00:00 `
  --warmup-start <warmup-date> `
  --cash 5000 `
  --currency USD `
  --daily-source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --summary-only
```

If the minute feed is missing and cannot be downloaded, report that as
"local minute replay feed unavailable" rather than treating it as a CLI
limitation.

## Operator checks

Before market open or after model changes, check:

- live loop process is alive
- active sleeves include `us_etf_rotation`
- open ticket count is zero or expected
- order runtime `needs_attention` is false
- target resolution is still `patch`
- alpha is still `us_etf_rotation_daa_pullback`
- config is still `daa_pullback_c18`

Useful read-only command:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-status `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id us_etf_rotation `
  --account-id kis-overseas `
  --account-store data/virtual-accounts/kis_overseas.json `
  --order-store data/order-runtime/kis_overseas.jsonl `
  --summary-only
```

## Capital scaling guidance

When adding capital, prefer scaling the existing ETF sleeve first. This keeps
the operating contract stable and improves whole-share target tracking.

Do not respond to good short-term performance by immediately adding single
stocks, raising exposure, or disabling churn guards. The first safe scaling
path is:

```text
more capital -> same model -> same risk limits -> observe live behavior
```

Only after that should the sleeve consider more aggressive parameters or a
separate single-stock satellite sleeve.

## 2026-05-19 scaling research

Before the 2026-05-19 US session, the sleeve compared the old top3 structure
with top4/top5 DAA pullback variants using the same daily runtime backtest,
warmup, KIS-style fees, and 5 bps slippage.

```text
current top3/max30/temp05:
  1y  +22.40%, Sharpe 1.52, MDD 9.98%
  1m   +5.98%, Sharpe 5.55, MDD 1.44%
  1w   -0.37%, Sharpe -0.66, MDD 1.02%

selected top4/max25/temp08:
  1y  +21.93%, Sharpe 1.54, MDD 8.70%
  1m   +6.35%, Sharpe 5.27, MDD 1.60%
  1w   -0.33%, Sharpe -0.51, MDD 0.96%
```

The top4 variant was selected for scaling because it kept recent performance
competitive while reducing one-year drawdown and single-position concentration.
Top5 variants lowered long-horizon drawdown further, but they lagged both the
current and top4 variants on the 1-month and 1-week windows.

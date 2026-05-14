# LEaps Operations Playbook

This note captures the practical operating lessons for the `LEaps` sleeve.
Keep it close to the sleeve code so future alpha, portfolio, risk, and
execution changes preserve the same LEAN-style boundaries.

## Current Live Strategy

The active `LEaps` live profile is a KRW/KRX momentum sleeve. The design thesis
is:

```text
own the strongest KRX trend leaders when the local regime is healthy,
avoid buying from stale/degraded data,
cut exposure when stop pressure or volatility rises,
and let execution throttle order style by market session.
```

The current strategy is not a prose-only rule set. It is split across the
pipeline:

- `selections/stock_momentum.py` builds the KRX entry candidate set.
- `alphas/kospi_conviction.py` emits KRX UP insights.
- `alphas/volatility_trailing_stop.py` emits FLAT exit/reduction insights.
- `portfolios/rl_ppo_constructor.py` wraps the RL target allocator.
- `risks/kospi_growth_us_hedge.py` clamps exposure by currency, cash, snapshot
  quality, and market regime.
- `executions/leaps_immediate.py` turns approved targets into session-aware
  sliced limit order intents.

The live config currently routes:

```text
leaps-stock-momentum      -> leaps-kospi-conviction
leaps-stock-momentum      -> leaps-kospi-pullback-reversion
leaps-operational-symbols -> leaps-volatility-trailing-stop
portfolio                 -> attention_ppo / rl_weights / top_k=8
risk                      -> KRW regime exposure cap
execution                 -> day limit orders with slicing
```

US ETF rotation is intentionally handled by the separate `us_etf_rotation`
sleeve, not by this LEaps live profile.

## Strategy Details

### Stock Momentum Selection

`StockMomentumSelectionModel` is KRX-only. It rejects non-KRX symbols and ETFs,
then ranks stocks using momentum, liquidity, and normalized volatility.

Important behavior:

- Missing indicator snapshot returns an empty selection with rejection reasons.
- Volatility >= `0.24` blocks the candidate.
- Volatility <= `0.18` passes the volatility filter.
- Between those values, the stock needs very strong momentum to pass.
- Score is recency-weighted momentum plus sector relative strength, trend
  strength, and a small liquidity bonus, minus a volatility penalty.
- Recency weighting uses 20-day momentum, 5-day acceleration, and 60-day
  momentum when the universe provides it.
- Sector relative strength is computed from the selected KRX research universe
  using the `sector` property in the universe file.

This is the first guardrail against chasing unstable names. It is still a
selector, not alpha; it only decides which symbols the KOSPI conviction alpha
is allowed to evaluate.

### KOSPI Conviction Alpha

`leaps-kospi-conviction` emits UP insights only. It does not emit FLAT exits.

Entry logic:

- Only KRX symbols are considered.
- `context.allows_new_entries` must be true.
- Close must be above the slow average.
- Fast average must be above the slow average.
- 20-day momentum/trend must be positive.
- Volatility must pass the filter, unless momentum and trend are strong enough
  to justify a high-volatility exception.

The alpha scores candidates with:

```text
KOSPI bias bonus
+ market breadth / average positive momentum bonus
+ recency-weighted momentum
+ trend strength
+ sector relative strength
+ entry timing bonus
+ liquidity bonus
- normalized volatility penalty
```

It emits at most 5 UP insights, with metadata carrying market breadth,
momentum, sector strength, entry timing, volatility, rank, and the conviction
bonus. The risk model uses this metadata to infer the local regime.

The entry timing metadata is not the final order trigger. It is evidence for
portfolio/risk/execution:

- `trend`: trend is healthy but no special timing bonus.
- `pullback`: strong trend with a healthy pullback from the 20-day high.
- `rebreak`: shallow pullback followed by positive 5-day re-acceleration.

### Trend Pullback/Rebreak Alpha

`leaps-kospi-pullback-reversion` is now part of the active LEaps alpha stack.
It is still an UP alpha, but it is deliberately narrower than KOSPI conviction.

It looks for:

- KRX symbols only.
- Existing positive trend and positive 20-day momentum.
- Volatility below the pullback alpha threshold.
- Either a healthy pullback in an uptrend or a shallow rebreak near the prior
  high with positive 5-day acceleration.

This alpha exists to avoid pure breakout chasing. In the portfolio layer it can
provide a cleaner entry-timing insight for symbols that are already in the
stock-momentum selected universe.

### Volatility Trailing Stop Alpha

`leaps-volatility-trailing-stop` is an exit alpha. It emits FLAT insights when
the close falls below the model's trailing stop.

The stop mark is:

```text
high_watermark - max(ATR * 2.5, stddev * 2.0, close * 0.08)
```

The high watermark is read from `context.model_state` when available. If there
is no previous state, the model initializes from current close and the
20-period rolling high. The updated mark is requested through `StatePatch`.

This keeps trailing-stop memory replayable and prevents alpha code from reading
virtual account files or order stores.

### RL Portfolio Constructor

`portfolios/rl_ppo_constructor.py` is a thin wrapper around the engine's
`ReinforcementLearningPortfolioConstructionModel`.

The active config uses:

```text
model_name = attention_ppo
allocation_mode = rl_weights
top_k = 8
exposure_levels = 0%, 25%, 50%, 75%, 95%
weight_temperature = 0.3
max_position_pct = 26%
fallback_gross_exposure = 78%
emit_zero_for_missing_held_targets = true
target_smoothing_alpha = 1.0
target_drift_threshold_pct = 3.5 percentage points
```

In this mode, the portfolio model decides target weights, not order quantities.
The engine later converts those target percentages into lots. If the policy
artifact is unavailable, the model falls back to configured deterministic
exposure behavior so validation and smoke runs still work.

The target drift guard reads the prior portfolio target from
`context.model_state` and requests anchor updates with `StatePatch`. With
`target_smoothing_alpha=1.0`, it does not lag meaningful rebalances; it only
keeps the previous target when the new target is within the configured drift
threshold. Explicit FLAT/DOWN or stop exits bypass the guard and remain 0%
targets immediately.

The current live model was preserved before the sector/pullback upgrade at:

```text
data/model-bundles/LEaps/20260514_224051_pre_sector_pullback_upgrade
```

The upgraded sector/pullback/target-anchor profile is bundled at:

```text
data/model-bundles/LEaps/20260514_230858_sector_pullback_target_anchor
```

The reward profile used for training is shape-aware rather than pure CAGR:

- downside return penalty
- volatility penalty
- drawdown penalty
- underwater-time penalty
- turnover penalty
- missed-upside penalty
- concentration penalty

This is why strategy evaluation should look at drawdown, Sharpe-like shape,
turnover, and live tradability, not only final return.

### Regime Risk Model

`LeapsKospiGrowthUsHedgeRiskModel` is the exposure gate. In the current LEaps
live profile it mainly manages KRW exposure.

Active live limits:

```text
max KRW position pct       = 26%
base KRW total exposure    = 68%
KRW cash buffer            = 10%
risk_off regime exposure   = 35%
neutral regime exposure    = 60%
risk_on regime exposure    = 78%
strong_risk_on exposure    = 95%
```

The regime is inferred from active `leaps-kospi-conviction` UP insights and
`leaps-volatility-trailing-stop` pressure:

- many stops, weak breadth, or high volatility -> `risk_off`
- broad strong momentum with low volatility -> `strong_risk_on`
- broad positive momentum -> `risk_on`
- narrow but very strong leadership can still become `risk_on`
- otherwise -> `neutral`

Freshness matters. If snapshot quality says new entries are not allowed, risk
rejects target increases but still permits risk checks and reductions.

### Execution Model

`LeapsMomentumExecutionModel` creates order intents only. It does not submit
broker tickets.

Active live execution settings:

```text
order_type                 = limit
time_in_force              = day
buy_limit_offset_bps       = 8
sell_limit_offset_bps      = 15
stop_sell_limit_offset_bps = 35
max_slice_notional         = 2,000,000 KRW
max_slices                 = 3
max_daily_volume_participation_bps = 50
chase_guard_intraday_return_bps    = 500
chase_guard_size_multiplier        = 0.4
```

Session policy:

- Closed or non-orderable session -> no order intent.
- Regular open/close auction -> buys are reduced, sells keep full size.
- Extended sessions -> buys are reduced more aggressively, sells keep full
  size.
- KRX after-hours single-price is blocked unless explicitly enabled later.
- KRX limit prices are rounded to the side-safe tick grid.

Slicing means the execution model may split a large target delta into up to
three order intents, each bounded by the configured notional cap. Any remaining
quantity is deferred to a later cycle; it is not hidden state inside the model.

## Layer Boundaries

Treat the sleeve as a deterministic pipeline:

```text
selection -> alpha -> portfolio -> risk -> execution -> order intent
```

The most important rule is simple: models should only read immutable context
and emit their own layer's output.

- Selection models choose symbols.
- Alpha models emit `Insight` records.
- Portfolio models emit target percentages, not broker quantities.
- Risk models approve, clamp, reject, or reduce targets.
- Execution models emit `OrderIntent` records.
- Broker tickets, fills, cancels, expiry, account state, and order lifecycle are
  engine/runtime concerns.

Do not let alpha, portfolio, risk, or execution modules call KIS, broker-engine,
virtual account stores, or order stores directly. If a model needs live account
or order information, that information must be projected into the immutable
context by the engine first.

## Stateful Models

Stateful strategy memory belongs in runtime model state, not ad hoc files.

Use:

```python
record = context.model_state.get(
    model_id="leaps-volatility-trailing-stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
)
```

and request updates with `StatePatch`:

```python
StatePatch(
    key=context.model_state.key(
        model_id="leaps-volatility-trailing-stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
    ),
    value={"high_watermark_price": 84000, "last_price": 82000},
    reason="trailing_stop_mark",
)
```

The framework commits state patches only after a successful framework cycle.
This keeps failed cycles from partially advancing model memory.

For function-style alpha modules, expose:

```python
def generate(context):
    ...

def state_patches(context, insights=()):
    ...
```

The Python alpha loader preserves both hooks.

## Trailing Stop

`alphas/volatility_trailing_stop.py` tracks the prior high watermark through
`context.model_state`, then emits only `InsightDirection.FLAT` when the current
close falls through the volatility stop.

It should not:

- read holdings directly from a virtual account file
- submit sell orders
- cancel open tickets
- write its own high-watermark file

It may:

- read current indicators from `SnapshotContext`
- read previous high watermark from `context.model_state`
- emit FLAT insights
- emit `StatePatch` records for the next committed cycle

Operational symbols are intentionally routed into trailing stop input. Held,
open-order, manual, and exit-watch symbols must remain visible even when fresh
entry selectors drop them.

## Portfolio Semantics

The RL portfolio constructor is a target portfolio allocator. It is not an
order placer.

The expected behavior is:

- Active UP insights define the candidate set.
- The RL model emits target weights over the top-k candidates plus cash.
- Portfolio construction emits target percentages.
- Order sizing converts percentages to quantities.
- Risk clamps or rejects.
- Execution creates order intents.

If a held symbol is absent from a new valid target set, the portfolio layer may
emit a zero target tagged as no longer in the target portfolio. If no actionable
insights exist at all, do not treat that as an implicit all-sell signal; wait
for explicit FLAT/DOWN insights or a fresh valid target set.

Small target changes may be suppressed by the target anchor drift guard. This is
a portfolio policy, not an execution shortcut: the model still emits target
percentages, then order sizing, risk, and execution run normally.

## Risk And Execution

Risk should express capital limits, freshness gates, long-only policy, regime
exposure caps, and cash buffers. It should not submit, cancel, or replace
orders.

Execution should express order style:

- market or limit
- limit offset
- time in force
- slice policy
- session multipliers
- stale/cancel/replace preferences

The order runtime owns the ticket lifecycle. Day-order expiry, stale-ticket
supervision, partial-fill reconciliation, and broker IDs are not sleeve model
state.

## Live State Store

Live operation should opt into the runtime state store:

```text
data/runtime/runtime-state/live_multi_sleeve.sqlite
```

Use read-only runs when checking behavior during live operation:

```powershell
$root=(Resolve-Path .).Path
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-run-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --journal "$root\data\runtime\state-smoke\LEaps_state_check.jsonl" `
  --order-batch-output "$root\data\runtime\state-smoke\LEaps_candidate_orders.json" `
  --framework-state "$root\data\runtime\state-smoke\LEaps_framework_state.json" `
  --runtime-state "$root\data\runtime\runtime-state\live_multi_sleeve.sqlite" `
  --runtime-state-read-only `
  --summary-only
```

Confirm these fields in the output:

```text
framework.new_insights.state_patch_count
framework.model_state.patch_count
framework.model_state.commit_enabled = false
engine_status.framework.model_state_patch_count
order_intents
```

`commit_enabled=false` is expected for read-only checks.

## No-Order Diagnosis

When a live or smoke cycle produces no orders, check in this order:

1. Market session
   - `execution.metadata.market_sessions.*.is_orderable`
   - closed sessions should produce no orders.
2. Snapshot quality
   - `snapshot_quality.status`
   - `allows_new_entries`
   - `failed_symbol_count`
3. Indicator readiness
   - warmup `ready_ratio`
   - short-window backtests must still have non-zero warmup.
4. Selection routing
   - `selection.selections`
   - `alpha.input_selections`
5. Insights
   - `new_insights.insight_count`
   - active FLAT/DOWN insights should override UP exposure for the same symbol.
6. Portfolio output
   - allocation target count
   - target weights and cash weight
7. Risk decisions
   - rejected targets
   - freshness gates
   - regime exposure cap
8. Execution output
   - order intent count
   - session multipliers
   - min order notional and quantity delta

Do not jump straight from "no orders" to "alpha is broken". In this sleeve,
closed market sessions and degraded snapshots are common valid reasons for no
new entries.

## Snapshot Quality

LEaps currently blocks fresh entries when the market snapshot is degraded beyond
policy thresholds, while still allowing risk checks. That is usually the right
tradeoff: do not buy from partial data, but keep monitoring exits.

If snapshot failures persist during the regular session, investigate data
provider/cache quality before changing alpha logic.

Useful fields:

```text
snapshot_quality.status
snapshot_quality.complete_ratio
snapshot_quality.reasons
snapshot_quality.allows_new_entries
snapshot_quality.allows_risk_checks
```

## Backtest Hygiene

For strategy research, prefer non-overlapping windows:

- train window
- validation/search window
- out-of-sample backtest window
- short recent behavior check

Use daily backtests for daily alpha/RL training validation. Use minute or
intraday checks for execution behavior, turnover, stale tickets, and slicing.

Always inspect:

```text
warmup_data_slice_count
metrics_by_currency
order_count
turnover
max_drawdown
collisions
state_patch_count
```

For mixed KRW/USD runs, use `metrics_by_currency`; aggregate total equity is
not meaningful without an FX conversion layer.

## Live Sanity Checklist

Before relying on a changed sleeve in live operation:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/live_multi_sleeve.json
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/live_multi_sleeve.json --sleeve-id LEaps --summary-only
py -3 -m pytest -q tests/test_alpha_runtime.py tests/test_leaps_strategy_models.py tests/test_runtime_bootstrap.py
```

Then run one read-only `runtime-run-once` with the live runtime state path and
confirm the framework path is healthy before allowing submission.

## Operator Notes

- A closed session producing zero order intents is normal.
- A degraded snapshot blocking new entries is normal.
- State patches appearing in read-only mode prove model state wiring without
  mutating live state.
- Open tickets that cannot be cancelled are order-runtime/reconciliation
  issues, not alpha issues.
- If a model needs memory, use `StatePatch`; if it needs broker facts, ask the
  engine to project those facts into context.

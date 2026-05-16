# Runtime Cadence And Resolution

This document defines how LEapsQuantEngine keeps daily strategy logic stable
while the live engine may run on minute or quote cycles.

The goal is simple:

```text
live cycles can run often
daily strategy state should not mutate often
urgent safety checks still run every cycle
```

## Problem

Daily alpha models such as momentum, trend, ETF rotation, and daily trailing
stops should not be recomputed from every live quote. If a 20-day SMA receives a
minute bar, it stops being a 20-day SMA. If portfolio construction runs every
minute from a daily RL allocator, small snapshot changes can create unnecessary
turnover.

The engine therefore separates four concerns:

- data resolution: what kind of bar updated an indicator
- alpha cadence: how often each alpha model generates new insights
- insight persistence: how long a generated thesis remains active
- portfolio cadence: how often active insights become new target weights

Risk and execution remain cycle-based so safety checks and pending target
handling can continue even when daily alpha or portfolio stages are skipped.

## Current Contract

```mermaid
flowchart LR
    LiveQuote["Live quote/minute Bar<br/>resolution=live"] --> Registry["IndicatorRegistry"]
    DailyBar["Confirmed daily Bar<br/>resolution=daily"] --> Registry
    Registry --> Snapshot["IndicatorSnapshot"]
    Snapshot --> Alpha["AlphaRuntime<br/>per-alpha cadence"]
    Alpha --> InsightManager["InsightManager<br/>active until expiry"]
    InsightManager --> Portfolio["PortfolioConstruction<br/>rebalance cadence"]
    Portfolio --> Blend["PortfolioBlend<br/>optional target transition"]
    Blend --> Sizing["OrderSizing"]
    Sizing --> Risk["Risk every cycle"]
    Risk --> Execution["Execution every cycle"]
```

## Data Resolution

`Bar` and `DataSlice` carry a `resolution` field. Universe indicator
definitions can also declare the resolution they accept:

```json
{
  "name": "sma_20_close",
  "type": "sma",
  "period": 20,
  "field": "close",
  "resolution": "daily"
}
```

`IndicatorRegistry` checks the incoming bar before updating each indicator.

Working rules:

- `daily` indicators update from `daily` or `daily_confirmed` bars.
- `live` or `quote` indicators update from live, quote, minute, intraday,
  second, or tick bars.
- `any` preserves old behavior for smoke tests and intentionally generic
  indicators.
- Incoming bars stamped as `any`, `unknown`, or left blank do not update
  confirmed daily indicators. Adapters must identify the stream before it
  reaches the registry.

Provider defaults:

- daily history loaders stamp bars as `resolution="daily"`.
- live market snapshots stamp provider bars as `resolution="live"` when the
  provider did not specify one.
- snapshot worker reports expose `indicator_update_count` and
  `indicator_resolution_mismatch_count` so operators can see when a live cycle
  intentionally skipped confirmed daily indicator updates.

This means a live snapshot cannot accidentally advance a confirmed daily
momentum or SMA window.

## Opening And Extended Sessions

Opening-auction and extended-session rows are valid market context, but they are
not confirmed daily bars. Minute replay/cache rows may carry:

```text
market_session_phase
is_regular_market_open
is_orderable_session
is_extended_market_hours
```

Models can use these values to reason about expected open, gap risk, urgency,
or execution sizing. The indicator registry still relies on `resolution`, so a
pre-open minute row should remain `resolution="minute"` and must not advance
daily SMA, momentum, ATR, or volatility indicators.

The LEAN-like split is:

- subscription/session controls decide whether extended data enters the engine
- `Bar.metadata` says which session produced the row
- alpha/execution models explicitly opt into using that context
- confirmed daily indicators stay on daily/history data

If snapshot quality is `invalid`, `FrameworkRunner` does not run alpha or
portfolio construction for that cycle. Active insights are suppressed from that
cycle's portfolio input, but the insight ledger is not cancelled solely because
of a transient bad snapshot. This keeps contaminated current data out of new
orders without erasing a valid previous daily thesis.

## Indicator Readiness

Universe indicator definitions may mark a feature as optional for warmup:

```json
{
  "name": "roc_60_close",
  "type": "roc",
  "period": 60,
  "field": "close",
  "resolution": "daily",
  "readiness": "optional"
}
```

Working rules:

- `readiness="required"` is the default and participates in
  `required_warmup_bars`, symbol readiness, and `warmup_not_ready` entry
  gating.
- Legacy-style `required_for_warmup: false` is accepted as an alias for
  `readiness="optional"` when loading universe JSON.
- `readiness="optional"` indicators are still registered and updated when
  history exists, but they do not block live entries or warmup readiness by
  themselves.
- Warmup reports include required and optional readiness counts separately, so
  operators can distinguish a hard readiness failure from a missing research
  feature.
- Models should treat optional indicators as nullable context and fall back to
  shorter-horizon signals when the value is absent.

## Alpha Cadence

Python alpha modules may declare cadence metadata:

```python
ALPHA_ID = "leaps-kospi-conviction"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
```

Supported cadence values:

- `every_cycle`: run on every framework cycle.
- `once_per_day`: run at most once per calendar day per `alpha_id`.
- `every_5m` / `every_5_minutes`: interval cadence used mainly by portfolio
  construction.
- `manual`: do not run automatically after startup unless the runtime adds an
  explicit trigger later.

`AlphaRuntime` tracks `last_run_at` by `alpha_id`. When cadence is not due, it
does not call the alpha model. It still publishes an empty `InsightBatch` with:

```json
{
  "metadata": {
    "ran_alpha_ids": [],
    "skipped_alpha_ids": ["leaps-kospi-conviction"],
    "cadence_by_alpha": {
      "leaps-kospi-conviction": "once_per_day"
    }
  }
}
```

Skipping an alpha does not close positions by itself. `InsightManager` keeps
previous insights active until their `expires_at` time.

## Portfolio Cadence

Runtime config controls portfolio target rebuild cadence:

```json
{
  "portfolio": {
    "model": "portfolios/rl_ppo_constructor.py",
    "rebalance": {
      "cash_reserve_pct": 0.0,
      "min_order_notional": 0.0,
      "min_quantity_delta": 1,
      "cadence": "every_5_minutes"
    }
  }
}
```

When cadence is due, `PortfolioConstructionEngine` builds a fresh
`PortfolioTargetBatch` from active insights and current virtual portfolio
state.

When cadence is not due, `FrameworkRunner` reuses the previous target batch's
allocation targets and marks it:

```json
{
  "metadata": {
    "reused": true,
    "source_batch_id": "portfolio-targets-..."
  }
}
```

`OrderSizingEngine` does not trust stale quantity plans from the reused batch.
It reads the persisted `target_percent` values, then recomputes
`desired_value`, `target_quantity`, and `delta_quantity` from the current
virtual portfolio, current mark price, current cash/equity, and current
rebalance policy every cycle.

Risk and execution still run every cycle using those freshly sized quantity
targets. This gives the engine a stable target state instead of interpreting
"no new daily alpha this minute" as "sell everything", while still responding
to fills, cash changes, and price/equity changes.

## Portfolio Blend

Portfolio Blend is an optional operational transition layer after target
resolution and before order sizing.

It is meant for this situation:

```text
old complete target snapshot -> new complete target snapshot
```

It is not implemented as "run old Python model plus new Python model." The
engine stores the previous committed target percentages, compares them to the
current resolved `PortfolioTargetBatch`, and linearly moves from the old weights
to the new weights over `portfolio.blend.duration_minutes`.

Target resolution happens first:

```text
portfolio model raw output
  -> PortfolioTargetResolver
  -> resolved complete target vector
  -> PortfolioBlendEngine
```

Default `portfolio.target_resolution.mode="complete"` means an omitted old or
held symbol is resolved to an explicit 0% target before blend when the new raw
batch contains at least one target. That makes model migrations cross-fade
naturally: old-only symbols fade out, new-only symbols fade in, and shared
symbols move from old weight to new weight. An empty raw batch is treated as
`empty_no_action` by default so a missing/expired insight set does not become an
implicit all-sell signal; models that truly want all-cash should emit explicit
0% targets or opt into `zero_missing_when_raw_empty=true`. Use `mode="patch"`
only for a portfolio model that intentionally emits partial patches; in that
mode omitted previous targets are carried forward before blend.

Runtime config:

```json
{
  "portfolio": {
    "target_resolution": {
      "mode": "complete"
    },
    "blend": {
      "enabled": true,
      "duration_minutes": 300,
      "target_drift_threshold_pct": 0.08,
      "clock": "orderable_session"
    }
  }
}
```

Working rules:

- `target_drift_threshold_pct` is an L1 target-weight drift threshold. A 4%
  decrease in one symbol and 4% increase in another is `0.08`.
- `clock="orderable_session"` advances only when the current market session is
  orderable. `regular_session` requires regular market open, and `wall_time`
  advances on elapsed cycle timestamps.
- Active blend state is written through runtime state under
  `engine-portfolio-blend / active_transition`.
- `FrameworkRunner` can advance an active blend on non-due portfolio cadence
  cycles without re-running the portfolio model.
- Tags containing `:flat`, `:down`, `stop`, `urgent`, `manual`, `operator`,
  `force`, `risk`, `no_longer_in_target_portfolio`, or
  `missing_target_zero` bypass the blend for that symbol. These exits should
  not be slowed by an operational transition.
- Retargeting during an active blend preserves the active transition clock. A
  changed destination target can update `to_weights`, but the original
  `started_at`, elapsed time, and deadline remain in force.
- Portfolio Blend does not decide whether a missing target means carry-forward
  or zero. That decision belongs to `PortfolioTargetResolver`.

Order sizing still recomputes quantities from the blended percentages, current
virtual account, current cash/equity, and current prices every cycle.

## Process Boundary State

The current live PowerShell order loop starts a fresh Python process for each
`runtime-run-multi-once`. Plain in-memory cadence state is therefore not enough
for live operation. Use a framework state directory so each sleeve keeps its own
cadence and active insight state:

```powershell
py -3 -m leaps_quant_engine.cli runtime-run-multi-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --framework-state-dir data/runtime/framework-state/multi-sleeve `
  --order-batch-output data/runtime/live-order-loop/multi_sleeve_candidate_orders.json
```

The state file persists:

- active insights
- alpha last-run timestamps
- last portfolio run timestamp
- last portfolio target batch

Operator/reporting commands may pass `--framework-state-read-only` so they can
inspect the current target state without advancing cadence or changing the live
state file.

Stateful models need one more store. Pass `--runtime-state` to attach the
SQLite model-state store:

```powershell
py -3 -m leaps_quant_engine.cli runtime-run-multi-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --framework-state-dir data/runtime/framework-state/multi-sleeve `
  --runtime-state data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --order-batch-output data/runtime/live-order-loop/multi_sleeve_candidate_orders.json
```

Framework state answers "what target thesis is currently active?" Runtime
model state answers "what stateful model memory should survive restart?" Keep
them separate.

Portfolio Blend uses runtime model state for active transition progress. The
framework state still persists the latest target batch so reporting and
cadence reuse stay readable across process boundaries.

## Market Session Gate

Runtime cycles may include multiple market scopes. The framework passes
normalized `MarketSession` objects into execution context, and order submit
guards use the same session metadata before touching KIS.

Working rules:

- Daily alpha/portfolio cadence is independent of orderable sessions.
- Execution models may choose whether to emit regular, pre-market, or
  after-hours order intents.
- Broker/session capability checks remain core guards.
- KRX holiday and US holiday handling should be added as market-calendar
  inputs to the session layer; do not encode holiday assumptions inside alpha
  or portfolio models.

## Warmup Sources

Live warmup should prefer cache/history providers and produce confirmed daily
indicator state before the first live cycle. FinanceDataReader is used as a
daily fallback provider in the current adapter stack; it should not be treated
as the v0 source of 30-day minute bars.

For minute simulation:

- Use existing minute replay files when available.
- Use `download-us-minute-feed` for US minute research feeds, understanding
  that public providers may limit how much 1-minute history is available.
- For deterministic multi-day minute replay, prefer our own live collector or
  KIS/cache artifacts over ad-hoc downloads.

## Exit And Safety Path

Daily cadence must not be the only exit path.

Use these layers for urgent behavior:

- Risk model: always-on clamps, exposure limits, oversell prevention, stale
  snapshot entry blocks, and emergency reduce/flat rules.
- Quote or intraday alpha: explicit `every_cycle` model for live stop logic when
  the strategy truly needs quote-level reassessment.
- Execution/order runtime: ticket lifecycle, duplicate submit protection,
  pending order awareness, and broker fill reconciliation.

Daily alpha and daily portfolio cadence are for strategy thesis updates, not
for all safety behavior.

## LEaps Current Settings

The live config `configs/runtime/live_multi_sleeve.json` currently uses:

```text
LEaps alpha:
  leaps-kospi-conviction         -> every_cycle, daily
  leaps-volatility-trailing-stop -> every_cycle, daily

LEaps portfolio:
  rl_ppo_constructor.py
  rebalance.cadence = every_5_minutes
  blend.enabled = true
  blend.duration_minutes = 300
  blend.clock = orderable_session

LEaps indicators:
  configs/universes/leaps_kr_research_core.json
  strategy indicators are resolution=daily
```

`LEaps` and `us_etf_rotation` run in the same live runner, but each sleeve keeps
separate cash currency, account route, alpha, portfolio, risk, execution, and
framework state.

## Operator Checklist

Before live open or after restart:

1. Warm daily indicators from cache/history.
2. Confirm snapshot quality is not blocked by `warmup_not_ready`.
3. Confirm daily alpha models are configured with `EVALUATION_CADENCE`.
4. Confirm portfolio rebalance cadence matches the strategy horizon.
5. Confirm urgent exits are covered by risk or explicitly live-resolution
   models.

When changing a daily alpha parameter:

1. Edit the alpha module or config.
2. Trigger the controlled reload path or restart the bounded runtime process.
3. Let bootstrap/warmup rebuild the indicator snapshot.
4. Dry-run one cycle and inspect `stage_decisions.alpha` and
   `stage_decisions.portfolio`.

## Backtest Expectations

Backtests should preserve the same contract:

- daily historical feed uses `resolution="daily"`
- indicator readiness is warmed before the evaluation period when needed
- alpha cadence is honored by `AlphaRuntime`
- portfolio target persistence is honored by `FrameworkRunner`
- sizing recomputes current quantities from persisted target percentages
- risk and execution still run every replay cycle

If a backtest uses minute bars with daily indicators, it must either provide a
daily consolidator or keep those daily indicators from consuming minute bars.
Do not silently mix the streams.

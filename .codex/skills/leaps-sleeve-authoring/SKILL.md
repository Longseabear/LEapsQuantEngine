---
name: leaps-sleeve-authoring
description: Use when creating, reviewing, or updating LEapsQuantEngine sleeve workspaces, sleeve-local selection/alpha/portfolio/risk/execution models, runtime config wiring, model state, tests, and agent handoff docs. Includes how to make a new sleeve, how the engine runs sleeve models, and the requirement that each sleeve has a stable fixed workspace_path.
---

# LEaps Sleeve Authoring

## Purpose

Use this skill when implementing or reviewing a LEaps sleeve. The goal is to
keep every sleeve LEAN-like: strategy code emits model outputs, while the engine
owns runtime orchestration, order lifecycle, broker adapters, and virtual
account mutation.

If the task is mainly a backtest, also use `leaps-backtesting`. If the task is a
portfolio/operator report, also use `leaps-reporting`.

## Engine Shape

The engine pipeline is:

```text
UniverseSelection
  -> Indicator/SnapshotContext
  -> AlphaRuntime / InsightManager
  -> PortfolioConstruction
  -> target resolution / optional portfolio blend
  -> OrderSizing
  -> RiskManagement
  -> EngineGuard
  -> ExecutionModel
  -> OrderIntent / OrderTicket / OrderEvent
  -> virtual account portfolio mutation from fills
```

Sleeve models must not skip layers. In particular, alpha models do not create
targets or orders, portfolio models do not emit quantities, and execution models
do not call broker APIs.

## Fixed Workspace Rule

Every sleeve has a stable workspace directory, normally:

```text
sleeves/<sleeve_id>/
```

The runtime config must set:

```json
{
  "sleeve_id": "my_sleeve",
  "workspace_path": "sleeves/my_sleeve"
}
```

Treat `workspace_path` as part of the public contract. Relative module refs such
as `alphas/momentum.py` and `portfolios/equal_weight.py` resolve inside that
workspace. Do not rely on the process current directory, sibling sleeve paths,
or ad hoc absolute local paths.

Changing files inside the workspace is not enough to update a running live
process. Runtime reload happens at a controlled cycle boundary through runtime
config/control, followed by preflight or a dry-run cycle.

Do not assume runtime artifact paths from the workspace. Strategy files live in
`sleeves/<sleeve_id>/`; live state lives under runtime/account/order stores
chosen by the runtime config. Before inspecting logs, framework state, order
stores, report files, or account stores, ask the engine for the current artifact
map:

```powershell
py -3 -m leaps_quant_engine.cli runtime-artifact-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id <sleeve_id> `
  --summary-only
```

This command is read-only and does not run models, call KIS, submit orders, or
mutate virtual accounts.

## Workspace Layout

Prefer this structure:

```text
sleeves/<sleeve_id>/
  AGENTS.md
  README.md
  selections/
  alphas/
  portfolios/
  risks/
  executions/
```

Runtime artifacts, live state, broker payloads, and generated reports belong
under `data/` or ignored runtime paths, not in the sleeve workspace unless they
are intentional samples.

Each sleeve `AGENTS.md` should state:

- what the sleeve trades
- active selection, alpha, portfolio, risk, and execution models
- state namespaces owned by the sleeve models
- known operational constraints
- validation commands

## New Sleeve Checklist

1. Choose a stable `sleeve_id`.
   Prefer lowercase hyphen/underscore ids for new sleeves. Existing legacy ids
   may differ, but do not rename a live sleeve casually.

2. Create a fixed workspace under `sleeves/<sleeve_id>/`.
   Add `AGENTS.md` before adding complicated model code.

3. Create or choose a universe file under `configs/universes/`.
   Include metadata such as market, asset type, currency, exchange, and ETF
   flags when relevant.

4. Add a sleeve block to a runtime config.
   Include `workspace_path`, `cash_by_currency`, `broker_account_routes`,
   universe settings, indicators/warmup, alpha modules, alpha
   `input_selections`, portfolio model, target resolution, risk model,
   execution model, and worker cadence.

5. Implement the smallest useful vertical slice.
   Start with one selector, one alpha, one portfolio model, one risk model, and
   one execution model. Keep the first backtest boring and easy to inspect.

6. Verify module loading.

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli sleeve-alpha-list configs/runtime/live_multi_sleeve.json --sleeve-id <sleeve_id>
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/live_multi_sleeve.json --sleeve-id <sleeve_id> --summary-only
```

7. Backtest the sleeve in isolation.

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/live_multi_sleeve.json `
  --sleeve-id <sleeve_id> `
  --start 2026-05-01 `
  --end 2026-05-08 `
  --warmup-start 2026-04-01 `
  --cash 2000000 `
  --currency KRW `
  --source finance-datareader `
  --include-insights `
  --summary-only
```

Research backtests should be sleeve-isolated. Live/paper runtime may run
multiple sleeves in one runner.

## Model Contracts

### Selection

Selection models define which symbols an alpha receives. They should rank or
filter symbols and return a stable `selection_id`. They should not decide order
quantities or mutate portfolio state.

The engine must still keep operational symbols visible: held symbols,
open-order symbols, exit-watch symbols, and manual/operator symbols must remain
in the live universe even if fresh selection rejects them.

### Alpha

Alpha models consume `SnapshotContext` and emit `Insight` records only.

Rules:

- loop over `context.symbol_keys`
- use `context.value(...)`, `context.metadata(...)`, fundamentals, and
  `context.model_state`
- set `ALPHA_ID`, `VERSION`, and usually `EVALUATION_CADENCE` and
  `INPUT_RESOLUTION`
- use `UP` for bullish/long desire
- use `FLAT` for explicit exit, zero target, stop, or reduce-to-flat intent
- use `DOWN` only when the portfolio/risk stack is expected to support short
  exposure, otherwise treat it as exit/reduce semantics
- do not call KIS, broker-engine, market-data-engine, yfinance, files, or
  external APIs directly

If the sleeve uses temporal PPO, alpha models must pass through the engine-made
window:

```python
rows = context.metadata_value(symbol_key, "rl_temporal_features")
metadata = dict(base_metadata)
if isinstance(rows, (list, tuple)) and rows:
    metadata["rl_temporal_features"] = list(rows)
```

The alpha should still decide whether the symbol has an UP insight. Temporal PPO
must stay alpha-gated; do not let the portfolio model scan the universe without
an active insight.

Different alpha models may disagree on a symbol. That is normal. Portfolio
construction owns conflict resolution, such as `FLAT/DOWN` overriding `UP` for
stop or exit signals.

### Portfolio

Portfolio construction consumes active insights plus current virtual portfolio
state and emits percentage targets.

Rules:

- emit `PortfolioAllocationTarget` or the configured target type
- emit target percentages, not share quantities
- read current holdings through the context, but do not mutate them
- decide whether the model is complete-target or patch-target and set
  `portfolio.target_resolution.mode` accordingly
- if a held symbol should exit, emit an explicit `0.0` target or rely on a
  documented complete-target resolver
- if using Portfolio Blend or smoothing, keep the state under
  `context.model_state`; do not hide it in module globals

For live sleeves, explicitly test how the portfolio model handles:

- selected UP insight
- unselected held symbol
- same-symbol UP plus FLAT
- stale/no alpha cycle
- small cash and whole-share rounding

### Risk

Risk models approve, reject, or clamp sized targets. Strategy risk belongs here:
max exposure, concentration, drawdown policy, daily loss limits, stale-data
preference, or stop/reduce policy.

Core safety guards are not sleeve risk models. Oversell prevention, cash
reservation, broker route validation, idempotency, market session validity, tick
size, and whole-share checks belong to the engine guard/broker layer.

### Execution

Execution models convert approved targets into `OrderIntent` records. They may
choose market vs limit, limit offsets, slicing, urgency, session behavior, and
cancel/replace policy metadata.

Execution models still do not submit broker orders. Broker adapters and order
runtime own tickets, polling, fills, cancellation, replacement, and
reconciliation.

Order intents do not mutate holdings. Portfolio state moves only through
`OrderEvent` fills applied by the virtual account. The order runtime exposes
recent `portfolio_mutations`, so sleeve authors should debug actual ownership
through lifecycle reports rather than assuming a target or intent was filled.

## State

Use `context.model_state` and `StatePatch` for model-owned state. State is
sleeve-namespaced by design.

Prefer a bound scope for non-trivial state:

```python
trail = context.model_state.scope(
    model_id="volatility_trailing_stop",
    namespace="trailing_stop",
).for_symbol(symbol_key)

state = trail.object_get(default={"high_watermark_price": 0})
patch = trail.object_merge({"high_watermark_price": next_high}, reason="mark")
```

Use `.for_position(position_id)` when the state should follow a position
instance. The model still returns `StatePatch`; the runtime commits it after a
successful cycle in backtest, research, paper, and live.

Good model state examples:

- trailing-stop high watermark
- portfolio target smoothing or lerp anchor
- blend transition anchor
- daily loss/drawdown risk counters
- execution chase/replace memory

Do not write state files manually from sleeve models. Do not store state in
module globals for live behavior; process restarts must be recoverable from the
runtime state store.

## Market And Cash Rules

Keep currencies separate. Do not mix KRW and USD as one spendable cash pool in
v0. If one logical sleeve trades multiple markets, configure
`broker_account_routes` and let route-specific cash/holdings stay separate.

Example:

```json
{
  "broker_account_routes": {
    "domestic": "kis-domestic",
    "overseas": "kis-overseas"
  },
  "cash_by_currency": {
    "KRW": 2000000,
    "USD": 2500
  }
}
```

For a new sleeve, prefer one market and one currency first. Add mixed-market
routing only after the single-route sleeve works in backtest and preflight.

## Cadence And Warmup

Daily models may run every cycle if their input indicators are confirmed daily
and resolution-gated. Do not let minute bars advance daily SMA, momentum, ATR,
or volatility indicators by accident.

Always separate warmup from evaluation in backtests. Short windows without
warmup can make valid alpha models appear inactive.

Temporal PPO needs more than current indicator readiness. Runtime/backtest
bootstrap can attach point-in-time daily `rl_temporal_features` windows to the
snapshot context when the portfolio config uses a temporal `feature_schema`, but
the sleeve still needs enough daily history before the first evaluated cycle.
Use at least 84 daily bars for `v2_temporal` and at least 144 daily bars for
`v2_temporal_residual`. Minute replay uses the daily history provider for those
windows; minute bars do not update confirmed daily temporal features.

Portfolio cadence is separate from alpha cadence. Skipping alpha for cadence
does not mean exit all positions; active insights remain alive until expiry.
Risk and execution can still run every cycle against the latest persisted target
batch.

## Testing Expectations

Add or update tests when model behavior changes. Prefer focused tests in
`tests/test_<sleeve>_sleeve.py` or an existing sleeve test file.

Minimum useful tests:

- workspace model files load from `workspace_path`
- alpha emits valid insights and never emits orders
- alpha input selections restrict `context.symbol_keys`
- FLAT/DOWN exits override or block same-symbol UP as intended
- portfolio emits expected target percentages
- risk explains clamps/rejections
- execution emits order intents with expected order type/session metadata

For Python behavior changes, run:

```powershell
py -3 -m pytest -q
```

For skill/docs-only changes, validate the markdown/frontmatter and say pytest
was not run because no Python behavior changed.

## Handoff Template

When finishing sleeve work, report:

```text
Sleeve:
Workspace:
Changed model folders:
Runtime config touched:
State namespaces:
Backtest/preflight run:
Live reload needed:
Risks/follow-ups:
```

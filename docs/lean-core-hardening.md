# LEAN Core Hardening

This document describes the current LEAN-style hardening layer for
LEapsQuantEngine. The goal is operational explainability:

```text
why a symbol was selected
why it produced an insight
why it became a target
why risk approved or rejected it
why execution emitted an order intent
why a broker ticket/fill changed the sleeve portfolio
```

The hardening layer is intentionally conservative. It does not add a new daemon
and it does not force strategy behavior into every sleeve. Strategy policy stays
inside opt-in models. Always-on engine safety stays in core guards.

## Implemented Surfaces

### Runtime Heartbeat And Supervisor Health

Live component liveness is read from explicit artifacts, not from Windows PID or
terminal command-line scans.

Primary module:

- `leaps_quant_engine.runtime_heartbeat`

The multi-sleeve live order loop writes:

```text
data/runtime/live-order-loop/multi_sleeve_heartbeat.json
```

Portfolio report loops and the EOD snapshot scheduler write the same heartbeat
shape beside their state/log files. The heartbeat includes `runtime_id`,
`component`, `status`, `updated_at`, `config_path`, `sleeve_ids`,
`cycle_index`, and metadata such as the current loop phase. `process_id` may be
recorded for operator context, but health evaluation does not check whether that
PID exists. This keeps supervisor checks bounded and avoids turning process
enumeration into a startup bottleneck.

Use:

```powershell
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --heartbeat data/runtime/live-order-loop/multi_sleeve_heartbeat.json `
  --heartbeat-component multi_sleeve_live_order_loop
```

`tools/leaps_safe_start_live_stack.ps1` now uses:

- HTTP `/health` for KIS gateway and broker-engine
- heartbeat freshness for the multi-sleeve live loop, report loops, and EOD
  scheduler
- strict preflight for config/routes/order status

Command-line process scanning is opt-in only through `-UseProcessScan true`.
Normal startup should stay on HTTP health + heartbeat artifacts.

### Portfolio Mutation Audit

Portfolio state changes from fills, not order intents.

`VirtualSleeveAccountStore` keeps the existing mutating methods:

```python
store.apply_order_event(event)
store.apply_fill(fill)
```

For audit/reporting, use the report-returning variants:

```python
report = store.apply_order_event_with_report(event)
report = store.apply_fill_with_report(fill)
```

The report includes:

- whether the fill was applied
- duplicate-fill status
- before/after cash
- before/after quantity
- before/after average price
- fee
- estimated realized PnL for sells
- `order_intent_id`
- `ticket_id`
- `event_id`
- `fill_id`

Primary classes:

- `leaps_quant_engine.virtual_account.PortfolioMutationRecord`
- `leaps_quant_engine.virtual_account.FillApplicationReport`

This gives operators and agents a deterministic answer to "what changed this
sleeve portfolio?"

`MultiSleeveOrderOrchestrator` now keeps the same audit surface when it applies
broker/order events. Its result exposes `fill_application_reports` and
`portfolio_mutations`, so a live/paper submit cycle can explain the actual
portfolio mutation without re-reading raw account files. `order-runtime-status`
also includes recent per-sleeve portfolio mutations for operator/debug use.

### Lineage Summary

Cycle output and cycle journals include a symbol-level lineage summary.

```python
from leaps_quant_engine.lineage import build_cycle_lineage_summary

summary = build_cycle_lineage_summary(
    cycle,
    order_tickets=tickets,
    order_events=events,
    portfolio_mutations=mutations,
)
```

The lineage builder links existing ids where available:

```text
Insight
  -> PortfolioTarget
  -> RiskDecision
  -> OrderIntent
  -> OrderTicket
  -> OrderEvent
  -> PortfolioMutation
```

No-order cycles still summarize cleanly. A missing link means the stage did not
produce that artifact, not that the report failed.

Primary module:

- `leaps_quant_engine.lineage`

### Unordered Quantity / Target Progress

Fast risk and execution cycles must not depend on slowing the framework clock.
The engine exposes working order state as an immutable execution-context read
model:

- open buy quantity by symbol
- open sell quantity by symbol
- remaining quantity for partial tickets
- reserved buy notional
- pending ticket ids, latest status, and oldest open-order age

The standard execution path computes unordered quantity before creating normal
order intents:

```text
projected_quantity = current_quantity + open_buy_quantity - open_sell_quantity
unordered_delta = target_quantity - projected_quantity
```

This gives the LEAN-like behavior expected from target collection progress:

- target 100, holding 70, open buy 30 -> no new normal order intent
- target 100, holding 70, open buy 10 -> buy only 20
- a reused target batch with unchanged state does not create duplicate intent
- urgent stop/risk exits may bypass normal churn suppression but still pass
  through order runtime and guards

Primary module:

- `leaps_quant_engine.execution.PendingOrderState`

### Market Calendar

The runtime now has calendar/session reports for:

- `domestic` / KRX
- `overseas` / US

Built-in rules cover weekends, regular session, and extended-session phases.
Default holiday JSON files live under `configs/market-calendars/` and are used
when no explicit holiday file is passed. Optional override files use the same
shape:

```json
{
  "holidays": ["2026-05-05"]
}
```

If a holiday file is missing, the calendar remains usable but reports degraded
quality with a warning. This is intentional: the engine can still say "weekend
closed" without pretending holiday accuracy is complete.

Primary module:

- `leaps_quant_engine.market_calendar`

Runtime integration:

- `runtime-preflight` includes `market_calendar` checks.
- `runtime-health` includes `market_calendar` checks when route scope is known.
- Both reports also emit `market_session_gate`, which says whether the current
  session is trading/orderable for the route.
- In strict live preflight, a non-trading or non-orderable session is a
  warning. The service may still start so it can heartbeat and supervise order
  state, but the live loop must not run models or submit orders for closed
  markets.
- The multi-sleeve live loop also checks weekends and configured market holidays
  inside its sleeve schedule gate before running models or submitting orders.
- Runtime session estimates use the calendar layer instead of raw synthetic
  session helpers.

Calendar reports are status/gating inputs. They should not be hard-coded inside
alpha or portfolio models.

### Security Catalog

`SymbolProperties` describes broker-relevant symbol behavior:

- market scope
- currency
- lot size
- quantity step
- tick rule
- default domestic exchange scope
- supported sessions
- overseas order exchange

`SecurityCatalog` resolves those properties from universe metadata and defaults.

Example universe metadata:

```json
{
  "symbols": ["KRX:005930", "US:SMH"],
  "symbol_properties": {
    "KRX:005930": {
      "market_scope": "domestic",
      "currency": "KRW",
      "lot_size": 1,
      "quantity_step": 1,
      "default_exchange_scope": "SOR"
    },
    "KRX:069500": {
      "market_scope": "domestic",
      "currency": "KRW",
      "asset_type": "etf",
      "is_etf": true,
      "default_exchange_scope": "KRX"
    },
    "US:SMH": {
      "market_scope": "overseas",
      "currency": "USD",
      "lot_size": 1,
      "quantity_step": 1,
      "order_exchange": "NASD"
    }
  }
}
```

Domestic KRX stocks default to `SOR`. Domestic ETF/ETN/ELW symbols default to
`KRX`, because KIS/NXT best-execution routing is not available for every listed
product class. Explicit `KRX`, `NXT`, or `SOR` metadata can override the default
when a route has been tested. Broker submit validation and engine guard use symbol
properties to reject invalid quantity steps, unsupported sessions, and invalid
venue metadata before a live broker call.

Primary module:

- `leaps_quant_engine.security`

### Active Universe Cadence

Active universe refresh is separate from alpha cadence and portfolio cadence.

Runtime config:

```json
{
  "universe": {
    "active": {
      "cadence": "startup_only",
      "selection_model": "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel"
    }
  }
}
```

Supported values:

- `startup_only`
- `once_per_day`
- interval aliases such as `every_5m` and `every_5_minutes`

The runtime persists active-universe state under:

```text
model_id = engine-universe-selection
namespace = active_universe
```

When cadence is due, `RuntimeSleeveRuntime.refresh_active_universe_if_due(...)`
reruns selection and swaps the `BackgroundSnapshotWorker` universe at a cycle
boundary.

The forced-live invariant always remains:

```text
live_universe =
  selected_active_symbols
  + held_symbols
  + open_order_symbols
  + exit_watch_symbols
  + manual/operator symbols
```

Selection cadence must never hide held or pending symbols from monitoring.

### Transaction Costs

Backtests keep simulated costs:

- `ZeroFeeModel`
- `FixedRateFeeModel`
- `KisFeeModel`
- `ZeroSlippageModel`
- `FixedBpsSlippageModel`

Live/paper execution sync preserves actual broker costs when KIS or broker
payloads include them. The parsed cost is stored as:

```text
VirtualFillEvent.fee
VirtualFillEvent.metadata.fee_components
VirtualFillEvent.metadata.transaction_costs
```

`VirtualFillEvent.fee` is the amount applied to sleeve cash. Simulated estimates
must not overwrite actual broker costs.

Primary class:

- `leaps_quant_engine.transactions.TransactionCostSummary`

### Model State Helpers

`RuntimeStateStore` remains the single model-state backend. No new persistence
backend was added.

Models can use `context.model_state` helper methods:

```python
state = context.model_state.object_get(
    model_id="trailing-stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
)

patch = context.model_state.object_merge(
    {"high_watermark_price": 84000},
    model_id="trailing-stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
    reason="trailing_stop_mark",
)
```

Available helpers:

- `object_get`
- `object_entries`
- `object_set`
- `object_merge`
- `object_delete`
- `patch`
- `scope`

Prefer `context.model_state.scope(...)` when a model repeatedly touches the
same sleeve/model/namespace:

```python
trail = context.model_state.scope(
    model_id="trailing-stop",
    namespace="trailing_stop",
).for_symbol("KRX:005930")

state = trail.object_get(default={"high_watermark_price": 0})
patch = trail.object_merge(
    {"high_watermark_price": max(state["high_watermark_price"], latest_price)},
    reason="trailing_stop_mark",
)
```

Patches are committed by `FrameworkRunner` only after a successful cycle when a
runtime state store is attached.

### Opt-In Risk Examples

Two example risk models are available:

- `DailyLossLimitRiskModel`
- `MaxDrawdownRiskModel`

They demonstrate model-owned risk state through `RuntimeStateStore`.

They are not always-on engine guards. A sleeve must configure them explicitly.
They reject new/increasing targets after the configured circuit breaker trips
while still allowing reductions.

## Responsibility Boundary

Use this split when implementing new features.

Model-owned state:

- trailing stop high watermarks
- portfolio blend or lerp anchors
- daily loss baseline
- drawdown peak
- strategy-specific stale-data tolerance
- strategy-specific reduce/flat rules

Core guard:

- oversell prevention
- cash and reserved quantity checks
- unsupported broker route block
- unsupported session block
- duplicate submit/idempotency
- missing price validation
- account route mismatch
- invalid symbol quantity step or lot size

This is the LEAN-style rule: the model expresses strategy intent, while the
engine prevents unsafe or unreplayable side effects.

## Runtime Commands

Run preflight with order status:

```powershell
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/live_multi_sleeve.json `
  --include-order-status `
  --strict-live
```

Run health:

```powershell
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --include-order-status `
  --summary-only
```

Run a multi-sleeve cycle with state:

```powershell
py -3 -m leaps_quant_engine.cli runtime-run-multi-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --framework-state-dir data/runtime/framework-state/multi-sleeve `
  --runtime-state data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --order-batch-output data/runtime/live-order-loop/multi_sleeve_candidate_orders.json
```

Use `--framework-state-read-only` for reporting commands that should inspect
the latest target state without advancing cadence.

## Verification

Focused test areas:

- `tests/test_virtual_account.py`
- `tests/test_lineage.py`
- `tests/test_market_calendar.py`
- `tests/test_security_catalog.py`
- `tests/test_engine_guard.py`
- `tests/test_account_sync.py`
- `tests/test_runtime_state.py`
- `tests/test_risk.py`
- `tests/test_runtime_bootstrap.py`
- `tests/test_brokerage.py`

Full verification:

```powershell
py -3 -m pytest -q
```

Last verified in this workspace:

```text
553 passed, 1 warning
```

## Open Follow-Ups

- Production daemon/service packaging remains separate from this hardening
  layer.
- Rich cancel/replace policy should remain model-driven through execution
  policy and order runtime lifecycle actions.
- Holiday accuracy depends on maintained holiday JSON artifacts.
- Universe daily refresh rollout for live sleeves should be a config decision,
  not a hidden code default.

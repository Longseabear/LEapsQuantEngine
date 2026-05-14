# Runtime State

Runtime state is the engine-owned persistence surface for optional model state.
It is wired into runtime cycles only when an explicit runtime state store is
provided. Stateless models remain the default.

## LEAN-Style Principle

State is allowed, but ownership must stay clear.

```text
market data + orders + fills
  -> deterministic engine context
  -> model decision
  -> optional StatePatch
  -> runtime commits state at a cycle boundary
```

Models should not write runtime state directly. A stateful model returns
`StatePatch` records, and the runtime validates and commits them. Stateless
models return no patches and remain the default.

The current runtime supports this through `context.model_state` plus optional
model `state_patches(...)` hooks:

```text
context.model_state.get(...)
model.generate/create_targets/manage_risk/create_orders(...)
model.state_patches(...)
FrameworkRunner commits patches after the cycle succeeds
```

## State Ownership

Engine-owned state:

- cash
- holdings
- open orders
- order events
- fills
- reservations
- position lifecycle

Model-owned state:

- trailing stop high watermark
- cooldown windows
- previous target weights
- target smoothing anchors
- execution chase/replace memory
- last rebalance metadata

The engine stores model-owned state, but it does not interpret the strategy
meaning of that state.

## Current Foundation

The offline foundation lives in
`src/leaps_quant_engine/runtime_state.py`.

It provides:

- `ModelStateKey`: sleeve/model/namespace/symbol/position namespacing.
- `StatePatch`: `merge`, `set`, or `delete` model state requests.
- `ModelStateRecord`: current projected state for a key.
- `ModelStateEvent`: append-style audit event for every patch.
- `InMemoryRuntimeStateStore`: test/backtest-friendly implementation.
- `SQLiteRuntimeStateStore`: local SQLite-backed implementation.

The SQLite store is intentionally separate from current live JSON/JSONL stores
and is used only when runtime commands receive `--runtime-state`.

## Trailing Stop Example

A trailing stop model should not own broker state or mutate the virtual account.
It should read a context value and return a patch:

```python
state = context.model_state.get(
    model_id="volatility_trailing_stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
    position_id=position_id,
)

previous_high = state.value.get("high_watermark_price", entry_price) if state else entry_price
new_high = max(previous_high, current_price)
stop_price = new_high * 0.94

patch = StatePatch(
    key=key,
    value={
        "high_watermark_price": new_high,
        "last_price": current_price,
        "stop_price": stop_price,
    },
    reason="trailing_stop_mark",
)
```

The model decides what the high watermark means. The runtime only stores the
patch and provides the projected state on the next cycle.

## Storage Direction

The preferred long-term storage shape is:

```text
SQLite runtime DB
  model_state          current projection
  model_state_events   append audit of patches
  order_events         order lifecycle
  fills                broker/application fills
  cash_ledger          cash transfers/sync
  cycle_journal        cycle summaries
```

EOD snapshots can still export JSON artifacts for review, but the runtime source
of truth should become a compact local database rather than a growing file set.

## Live Safety

Do not connect this state store to live runtime implicitly. A live loop must pass
`--runtime-state` or a tool parameter such as `RuntimeStatePath`.

The live integration path is:

1. Prove state patches in tests, backtests, or paper runs.
2. Run live shadow mode with `--runtime-state-read-only` if needed.
3. Inspect `framework.model_state`, cycle journal patch/event counts, and the
   SQLite `model_state_events` audit.
4. Enable writes by removing read-only mode.
5. Keep rollback simple: remove the runtime-state argument and stateless models
   continue unchanged.

Runtime commands:

```powershell
py -3 -m leaps_quant_engine.cli runtime-run-once `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --framework-state data/runtime/framework-state/LEaps.json `
  --runtime-state data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --summary-only

py -3 -m leaps_quant_engine.cli runtime-run-multi-once `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --framework-state-dir data/runtime/framework-state/multi-sleeve `
  --runtime-state data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --summary-only
```

## After-Close Checklist

Do the live-facing integration only after the market is closed or the live loop
is intentionally stopped.

1. Capture the end-of-day snapshot first:
   - portfolio report
   - order runtime status
   - virtual account store copy
   - cycle journal tail
   - open ticket count

2. Confirm live is idle:
   - no open tickets
   - no pending broker submit process
   - no unallocated fills
   - latest virtual account cash/holdings match expected fills

3. Run a read-only state bootstrap:
   - derive initial model state from existing virtual `PositionState`
   - do not overwrite virtual account holdings
   - write bootstrap output to a temporary SQLite file
   - inspect records for sleeve/model/symbol/position namespacing

4. Add or enable stateful models in shadow mode first:
   - read state through immutable model context
   - return model `StatePatch` outputs
   - use `--runtime-state-read-only` when only observing
   - include state patch counts in cycle journal/report output

5. Prove with replay:
   - run backtest with in-memory store
   - run shadow/paper cycle with SQLite store
   - verify trailing stop high-watermark updates are deterministic
   - verify stateless models emit no patches and behave unchanged

6. Enable paper commit:
   - commit `StatePatch` records only after a successful framework cycle
   - record `ModelStateEvent` audit rows
   - verify restart reloads the same state projection

7. Enable live commit only after paper/shadow matches:
   - start with one stateful model
   - keep order submit behavior unchanged
   - watch journal/report state patch counts
   - keep rollback path to disable state writes without changing model code

Do not combine this work with order submit, cash sync, or live config changes in
the same deploy unless there is a separate rollback plan.

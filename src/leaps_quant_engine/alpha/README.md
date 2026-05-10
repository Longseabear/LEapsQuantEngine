# Alpha Engine

Alpha converts immutable indicator snapshots into predictions called `Insight`.

```text
IndicatorSnapshot
  -> SnapshotContext
  -> AlphaModel.generate(...)
  -> InsightBatch
  -> InsightManager
  -> active insights
```

## Main Files

- `domain.py`: `Insight`, `InsightBatch`, directions, types, and states.
- `loader.py`: Python alpha module loader.
- `runtime.py`: active/pending alpha runtime and safe activation.
- `manager.py`: active insight state, expiry, superseding, and tracked symbols.
- `store.py`: optional insight record storage helpers.

## Contract

Alpha models must:

- consume `SnapshotContext`, not mutable indicator engine internals
- emit `Insight` records only
- include sleeve, symbol, alpha id/version, source snapshot, and reason/debug metadata
- avoid broker, KIS, order, and portfolio mutation logic

Alpha models must not:

- create orders
- mutate portfolio state
- fetch KIS or external data directly
- reload themselves in the middle of a framework cycle

## Runtime Notes

`AlphaRuntime.stage(...)` can dry-run pending models against a validation context. Activation happens at a snapshot/framework boundary, not halfway through a cycle.

`AlphaRuntime.run(...)` can receive `symbols_by_alpha`, a runtime wiring map from
`alpha_id` to the symbol keys selected for that model. The runtime scopes
`SnapshotContext.symbol_keys` before calling each model. Alpha code still sees
only `SnapshotContext`; it does not know which `SelectionModel` produced those
symbols and should not call selection code directly.

Runtime config exposes this as `alpha.input_selections`, mapping `alpha_id` to a
selection result id such as `stock_momentum_top_40` or `etf_rotation_top_20`.

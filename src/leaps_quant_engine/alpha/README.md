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


# AGENTS.md

## Scope

Universe code owns raw/coarse/fine/active/live symbol selection and the operational symbols that must remain monitored.

## Selection Flow

```text
Raw market universe
  -> CoarseSelection
  -> FineUniverseCache
  -> ActiveSelection
  -> ForcedWatchlistPolicy
  -> LiveUniverse
```

## Invariants

The live universe must always include:

```text
selected_active_symbols
  + held_symbols
  + open_order_symbols
  + exit_watch_symbols
  + manual/operator symbols
```

`held_symbols`, `open_order_symbols`, and `exit_watch_symbols` must never be silently dropped.

## Rules

- Universe selection is sleeve/strategy-owned but engine safety invariants are global.
- Config files may define precomputed coarse universes in v0.
- Fine universe entries should carry freshness and failure metadata.
- Active selection cadence is runtime-owned. Use `universe.active.cadence`
  (`startup_only`, `once_per_day`, or interval aliases) and let
  `RuntimeSleeveRuntime` swap the worker universe at cycle boundaries.
- Do not create orders, targets, broker calls, or portfolio mutations here.

## Tests

Tests should prove selection determinism, forced inclusion, stale fine-cache behavior, and sleeve isolation.

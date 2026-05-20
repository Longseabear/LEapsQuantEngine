# Universe Engine

Universe selection defines which symbols a sleeve should monitor and trade.

The intended hierarchy is:

```text
raw market universe
  -> coarse universe
  -> fine universe cache
  -> active selection
  -> forced live universe
```

## Main Files

- `definition.py`: `UniverseDefinition` and symbol metadata.
- `loader.py`: JSON universe loading/parsing.
- `fine.py`: fine universe cache entries, refresh reports, and runtime.
- `selection.py`: active selection context/results and selection models.
- `runtime.py`: active universe runtime and forced-symbol invariant.

## Multi-Model Selection

Selection is independent from alpha model code. A runtime config can wire one
selection result into one or more alpha models, but the selector must not import
or call the alpha. This keeps the dependency as runtime wiring:

```text
SelectionModel
  -> UniverseSelectionResult(selection_id, selected_symbols)
  -> CompositeUniverseSelectionResult
  -> AlphaRuntime symbols_by_alpha
```

`CompositeUniverseSelectionRuntime` runs multiple `UniverseSelectionModel`
instances, preserves each `selection_id`, and builds one live universe from the
union of model-selected symbols plus forced operational symbols.

Runtime config can declare either the legacy single `universe.active.selection_model`
or a multi-model `universe.active.selection_models` list. The selected symbols
remain attributable by `selection_id` for alpha input wiring and status reports.

## Active Selection Cadence

`universe.active.cadence` controls when active selection is refreshed:

- `startup_only`: select during bootstrap and reuse until reload or forced refresh.
- `once_per_day`: refresh once per calendar day.
- interval aliases such as `every_5m` / `every_5_minutes`.

The runtime stores the latest active universe in `RuntimeStateStore` under
`engine-universe-selection / active_universe`. When a refresh is due,
`RuntimeSleeveRuntime` rebuilds the active result and calls
`BackgroundSnapshotWorker.update_universe(...)` at the cycle boundary. Selection
models should remain deterministic; they do not own worker mutation directly.

## Forced Live Universe

Selection models may rank or reject candidates, but the engine must force operational symbols back into the live universe:

```text
live_universe =
  selected_active_symbols
  + held_symbols
  + open_order_symbols
  + exit_watch_symbols
  + manual/operator symbols
```

This invariant protects exits and order maintenance:

```text
held_symbols subset live_universe
open_order_symbols subset live_universe
exit_watch_symbols subset live_universe
```

## Responsibility Boundary

Universe selection chooses symbols. It should not emit insights, create targets, place orders, or mutate portfolio state.

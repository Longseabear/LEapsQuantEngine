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


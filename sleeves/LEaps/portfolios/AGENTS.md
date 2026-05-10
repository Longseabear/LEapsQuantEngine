# AGENTS.md

## Scope

This folder contains LEaps sleeve portfolio construction models.

## Contract

Portfolio models consume active insights and produce target allocations.

## Rules

- Emit percentages or allocation targets, not broker orders.
- Do not convert target percentages into share quantities here.
- Do not mutate holdings, cash, order tickets, or virtual accounts.
- Preserve insight lineage in target metadata.
- Keep low-cash behavior explainable by leaving rounding to order sizing.
- Respect active non-UP insights. If a symbol has an active FLAT/DOWN insight
  at the same or newer timestamp than an UP insight, do not keep a long target
  for that symbol; let the engine produce an exit target for held quantities.
- Treat KRW and USD as separate buckets. Do not use mixed global equity for
  allocation math unless a real FX conversion layer is explicitly passed in.
- When `portfolio.rebalance.cadence` skips a rebuild, the engine reuses the
  previous allocation target batch while `OrderSizingEngine` still recomputes
  current target quantities from the current portfolio, cash, and prices.

## Tests

Test active-insight selection, equal/weighted allocation behavior, flat/exit handling, and lineage preservation.

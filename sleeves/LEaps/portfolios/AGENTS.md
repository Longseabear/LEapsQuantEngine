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
- For complete target portfolio models, missing held symbols in an actionable
  currency bucket should be represented as explicit 0% allocation targets, not
  silent carry-forward. LEaps uses
  `emit_zero_for_missing_held_targets=true` for this behavior.
- The engine-level `PortfolioTargetResolver` also treats LEaps as a complete
  target portfolio by default. Omitted old targets are resolved to 0% before
  portfolio blend, so model migrations can fade old-only symbols out instead of
  accidentally carrying them forever.
- When `portfolio.rebalance.cadence` skips a rebuild, the engine reuses the
  previous allocation target batch while `OrderSizingEngine` still recomputes
  current target quantities from the current portfolio, cash, and prices.
- Do not implement same-day opposite-side cooldowns in portfolio construction.
  That is an execution/risk policy decision. For reused allocation batches,
  runtime config may opt into `portfolio.rebalance.reused_target_churn_guard`
  so `OrderSizingEngine` suppresses tiny adjacent-lot non-exit churn while
  still allowing fresh target batches and explicit exits.

## Tests

Test active-insight selection, equal/weighted allocation behavior, flat/exit handling, and lineage preservation.

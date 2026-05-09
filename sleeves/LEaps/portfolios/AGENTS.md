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

## Tests

Test active-insight selection, equal/weighted allocation behavior, flat/exit handling, and lineage preservation.

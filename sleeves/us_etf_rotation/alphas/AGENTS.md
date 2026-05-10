# AGENTS.md

## Scope

Alpha models for the `us_etf_rotation` sleeve.

## Rules

- Consume `SnapshotContext` only.
- Loop over `context.symbol_keys` so config-driven selection controls inputs.
- Emit `Insight` records only.
- Do not create targets, orders, tickets, fills, or broker calls.
- Daily ETF rotation models should declare daily cadence metadata when applicable.

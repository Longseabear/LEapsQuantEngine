# AGENTS.md

## Scope

Universe selection models for `us_etf_rotation`.

## Rules

- ETF rotation selection must reject non-ETF symbols.
- Forced operational symbols may be selected only through explicit runtime state.
- Selection models return provenance through `UniverseSelectionCandidate`.

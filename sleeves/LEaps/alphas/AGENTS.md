# AGENTS.md

## Scope

This folder contains LEaps sleeve alpha models.

## Contract

Alpha modules consume snapshot context and emit insights only.

## Rules

- Do not emit portfolio percentages, integer quantities, order intents, tickets, or fills.
- Do not read or write virtual account files.
- Do not call KIS, broker-engine, market-data-engine, or adapter functions.
- Use explicit `alpha_id`, version, confidence, expiry/horizon, and reason fields when available.
- Exit or flat signals are acceptable as insights, but downstream layers decide sizing and orders.
- LEaps live alphas may declare `EVALUATION_CADENCE = "every_cycle"` while
  keeping `INPUT_RESOLUTION = "daily"`. The engine gates daily indicator
  updates separately, and portfolio construction handles slower rebalance
  cadence such as `every_5m`.
- Loop over `context.symbol_keys`, not the full snapshot, so runtime
  `alpha.input_selections` can keep KOSPI, ETF, and operational inputs separate.
- `InsightDirection.FLAT` is the correct shape for stop/exit alpha. Portfolio
  construction and risk decide the final exit target and order intent.

## Tests

Add example or unit tests for new alpha models, especially around stale data, missing indicators, and deterministic output.

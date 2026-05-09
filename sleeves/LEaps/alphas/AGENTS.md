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

## Tests

Add example or unit tests for new alpha models, especially around stale data, missing indicators, and deterministic output.

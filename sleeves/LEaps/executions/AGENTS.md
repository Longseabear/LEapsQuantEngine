# AGENTS.md

## Scope

This folder contains LEaps sleeve execution models.

## Contract

Execution models consume risk-approved targets and emit order intents.

## Rules

- Produce intent records only; do not submit to brokers.
- Preserve sleeve, target, risk decision, and model lineage.
- Keep order intent ids or idempotency keys stable for retries when the interface supports it.
- Do not mutate portfolio holdings, cash, or ticket state.
- Same-symbol buy/sell conflicts across sleeves are allowed to exist until global order orchestration reports or resolves them.

## Tests

Test order intent shape, empty/no-op targets, sell symmetry, idempotency metadata, and lineage preservation.

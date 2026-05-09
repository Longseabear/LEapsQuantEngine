# AGENTS.md

## Scope

This folder contains LEaps sleeve risk models.

## Contract

Risk models consume quantity-based targets after order sizing and produce approval, rejection, or clamp decisions.

## Rules

- Run deterministically from target batch, portfolio state, snapshot context, and configured risk limits.
- Do not create order intents or broker tickets.
- Do not mutate holdings or cash.
- Include auditable reasons for every rejection or clamp.
- Sleeve-level risk is local; cross-sleeve buy/sell collision handling belongs to order orchestration.

## Tests

Cover maximum position, cash/notional, concentration, stale-data, and exit-preservation cases.

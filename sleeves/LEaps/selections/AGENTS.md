# AGENTS.md

## Scope

This folder owns LEaps sleeve universe selection models.

## Rules

- Selection models emit selected symbols only.
- Do not emit insights, targets, orders, or broker calls here.
- Keep `selection_id` stable because runtime config maps alpha ids to selection ids.
- Forced operational symbols are still enforced by the engine, but an operational selector may select them intentionally for exit/stop alpha input.
- Keep LEaps selector responsibilities separate:
  - `leaps-stock-momentum` feeds KRX-only stock candidates to
    `leaps-kospi-conviction`.
  - `leaps-etf-rotation` feeds ETF candidates to `leaps-us-stability-hedge`.
  - `leaps-operational-symbols` feeds held/open/manual symbols to stop and exit
    logic.
- A selector may reject a symbol even when that symbol still appears in the
  final live universe as a forced held/open/manual symbol.

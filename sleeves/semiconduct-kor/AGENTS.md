# AGENTS.md

## Scope

`semiconduct-kor` is a KRW-only sleeve workspace for Korean semiconductor stocks.
It owns selection, alpha, portfolio, risk, and execution models for this sleeve.

## Rules

- Keep the sleeve paper/research-first until an operator explicitly wires a live KIS route.
- Keep semiconductor classification in the universe and selection layer, not in runtime config formulas.
- Alpha models emit insights only; they do not create orders or mutate holdings.
- Portfolio models emit target allocations only.
- The active default profile is buy-only for Samsung re-entry. Do not wire sell,
  trim, `FLAT`, or trailing-stop alpha modules into the default runtime unless
  the operator explicitly changes this constraint.
- Risk and execution models must stay deterministic for the same context input.
- Do not call KIS, broker-engine, market-data-engine, or web APIs from sleeve model code.

## Current Contract

Runtime config:

```text
configs/runtime/semiconduct_kor_sleeve.json
```

Universe:

```text
configs/universes/semiconduct_kor_core.json
```

The sleeve is intentionally not added to `configs/runtime/live_multi_sleeve.json`
by default.

Active default models:

```text
alphas/samsung_strike_risk_reentry.py
portfolios/samsung_buy_only.py
risks/basic.py
executions/immediate.py
```

Strike risk status must arrive through snapshot metadata or model state. Missing
status is treated as `on`, which freezes new buys.

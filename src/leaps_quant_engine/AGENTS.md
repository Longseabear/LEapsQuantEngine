# AGENTS.md

## Scope

This is the deterministic engine core. It owns domain objects, runtime wiring, order lifecycle records, virtual sleeve accounts, backtesting, and CLI entry points.

## Pipeline Boundary

The target runtime flow is:

```text
DataSlice
  -> UniverseSelection
  -> IndicatorSnapshot
  -> AlphaModel
  -> InsightManager
  -> PortfolioConstruction
  -> OrderSizing
  -> RiskManagement
  -> ExecutionModel
  -> OrderIntent
  -> OrderTicket / OrderEvent
  -> PortfolioState / VirtualAccount
```

## Ownership Rules

- Alpha emits insights only.
- Portfolio construction emits target percentages and desired values only.
- Order sizing owns integer quantity conversion, rounding loss, lot-size rules, and rebalance noise filtering.
- Risk approves, rejects, or clamps quantity targets.
- Execution emits order intents only.
- Order runtime owns tickets, broker submission events, polling, reconciliation, and collision reports.
- Portfolio and virtual accounts change from fills or explicit reconciliation events, not from order intents.

## Do Not

- Do not call KIS, broker-engine, market-data-engine, or external providers from alpha, portfolio, risk, execution, or indicators.
- Do not let strategy modules submit broker orders directly.
- Do not mutate holdings from `OrderIntent` in live or paper flows.
- Do not add cross-sleeve state sharing without an explicit transfer, reconciliation, or order event.

## Tests

For behavioral changes, add or update tests near the layer touched and run:

```powershell
py -3 -m pytest -q
```

# AGENTS.md

## Scope

This folder owns the LEAN-style framework pipeline after alpha and before concrete order lifecycle.

```text
InsightManager
  -> PortfolioConstruction
  -> OrderSizing
  -> RiskManagement
  -> ExecutionModel boundary
```

## Portfolio Construction

- Consume active insights, not raw alpha modules.
- Emit `PortfolioAllocationTarget` percentages and desired-value plans.
- Do not emit integer share quantities as the primary portfolio construction output.
- Do not mutate holdings, cash, tickets, or virtual accounts.

## Order Sizing

- Convert allocation targets into quantity-based `PortfolioTarget` records.
- Own rounding, lot-size handling, minimum notional filters, and rebalance noise filters.
- Preserve lineage back to sleeve, insight, alpha model, and allocation target.
- Surface rounding loss and skipped targets so low-cash backtests remain explainable.

## Risk

- Run every framework cycle, even when alpha emits no new insights.
- Approve, reject, or clamp quantity targets with auditable reasons.
- Handle sleeve-level risk first; account-level collision and cash coordination belongs later in order orchestration.

## Runner

- Keep cycles deterministic and replayable.
- Never reload Python model code mid-cycle.
- Return enough stage output for agent-readable runtime status and backtest timelines.

## Tests

Framework tests should cover full cycle flow, empty insights, flat/exit signals, low-cash rounding, risk rejection, and timing/status fields.

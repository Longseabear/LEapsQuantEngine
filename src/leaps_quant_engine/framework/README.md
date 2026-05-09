# Framework Engine

The framework package owns the LEAN-style model chain after alpha.

```text
active insights
  -> PortfolioConstructionEngine
  -> RiskManagementModel
  -> ExecutionEngine
  -> OrderIntentBatch
```

`FrameworkRunner` wires these stages into one sleeve-local cycle.

## Main Files

- `runner.py`: sleeve-local alpha, insight manager, portfolio, risk, and execution cycle.
- `portfolio_construction.py`: `PortfolioConstructionEngine`, target batches, target plans, rebalance policy, and equal-weight model.
- `portfolio_model_loader.py`: Python portfolio construction model loading.
- `risk.py`: `RiskManagementModel`, `RiskDecisionBatch`, `BasicRiskManagementModel`, and risk limits.
- `risk_model_loader.py`: Python risk model loading.

## Portfolio Construction

Portfolio construction reads active insights and the current sleeve portfolio. It emits auditable target records:

- `PortfolioTargetBatch`
- `PortfolioTargetPlan`

It should describe desired holdings. It should not submit orders or mutate holdings.

## Risk

Risk receives portfolio targets and returns decisions:

- approved
- clamped
- rejected

The current `BasicRiskManagementModel` supports:

- long-only rejection
- per-symbol max position clamp
- portfolio-level gross exposure clamp
- available-cash clamp
- snapshot quality entry gate

Risk should run every framework cycle, even if alpha emits no new insights.

## Execution

Execution is implemented in the package root `execution.py`, but is invoked by `FrameworkRunner`. It converts approved targets into an `OrderIntentBatch`. It does not submit broker orders.


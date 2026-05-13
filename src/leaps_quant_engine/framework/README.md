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
- `state.py`: optional file-backed framework state for `runtime-run-once` process loops.
- `portfolio_model_loader.py`: Python portfolio construction model loading.
- `risk.py`: `RiskManagementModel`, `RiskDecisionBatch`, `BasicRiskManagementModel`, and risk limits.
- `risk_model_loader.py`: Python risk model loading.

## Portfolio Construction

Portfolio construction reads active insights and the current sleeve portfolio. It emits auditable target records:

- `PortfolioTargetBatch`
- `PortfolioTargetPlan`

It should describe desired holdings. It should not submit orders or mutate holdings.

Portfolio construction may run slower than alpha. For live loops that launch a
new process each cycle, pass `runtime-run-once --framework-state ...` so the
runner can restore the last portfolio target batch and active insights before
checking cadence. A non-due cycle reuses the previous target batch, then risk,
execution, and order sync still run against the current virtual portfolio.

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

# Framework Engine

The framework package owns the LEAN-style model chain after alpha.

```text
active insights
  -> PortfolioConstructionEngine
  -> PortfolioBlendEngine
  -> OrderSizingEngine
  -> RiskManagementModel
  -> ExecutionEngine
  -> OrderIntentBatch
```

`FrameworkRunner` wires these stages into one sleeve-local cycle.

## Main Files

- `runner.py`: sleeve-local alpha, insight manager, portfolio, risk, and execution cycle.
- `portfolio_construction.py`: `PortfolioConstructionEngine`, target batches, target plans, rebalance policy, and equal-weight model.
- `portfolio_blend.py`: optional target-transition layer for smooth operational target changes.
- `state.py`: optional file-backed framework state for `runtime-run-once` process loops.
- `../runtime_state.py`: optional SQLite/in-memory model state store for stateful models.
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

`PortfolioTargetBatch` is the engine-owned target ledger. Portfolio models emit
percent targets; `OrderSizingEngine` converts them into current integer target
quantities every cycle.

## Portfolio Blend

`PortfolioBlendEngine` is an optional engine-level target transition layer. It
does not run an old portfolio model beside a new one. Instead, it compares the
previous committed target snapshot with the current raw `PortfolioTargetBatch`
and linearly blends target percentages for a configured duration.

This is for operational model/config transitions such as "move from old target
weights to new target weights over five hours." Strategic smoothing that is part
of a model's thesis still belongs inside the model.

Blend state is stored through `RuntimeStateStore` under
`model_id="engine-portfolio-blend"` with:

- `namespace="last_target"` for the last committed raw target snapshot
- `namespace="active_transition"` for an in-progress transition

`FrameworkRunner` advances an active blend even when portfolio rebalance cadence
is not due, so a five-minute portfolio model can still produce minute-by-minute
transition progress without re-calling the model.

## Model State

Stateful models may read `context.model_state` and return `StatePatch` records
through their optional `state_patches(...)` hooks. `FrameworkRunner` commits the
patches at the end of a successful cycle when a runtime state store is attached.
Without a store, stateless models behave unchanged and emitted patches are only
visible in the framework result.

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

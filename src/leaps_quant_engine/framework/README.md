# Framework Engine

The framework package owns the LEAN-style model chain after alpha.

```text
active insights
  -> PortfolioConstructionEngine
  -> PortfolioTargetResolver
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
- `portfolio_target_resolver.py`: resolves raw portfolio model output into a complete target vector.
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

## Target Resolution

`PortfolioTargetResolver` runs after raw portfolio construction and before
portfolio blend. Its job is to make target semantics explicit:

```text
raw portfolio output -> resolved complete target vector -> portfolio blend
```

The default `mode="complete"` treats the portfolio model output as the desired
portfolio state. A symbol present in the previous target snapshot or current
sleeve holdings but absent from a non-empty new raw output becomes an explicit
0% target. Empty raw batches are treated as no-action by default so expired or
missing insights do not become an implicit all-sell signal. Use `mode="patch"`
only for models that intentionally emit partial target patches; missing previous
targets are carried forward before blend.

## Portfolio Blend

`PortfolioBlendEngine` is an optional engine-level target transition layer. It
does not run an old portfolio model beside a new one. Instead, it compares the
previous committed target snapshot with the resolved complete
`PortfolioTargetBatch` and linearly blends target percentages for a configured
duration.

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

## Deferred Target Execution

Portfolio targets stay alive until the next portfolio retarget or the relevant
session/order expiry. This lets execution finish a rebalance without forcing the
portfolio model to run again.

Example:

```text
A: target 40% -> 10%
B: target 0%  -> 30%
```

If there is not enough cash to buy `B` before `A` sells, risk may reject or
clamp the `B` buy as `insufficient_cash`. The target is still retained in the
last `PortfolioTargetBatch`. After the `A` sell fill mutates the virtual
account, the next execution pass recomputes quantity from the current portfolio
and may create the `B` buy from the same target batch.

The engine records this in `stage_decisions["target_lifecycle"]`:

- `deferred_buy_count`
- `deferred_buys`
- `blocked_count`
- `blocked_targets`
- `wake_conditions`
- `target_validity`

Only cash-limited buys are reported as `deferred_waiting_for_cash`. Risk
rejects such as stale data, missing price, short target rejection, route/session
guard failures, or operator-disabled conditions must be treated as blocked
targets, not as automatic retry candidates. Closed or unsupported sessions are
reported as `blocked_by_session`; they may become retriable only when the
market/session gate opens again and the target is still current.

This is intentionally execution lifecycle behavior, not portfolio strategy
logic. Portfolio models describe desired holdings. Execution, pending-order
state, risk, and fills decide when remaining unordered quantity can actually be
submitted.

## Model State

Stateful models may read `context.model_state` and return `StatePatch` records
through their optional `state_patches(...)` hooks. `FrameworkRunner` commits the
patches at the end of a successful cycle when a runtime state store is attached.
Without a store, stateless models behave unchanged and emitted patches are only
visible in the framework result.

For simple JSON state, prefer the helper surface on `RuntimeModelStateView`:

- `object_get(...)`
- `object_entries(...)`
- `object_set(...)`
- `object_merge(...)`
- `object_delete(...)`

State ownership is deliberately narrow. Trailing stop high watermarks,
portfolio blend/lerp anchors, daily loss baselines, and drawdown peaks are model
state. Oversell prevention, cash reservation, unsupported routes, unsupported
sessions, idempotency, and missing prices are core guards.

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

Opt-in example models also exist:

- `DailyLossLimitRiskModel`
- `MaxDrawdownRiskModel`

They demonstrate model-owned circuit breaker state through `RuntimeStateStore`.
They are not always-on engine guards; a sleeve must configure them explicitly.

Risk should run every framework cycle, even if alpha emits no new insights.

## Execution

Execution is implemented in the package root `execution.py`, but is invoked by `FrameworkRunner`. It converts approved targets into an `OrderIntentBatch`. It does not submit broker orders.

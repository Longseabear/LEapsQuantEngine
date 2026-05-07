# Agent Artifact Runtime

## Decision

The live engine should run as a long-lived deterministic process. Agents should not mutate live engine memory, portfolio state, indicator state, or broker orders directly.

Agents participate by writing structured artifacts:

```text
agent
  -> artifact inbox
  -> validator
  -> approved artifact store
  -> live engine reloads at a safe boundary
```

This is the main reason to keep the engine pipeline artifact-friendly. Every pipeline stage can accept externally authored but validated model artifacts.

## Runtime Roles

```text
leaps-engine-live
  main market loop
  market data ingest
  indicator update
  universe/alpha/portfolio/risk/execution pipeline
  approved artifact reload at cycle boundaries

leaps-agent-worker
  reads snapshots and public context
  writes universe/alpha/portfolio/risk/execution artifacts
  never sends broker orders directly

leaps-validator
  validates artifact schema and semantics
  compiles artifact to deterministic model
  runs dry-run checks against snapshots
  writes approved/rejected validation result

leaps-backtest-worker
  runs isolated historical tests
  uses virtual/cached data providers
  never touches live portfolio state or broker adapters
```

These roles can be separate Docker containers, separate local processes, or separate CLI commands during development.

## Pipeline Injection Points

Agents may write artifacts for each pipeline stage:

```text
UniverseSelectionModel
  whitelist, blacklist, candidate pool, sector/theme filters

AlphaModel
  signal rules, indicator thresholds, ranking rules

PortfolioConstructionModel
  sizing rules, sleeve allocation, rebalance policy

RiskManagementModel
  max position, drawdown guard, symbol blocks, exposure caps

ExecutionModel
  order guard, limit price policy, session restrictions
```

The engine should load these as models only after validation.

## Validation Flow

The live engine must not stop to test a new artifact.

```text
1. Agent writes artifact to inbox.
2. Validator reads artifact and snapshots.
3. Validator performs schema validation.
4. Validator performs semantic validation.
5. Validator compiles artifact into deterministic model.
6. Validator dry-runs model against recent snapshots.
7. Validator writes approved or rejected report.
8. Live engine observes approved artifact.
9. Live engine atomically swaps model at next safe boundary.
```

Validation layers:

```text
schema
  JSON/YAML shape and required fields

semantic
  rule types, value ranges, sleeve scope, expiry, symbol validity

compile
  artifact can become a deterministic model object

dry-run
  model can run against recent snapshots without exceptions

safety
  model does not exceed hard limits or bypass execution controls
```

## Safe Reload Boundary

The live engine may reload approved artifacts only at deterministic boundaries:

```text
before cycle start
after a market data batch
before order-intent generation
next minute bar boundary
next scheduled rebalance boundary
```

Never swap a model in the middle of a stage execution.

Use atomic replacement:

```text
active_model = old_model
pending_model = validated_new_model

safe boundary:
  active_model = pending_model
```

If validation fails, the live engine keeps the previous active model.

## Snapshot-Based Validation

Validators should test artifacts against copied snapshots, not live in-memory objects.

Example snapshots:

```text
snapshots/latest_indicators.json
snapshots/latest_universe.json
snapshots/latest_candidates.json
snapshots/latest_portfolio.json
snapshots/latest_targets.json
snapshots/latest_order_intents.json
```

The live engine can publish snapshots at controlled points. Validators consume those snapshots asynchronously.

## Backtesting Runtime

Strategy backtests should run in a separate process from the live engine.

```text
same pipeline contracts
same model artifact compiler
same universe/alpha/portfolio/risk/execution interfaces
different runtime context
```

Live runtime:

```text
broker-engine/KIS
live portfolio state
approved live artifacts
real order lifecycle
```

Backtest runtime:

```text
VirtualMarketDataProvider or cached historical data
sandbox portfolio state
simulated fills
artifact timeline replay
no broker calls by default
```

The live engine should not run long historical tests in its own loop. Backtests belong to `leaps-backtest-worker` or a dedicated CLI command.

Every live pipeline stage must be reproducible in backtests through the same interface.

The backtest runtime should replay:

```text
historical data
  -> indicator warmup/update
  -> universe selection
  -> security changes
  -> alpha
  -> portfolio construction
  -> risk
  -> execution
  -> fill simulation
  -> portfolio state
```

Backtest reports should include more than final returns:

```text
universe changes
indicator readiness
insights
portfolio targets
risk rejections
order intents
simulated fills
portfolio snapshots
artifact changes
```

Agent-authored artifacts should be replayable by timeline:

```text
artifact_id
approved_at
effective_from
effective_until
superseded_by
```

This allows the system to test whether an agent-generated universe, alpha, risk, or execution policy would have behaved safely before it is eligible for live approval.

## Artifact Lifecycle

Recommended local layout:

```text
workspaces/sleeves/<sleeve_id>/
  artifacts/
    inbox/
    approved/
    rejected/
  validation/
    reports/
  snapshots/
    latest.json
  models/
    universe/
    alpha/
    portfolio/
    risk/
    execution/
```

Artifact states:

```text
draft
submitted
validating
approved
rejected
expired
superseded
active
retired
```

Each artifact should have:

```text
artifact_id
artifact_type
sleeve_id
created_at
valid_until
schema_version
content_hash
author
rationale
```

## Operating Rules

- Agents write artifacts or directives; they do not place broker orders.
- The validator is the only path from inbox to approved store.
- The live engine loads approved artifacts only.
- The live engine keeps running while validation/backtests happen.
- Backtests run in isolated runtime contexts.
- KIS calls stay behind broker-engine adapters.
- Backtests use cached/virtual providers unless explicitly testing broker-backed history.
- Every live model swap should be logged with artifact id and hash.

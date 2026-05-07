# AGENTS.md

## Project Mission

LEapsQuantEngine is a new LEAN-style dynamic quant engine with first-class sleeve support.

The previous StockProgram stack lives under `reference/stockprogram_legacy` and is reference-only. Do not extend legacy services for new engine work unless the user explicitly asks.

The engine goal is:

```text
universe -> alpha -> portfolio -> risk -> execution -> buy/sell
```

Strategies should feel LEAN-like at the algorithm boundary, while sleeves provide capital ownership, policy boundaries, and operational isolation.

## Development North Star

Build the engine as deterministic, replayable layers:

- `Universe`: selects tradable symbols for a run or sleeve.
- `Alpha`: converts market/fundamental/context data into insights or target signals.
- `Portfolio`: turns alpha into desired holdings and manages current state.
- `Risk`: clamps, rejects, or reshapes portfolio targets before orders.
- `Execution`: converts approved targets into order intents and later broker tickets.
- `Buy/Sell`: concrete side effects are represented as explicit order lifecycle records, never hidden in strategy prose.

Algorithms must not place broker orders directly. They should emit intent-like outputs: insights, targets, or portfolio instructions. Execution is the only layer that should produce order intents.

Market data providers, including KIS, are adapters. Keep provider-specific payloads outside the deterministic engine core.

## Preferred Repository Shape

```text
project-root/
  AGENTS.md
  PROJECT.md
  DEVELOPMENT.md
  README.md
  pyproject.toml
  schema_draft.json
  sample_swing_kor_pipeline.json

  src/leaps_quant_engine/
    algorithm.py
    config.py
    data.py
    engine.py
    execution.py
    models.py
    portfolio.py
    runtime.py
    sleeve.py

  tests/
  docs/
  reference/stockprogram_legacy/
```

Future packages should follow the engine pipeline names before inventing new concepts:

```text
src/leaps_quant_engine/
  universe/
  alpha/
  portfolio/
  risk/
  execution/
  brokerage/
  runtime/
```

Indicators are shared computation primitives for universe, alpha, and risk. Keep them broker-agnostic and feed them normalized `Bar` or `DataSlice` data only.

## KIS Integration Direction

Do not rebuild the old `apps/market-data-engine` or `apps/broker-engine` inside the new core.

Use this layering:

```text
KIS
  -> local broker-engine
  -> market-data adapter / cached replay
  -> MarketDataProvider adapter
  -> normalized Bar / DataSlice
  -> universe / alpha / portfolio / risk / execution
```

KIS access must go through broker-engine because request throughput is shared at the AppKey/lane level. The legacy broker-engine is the reference for rate limiting, token reuse, websocket approval, order command idempotency, and broker operation boundaries.

Historical KIS data has separate operation paths such as `get_daily_ohlcv`, `get_or_cache_daily_ohlcv`, `build_position_replay_feed`, and `get_or_cache_domestic_minute_bars`. New engine history workflows should be cache-first and should normalize payloads before they reach universe or alpha code.

See `docs/kis-market-data-architecture.md` before changing KIS, history, cache, or market-data adapter behavior.

## Backtesting Direction

Backtesting should virtualize external dependencies behind the same interfaces used by live/paper runtime.

```text
VirtualMarketDataProvider / CSV / cached KIS history
  -> replay DataSlice feed
  -> Engine.run(..., fill model)
  -> OrderIntent / OrderEvent / PortfolioState snapshots
```

The virtual provider should be deterministic and sorted chronologically. Do not special-case strategy code for backtests. Strategy code should see the same normalized `DataSlice` shape it would see in live mode.

Live validation and strategy backtests must run outside the live engine loop. See `docs/agent-artifact-runtime.md` for the agent artifact, validator, safe reload, and isolated backtest-worker architecture.

Design rule: every live pipeline stage must be reproducible in the backtest runtime through the same interface.

Backtests must be able to simulate:

- indicator warmup and readiness
- universe selection
- selected universe changes
- alpha outputs
- portfolio construction
- risk decisions and rejections
- execution decisions
- simulated fills
- portfolio state transitions
- agent artifact timelines

If a stage affects live trading but cannot be replayed or dry-run in backtests, the stage boundary is not acceptable yet.

## Indicator Direction

Indicators should follow a LEAN-like surface:

```text
indicator.update(bar)
indicator.is_ready
indicator.current
indicator.warmup_period
```

Use indicators for incremental time-series state such as SMA, EMA, momentum, ROC, ATR, rolling high/low, rolling volatility, VWAP, OBV, and rolling dollar volume. Use separate feature/cache tables for snapshot metadata such as market cap, sector, PER, ETF flags, and universe tags.

Universe selection and alpha models may share indicators through a registry, but strategy code must not fetch raw external data inside an indicator.

Indicator update targets should come from universe configuration, not from the indicator engine itself.

```text
UniverseDefinition file
  -> IndicatorDefinition plan
  -> IndicatorEngine.register_universe(sleeve_id, ...)
  -> DataSlice updates registered symbols only
```

Store universe and indicator plans in config files. Keep live indicator objects, rolling windows, readiness, and current values in memory. Do not write indicator state to disk on every update.

Indicator state must be sleeve-namespaced:

```text
sleeve_id -> symbol_key -> indicator_name -> indicator
```

The same symbol may have different indicator periods, names, and readiness in different sleeves.

Indicator runtime support should cover both:

- broker-engine/KIS latest-bar updates through `MarketDataProvider`
- deterministic backtest updates through `VirtualMarketDataProvider`

Both paths must update the same `IndicatorEngine` surface.

## Sleeve System

A sleeve is a first-class portfolio compartment.

Each sleeve owns:

- `sleeve_id`
- cash allocation
- holdings and pending orders
- universe settings
- algorithm or alpha model
- portfolio construction policy
- risk policy
- execution policy

Sleeves may share infrastructure such as data feeds and broker adapters, but they must not silently share state. Cross-sleeve movement of cash, lots, or positions must be explicit and auditable.

## Pipeline Contract

The intended v0 runtime flow is:

```text
DataSlice
  -> UniverseSelection
  -> AlphaModel
  -> PortfolioConstruction
  -> RiskManagement
  -> ExecutionModel
  -> OrderIntent
  -> OrderTicket
  -> OrderEvent
  -> PortfolioState transition
```

Current v0 code starts with:

```text
DataSlice -> Engine -> Sleeve -> Algorithm.on_data -> PortfolioTarget -> SleevePolicy -> ExecutionModel -> OrderIntent
```

When adding new functionality, move toward the full contract without breaking the current simple path.

## Portfolio State Machine

Portfolio state must be explicit, deterministic, and replayable.

Use a state-machine mindset instead of mutating holdings casually. A portfolio should evolve from events:

```text
empty
  -> target_created
  -> risk_approved
  -> order_intent_created
  -> order_submitted
  -> partially_filled
  -> filled
  -> invested
  -> exit_target_created
  -> exit_order_submitted
  -> reduced | closed
```

Error and control states are also first-class:

```text
risk_rejected
order_rejected
cancel_requested
cancelled
replace_requested
stale
suspended
reconciled
```

### State Ownership

- `PortfolioTarget` represents desired holdings.
- `RiskDecision` should explain approval, clamping, or rejection.
- `OrderIntent` represents an execution request before broker submission.
- `OrderTicket` should represent broker submission identity.
- `OrderEvent` should represent broker/fill/cancel/reject updates.
- `PortfolioState` should be updated only from accepted state transitions, not from random strategy logic.

### Transition Rules

- Do not update holdings from `OrderIntent` alone in live/paper flows.
- Holdings change from fills or explicit reconciliation events.
- Backtests may use immediate fills, but that must be a fill model decision.
- Every buy or sell should be traceable back to sleeve, algorithm, symbol, target, risk decision, and execution model.
- Replacements and cancellations should preserve lineage to the original intent.
- Idempotency keys are required before live broker submission exists.

## Buy/Sell Structure

Buy and sell behavior should be symmetrical where possible:

- Buy starts from alpha/portfolio desire and is capped by cash, sleeve allocation, risk, and market constraints.
- Sell starts from portfolio state, exit alpha, risk reduction, stop logic, rebalance, or operator instruction.
- Both produce order intents first.
- Both must pass through risk and execution.
- Both should emit auditable events.

Never encode actionable buy/sell conditions as prose-only notes when they can be represented as targets, risk rules, or order lifecycle records.

## Legacy Mapping

Use the legacy stack to understand operational lessons:

- old `total_orchestrator` and `stack_orchestrator`: runtime orchestration reference
- old sleeve agents: sleeve ownership and workflow reference
- old contracts: source intent/reference for order instructions
- old order-chain records: lineage model for future order lifecycle
- old fund-system: source-of-truth lesson for portfolio and fills

Do not copy legacy complexity into the new core. Rebuild concepts in smaller deterministic layers.

## Engineering Rules

- Prefer small, tested core models before services.
- Keep broker-specific code behind adapters.
- Keep strategy APIs small and LEAN-like.
- Use dataclasses or typed models for domain state.
- Add tests for every state transition or pipeline layer.
- Keep generated runtime artifacts out of Git unless they are intentional samples.
- Run `py -3 -m pytest -q` before reporting a completed code change.

## Current Commands

```powershell
py -3 -m pytest -q
```

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli run-once sample_swing_kor_pipeline.json
```

## Working Agreement

When asked to continue development, prefer implementing the next narrow vertical slice:

1. Define the domain object.
2. Route it through the pipeline.
3. Add a test that proves the behavior.
4. Update docs or samples if the public contract changed.

The project should grow from executable slices, not from large untested scaffolding.

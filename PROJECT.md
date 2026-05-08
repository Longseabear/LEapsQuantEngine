# PROJECT

## Goal

Build a LEAN-style dynamic quant engine with first-class sleeve support.

The engine should feel like LEAN at the strategy boundary while retaining the useful operating lessons from the legacy stack:

- deterministic event loop
- explicit portfolio state
- sleeve-level capital/risk isolation
- order intent generation before broker submission
- replayable configs and samples

## Current v0 Scope

Implemented slices now cover:

- Core domain models for symbols, bars, data slices, holdings, targets, and order intents.
- A minimal LEAN-like algorithm interface and sleeve-aware synchronous engine loop.
- Backtesting over virtual/cached bars with report-ready metrics: CAGR, Sharpe, MDD, turnover, average holding days, average exposure, win rate, trade count, and order count.
- A 30+ indicator catalog with sleeve-namespaced `IndicatorEngine` state.
- Immutable `IndicatorSnapshot` and `MarketDataSnapshot` objects so strategy/risk/execution consumers can read stable state while live indicator objects keep updating.
- Cache-first daily indicator benchmark for 200-symbol Korean universes.
- Live quote snapshot checks through local `market-data-engine`, including mixed US exchanges and configurable request pacing.
- Structured JSON logging for snapshot collection, provider calls, failures, and indicator snapshot publication.

Still intentionally incomplete:

- Full `UniverseSelection -> Alpha -> PortfolioConstruction -> Risk -> Execution` model chain.
- Background snapshot worker that continuously refreshes active market snapshots.
- Real order ticket/order event lifecycle and live fill reconciliation.
- Agent artifact validator and safe reload implementation.

## Current Runtime Shape

```text
UniverseDefinition
  -> MarketDataProvider / VirtualMarketDataProvider
  -> MarketDataSnapshot
  -> IndicatorEngine.on_data(DataSlice)
  -> IndicatorSnapshot
  -> future universe / alpha / portfolio / risk / execution consumers
```

The simple strategy path still exists:

```text
DataSlice -> Engine -> Sleeve -> Algorithm.on_data -> PortfolioTarget -> SleevePolicy -> ExecutionModel -> OrderIntent
```

Future work should converge these two paths through explicit alpha, portfolio construction, risk, and execution model interfaces.

## Reference Policy

Legacy code lives in `reference/stockprogram_legacy`.

Use it to understand orchestration, order-chain semantics, sleeve workspaces, and operational safeguards. Do not extend that tree for the new engine unless explicitly asked.

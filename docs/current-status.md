# Current Status

Last updated: 2026-05-09

This document records what is currently implemented and what should come next. It is intentionally operational: keep it close to the code and update it whenever a public runtime contract changes.

## Implemented

### Core Engine

- `Symbol`, `Bar`, `DataSlice`, `PortfolioTarget`, and `OrderIntent` domain models.
- `Algorithm` interface with a LEAN-like `on_data` boundary.
- `Engine` loop that routes `DataSlice` through sleeves.
- `Sleeve` and `SleevePolicy` for cash/policy compartments.
- `ExecutionModel` that creates order intents instead of broker orders.
- Runtime config loaders and a sample swing pipeline.

### Backtesting

- `VirtualMarketDataProvider` for deterministic replay.
- `run_backtest(...)` with immediate fill simulation.
- `run_framework_backtest(...)` for LEAN-style alpha framework replay:
  - historical `DataSlice`
  - `IndicatorEngine`
  - `IndicatorSnapshot`
  - `FrameworkRunner`
  - immediate fills
  - `BacktestMetrics`
- Aggregate and per-sleeve metrics:
  - CAGR
  - Sharpe
  - MDD
  - turnover
  - average holding days
  - average exposure
  - win rate
  - trade count
  - order count

### Indicators

- LEAN-like indicator surface:

```text
indicator.update(bar)
indicator.is_ready
indicator.current
indicator.warmup_period
```

- 30+ supported indicator types, including:
  - SMA, EMA, momentum, ROC
  - rolling min/max/range
  - variance, standard deviation, z-score
  - ATR, true range, gap percent, bar return
  - rolling volume, rolling dollar volume, VWAP
  - OBV, PVT, accumulation/distribution, money flow volume

- `IndicatorEngine` namespaces state by sleeve:

```text
sleeve_id -> symbol_key -> indicator_name -> indicator
```

- Indicator update targets come from `UniverseDefinition`, not the indicator engine itself.

### Snapshots

The live indicator objects are mutable, but consumers should read stable snapshots:

```text
MarketDataSnapshot
  -> IndicatorEngine.on_data(DataSlice)
  -> IndicatorSnapshot
  -> IndicatorSnapshotStore
```

Implemented snapshot classes:

- `MarketDataSnapshot`
- `MarketDataCollectionReport`
- `MarketDataCollectionFailure`
- `BackgroundSnapshotWorker`
- `SnapshotWorkerCycleReport`
- `SnapshotWorkerRunReport`
- `SnapshotFreshnessPolicy`
- `SnapshotQualityReport`
- `IndicatorValue`
- `IndicatorSnapshot`
- `IndicatorSnapshotStore`

Snapshot collection supports best-effort operation:

- symbol-level quote failures are recorded
- `min_success` can guard snapshot quality
- successful bars can still be published when the threshold is satisfied

`SnapshotFreshnessPolicy` evaluates collected market data before consumers use it:

```text
MarketDataSnapshot
  -> SnapshotFreshnessPolicy
  -> SnapshotQualityReport
  -> IndicatorSnapshot
  -> future Alpha / Risk consumers
```

Quality statuses:

- `fresh`: safe for new entries and risk checks
- `degraded`: usable for cautious/risk workflows, but not ideal for new entries
- `stale`: old snapshot; risk checks may inspect it, but new entries should be blocked
- `invalid`: do not use for decisions

Current quality inputs:

- complete ratio
- snapshot age
- collection duration
- requested/collected/failed symbol counts

### Warmup

Indicator warmup is now a separate startup/restart step, not a per-tick loop.

```text
UniverseDefinition
  -> WarmupPolicy.required_bars(...)
  -> cached daily history load
  -> IndicatorEngine.warm_up(...)
  -> WarmupReport
  -> live snapshot worker starts from ready in-memory state
```

Implemented warmup classes:

- `WarmupPolicy`
- `WarmupSymbolReport`
- `WarmupReport`
- `WarmupResult`

Warmup computes the largest required indicator `warmup_period` for the sleeve universe, loads cache-first daily history, keeps only the needed trailing bars, updates the same in-memory `IndicatorEngine` used by live/replay paths, and reports symbol readiness.

Operational triggers:

- before market open
- engine restart
- universe or indicator-plan change
- confirmed daily bar refresh
- explicit operator smoke/debug run

### Background Snapshot Worker

The first long-running runtime building block is implemented as a deterministic worker object:

```text
BackgroundSnapshotWorker.run(...)
  -> optional warmup
  -> collect MarketDataSnapshot best-effort
  -> evaluate SnapshotFreshnessPolicy
  -> IndicatorEngine.on_data(...)
  -> publish IndicatorSnapshotStore active snapshot
  -> return SnapshotWorkerCycleReport
```

The worker can run a bounded number of cycles for smoke/debug commands or can be started on a background thread with `start(...)`. It does not make strategy decisions. Its job is to keep the latest indicator snapshot available for future alpha/risk consumers.

### Runtime Config And Control

Runtime option snapshots now have a v0 contract.

Implemented contracts:

- `ModuleReference`
- `MarketDataRuntimeConfig`
- `UniverseRuntimeConfig`
- `FineUniverseRuntimeConfig`
- `ActiveUniverseRuntimeConfig`
- `IndicatorRuntimeConfig`
- `AlphaRuntimeConfig`
- `PortfolioRuntimeConfig`
- `RebalancePolicyRuntimeConfig`
- `WorkerRuntimeConfig`
- `SleeveRuntimeConfig`
- `RuntimeConfig`
- `RuntimeConfigSnapshot`
- `RuntimeControlCommand`
- `RuntimeControlQueue`
- `RuntimeConfigController`

The boundary is intentional:

```text
config = settings and module references
module = strategy / selection / alpha / risk logic
control command = when to apply a change
runtime snapshot = currently applied config version
```

The running process should not poll and reload config files every cycle. It should keep the active `RuntimeConfigSnapshot` in memory, drain control commands at cycle boundaries, and load a config file only for explicit `reload_config` commands.

Runtime bootstrap is also implemented. `bootstrap_sleeve_runtime(...)` takes a validated `RuntimeConfigSnapshot` and builds:

- coarse `UniverseDefinition`
- market-data and history providers
- optional `FineUniverseRuntime` and fine refresh report
- configured `UniverseSelectionModel`
- active `UniverseDefinition`
- `AlphaRuntime` from alpha module references
- `PortfolioConstructionEngine` from Portfolio Construction Model references and rebalance policy settings
- sleeve `Portfolio` with configured cash allocation
- `FrameworkRunner` for alpha, insight state, portfolio construction, risk, and execution
- `BackgroundSnapshotWorker`

This is the first executable bridge from option snapshot to a live one-cycle worker path.
The worker now owns snapshot collection and indicator publication only in this bootstrap path. Alpha and downstream framework stages run after the active `IndicatorSnapshot` is published, so `runtime-run-once` can show both worker timing and framework artifacts.

The first config smoke file is:

```text
configs/runtime/live_us_smoke.json
```

Validation command:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/live_us_smoke.json
```

Configured one-cycle runtime command:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-run-once configs/runtime/live_us_smoke.json --sleeve-id us-live --held IBM --skip-warmup --summary-only
```

### Alpha Runtime

Python Alpha Models are supported directly.

Implemented alpha contracts:

- `SnapshotContext`
- `InsightDirection`
- `Insight`
- `InsightBatch`
- `InsightStore`
- `AlphaRuntime`
- `PythonAlphaLoader`
- `FunctionAlphaModel`

Runtime flow:

```text
IndicatorSnapshot
  -> SnapshotContext
  -> AlphaRuntime
  -> Python AlphaModel.generate(context)
  -> InsightBatch
  -> InsightStore
```

Python alpha module formats:

- `create_alpha_model()`
- `ALPHA_MODEL`
- module-level `generate(context)` with optional `ALPHA_ID` and `VERSION`

Alpha models are direct Python code and therefore trusted in-process model modules. They must still dry-run against a snapshot before activation, and `AlphaRuntime` swaps pending models only at snapshot boundaries.

Alpha output is intentionally not an order. `Insight` is the next deterministic pipeline artifact before portfolio construction and risk.

Current example alpha modules:

- `examples/alpha/live_quote_smoke_alpha.py`
- `examples/alpha/price_above_sma_alpha.py`
- `examples/alpha/momentum_strategy_alpha.py`
- `examples/alpha/etf_rotation_alpha.py`
- `examples/alpha/volatility_trailing_stop_alpha.py`

The examples are intentionally simple. Live quote smoke emits short-lived UP insights from live quote indicators so the configured runtime can prove snapshot-to-order-intent flow. Momentum emits UP insights for price-above-average positive momentum. ETF rotation emits weighted UP insights for top-ranked ETFs and FLAT insights for unselected names. Volatility trailing stop emits FLAT exit insights when price breaches a volatility-adjusted stop.

### Insight Manager And Framework Pipeline

`Insight` now carries LEAN-style prediction fields:

- `insight_id`
- `insight_type`
- `direction`
- `generated_at`
- `expires_at`
- `magnitude`
- `confidence`
- `weight`
- `group_id`
- `alpha_id`
- `alpha_version`
- `source_snapshot_id`
- `reason`

`InsightManager` tracks insight state:

```text
active
expired
cancelled
superseded
```

The manager ingests new `InsightBatch` objects, supersedes active same-alpha/same-symbol/same-type insights, expires old insights by time, and exposes the current active insight set.

The first LEAN-style framework runner is implemented:

```text
IndicatorSnapshot
  -> AlphaRuntime
  -> InsightManager
  -> PortfolioConstructionEngine
  -> PortfolioTargetBatch
  -> RiskManagementModel
  -> ExecutionModel
  -> OrderIntent
```

Implemented framework contracts:

- `FrameworkRunner`
- `FrameworkCycleResult`
- `StageTiming`
- `PortfolioTargetBatch`
- `RebalancePolicy`
- `PythonPortfolioConstructionModelLoader`
- `PortfolioConstructionContext`
- `PortfolioConstructionEngine`
- `PortfolioConstructionModel`
- `EqualWeightPortfolioConstructionModel`
- `RiskManagementContext`
- `RiskManagementModel`
- `RiskDecision`
- `RiskDecisionBatch`
- `PassThroughRiskManagementModel`

Current v0 behavior:

- Alpha emits new insights only.
- `InsightManager` maintains active/inactive signal state.
- `PortfolioConstructionEngine` reads active insights and produces auditable `PortfolioTargetBatch` records.
- `EqualWeightPortfolioConstructionModel` remains the first model implementation.
- `RebalancePolicy` can reserve cash, filter tiny quantity deltas, and suppress tiny non-exit order notionals.
- Portfolio Construction Models can be loaded from Python model modules through `PythonPortfolioConstructionModelLoader`.
- When previously managed insights expire, portfolio construction can emit flatten targets.
- Risk runs every framework cycle and currently passes targets through.
- Execution turns approved targets into `OrderIntent` records using the existing immediate execution model.
- `runtime-run-once` now runs this framework path after `BackgroundSnapshotWorker` publishes the active indicator snapshot.

### Universe Selection

Universe selection now has a v0 domain structure.

Implemented contracts:

- `UniverseSelectionContext`
- `UniverseSelectionCandidate`
- `UniverseSelectionResult`
- `StaticUniverseSelectionModel`
- `MomentumUniverseSelectionModel`
- `FineUniverseCache`
- `FineUniverseRuntime`
- `FineUniverseRefreshReport`

The selection hierarchy is:

```text
coarse universe file, e.g. 200 symbols
  -> fine universe cache refresh
  -> fresh fine universe
  -> sleeve UniverseSelectionModel
  -> selected active candidates
  -> forced operational watchlist
  -> live universe
```

Fine universe is the paced cache tier. It can refresh a larger symbol set over a 1-5 minute window, keeping per-symbol `updated_at`, freshness, and failure metadata. Active selection can then run from fresh fine symbols instead of assuming every coarse symbol is current.

Forced symbols are always included in the final live universe:

```text
live_symbols =
  selected_symbols
  + held_symbols
  + open_order_symbols
  + exit_watch_symbols
  + manual_symbols
```

`UniverseSelectionResult.to_universe_definition(...)` can turn the selected live symbols back into a `UniverseDefinition` for `BackgroundSnapshotWorker`.

The first implemented strategy selector is momentum/liquidity based. It scores candidates from an `IndicatorSnapshot` using:

- liquidity indicator
- momentum indicator
- optional price-above-average bonus
- optional volatility penalty

This is intentionally only a v0 selector. Different sleeves should be able to provide different selection models later.

### Market Data

KIS is not called directly by the deterministic engine core.

Current adapter path:

```text
local market-data-engine / broker-engine
  -> MarketDataEngineLiveQuoteProvider / KISCachedMarketDataProvider
  -> normalized Bar
  -> MarketDataSnapshot
```

Implemented adapters:

- `KISBrokerEngineMarketDataProvider`
- `KISCachedMarketDataProvider`
- `MarketDataEngineLiveQuoteProvider`

Live quote pacing:

- default env: `MARKET_DATA_ENGINE_RATE_LIMIT_PER_SECOND`
- CLI override: `--rate-limit-per-second`
- KIS-backed live quote adapter clamps to 20/s

Mixed overseas exchange metadata is supported in universe files:

```json
{
  "ticker": "NVDA",
  "exchange": "NAS"
}
```

### Benchmarks And Smokes

Daily indicator benchmark:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli benchmark-indicators-daily configs/universes/benchmark_kor_200.json --sleeve-id benchmark-kor
```

Daily indicator warmup smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli warmup-indicators-daily configs/universes/swing_kor_core.json --sleeve-id swing-kor --summary-only
```

Bounded snapshot worker run:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli snapshot-worker-run configs/universes/swing_kor_core.json --sleeve-id swing-kor --cycles 1 --interval-seconds 0 --summary-only
```

Python alpha smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli alpha-run-snapshot configs/universes/swing_kor_core.json examples/alpha/price_above_sma_alpha.py --sleeve-id swing-kor --min-success 2 --rate-limit-per-second 20 --summary-only
```

Active universe selection smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli select-active-universe configs/universes/benchmark_kor_200.json --sleeve-id benchmark-kor --top-n 60 --summary-only
```

Active-only snapshot worker smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli active-snapshot-worker-run configs/universes/us_live_smoke.json --sleeve-id us-live --top-n 2 --held IBM --cycles 1 --interval-seconds 0 --skip-worker-warmup --min-success 2 --rate-limit-per-second 20 --summary-only
```

Fine universe refresh smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli fine-universe-refresh configs/universes/us_live_smoke.json --rate-limit-per-second 20 --include-entries
```

Recent 200-symbol Korean daily replay result:

- universe: 200
- indicators per symbol: 31
- sessions: 30
- estimated indicator updates: 186,000
- average `IndicatorEngine.on_data`: about 6.37 ms
- p95: about 10.50 ms

US live snapshot smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3 --rate-limit-per-second 20
```

Recent 200-symbol US live snapshot smoke:

- quote source: local `market-data-engine`
- universe: 200
- updated: 200
- indicators per symbol: 28
- collection: about 43.8 seconds at the observed shared-lane pace
- indicator update + snapshot publish: about 20.6 ms

Interpretation:

```text
data collection is the bottleneck
indicator update is not the bottleneck
```

Recent US Coarse/Fine/Active smoke result:

```text
Coarse universe:
  NVDA, MSFT, AAPL, IBM

Fine refresh:
  requested: 4
  updated: 4
  failed: 0
  fresh: 4
  elapsed: about 857 ms

Active selection:
  selected: NVDA, MSFT
  forced held: IBM
  live universe: NVDA, MSFT, IBM

Active live worker:
  requested: 3
  updated: 3
  failed: 0
  quality: fresh
  collection: about 2,358 ms
  indicator update + snapshot publish: about 0.206 ms
```

Interpretation:

```text
fine cache can refresh broader candidates on a slower cadence
active worker should update only symbols that need live freshness
KIS / market-data-engine collection remains the bottleneck
engine-side indicator and snapshot work remains sub-millisecond for this size
```

Recent configured runtime smoke:

```text
command:
  runtime-run-once configs/runtime/live_us_smoke.json --sleeve-id us-live --held IBM --skip-warmup --summary-only

fine refresh:
  requested: 4
  updated: 4
  elapsed: about 538 ms on a warm local cache

worker:
  live universe: NVDA, MSFT, IBM
  requested: 3
  updated: 3
  quality: fresh
  collection: about 262 ms
  indicator update + snapshot publish: about 0.140 ms

framework:
  alpha: live-quote-smoke
  new insights: 3
  portfolio targets: 3
  approved risk decisions: 3
  order intents: 3 buy intents
  framework total: about 0.252 ms
```

Recent Korean minute framework backtest smoke:

```text
date: 2026-05-08
symbols: 005930, 000660, 035420
resolution: 1 minute
loaded bars: 1,143
data slices: 381
indicator snapshots: 381
framework cycles: 381
alpha: price-above-sma-demo
insights: 424
orders: 21
buy orders: 11
sell orders: 10
history load: about 2,979 ms
framework backtest loop: about 158 ms
framework stage total: about 59 ms
```

Single-day intraday CAGR is not meaningful because the existing metric annualizes the very short test window. For minute-level smoke interpretation, prefer total return, drawdown, turnover, exposure, win rate, trade count, order count, and stage timing.

Recent Korean five-year daily framework backtest smoke:

```text
source: FinanceDataReader
period: 2021-05-10 -> 2026-05-08
symbols: 005930, 000660, 035420
loaded bars: 3,672
data slices: 1,224
indicator snapshots: 1,224
framework cycles: 1,224
alpha: price-above-sma-demo
insights: 1,600
orders: 991
buy orders: 509
sell orders: 482
history load: about 791 ms
framework backtest loop: about 681 ms
framework stage total: about 520 ms
total return: 243.20%
CAGR: 28.01%
Sharpe: 1.00
MDD: 42.45%
turnover: 17.20
average holding days: 267.24
average exposure: 97.19%
win rate: 55.81%
trade count: 482
```

KIS/broker-engine daily cache limitation observed on 2026-05-09:

```text
requested period: 2021-05-10 -> 2026-05-08
returned period: 2026-03-26 -> 2026-05-08
returned sessions per symbol: 30
```

The legacy broker operation currently applies `start_date` / `end_date` filtering after receiving the KIS daily payload. If the provider payload contains only recent rows, the new engine cannot expand that into a five-year replay. Treat KIS daily cache as a recent-cache smoke until a paged KIS history path or a dedicated historical provider adapter is implemented.

Recent Korean 200-symbol five-year daily framework load test:

```text
source: FinanceDataReader
period: 2021-05-10 -> 2026-05-08
universe: benchmark_kor_200
symbols with data: 200
partial-history symbols: 30
indicator count per symbol: 31
loaded bars: 225,051
estimated indicator updates: 6,976,581
data slices: 1,224
average symbols per slice: 183.87
framework cycles: 1,224
alpha: momentum-strategy-demo
insights: 56,914
orders: 101,627
buy orders: 48,604
sell orders: 53,023
history load: about 53,043 ms
feed build: about 48 ms
framework backtest loop: about 38,786 ms
framework stage total: about 4,094 ms
average framework cycle: about 3.34 ms
p95 framework cycle: about 8.72 ms
max framework cycle: about 21.02 ms
alpha avg: about 0.68 ms
insight manager avg: about 0.34 ms
portfolio avg: about 2.10 ms
risk avg: about 0.08 ms
execution avg: about 0.16 ms
```

This load test first exposed a severe `InsightManager` scaling issue: active/supersede lookups were scanning all historical insight records, causing the same run's framework loop to take about 431 seconds. `InsightManager` now maintains active indexes by sleeve/symbol/alpha/type and tracked-symbol indexes by sleeve. After the fix, the framework loop dropped to about 38.8 seconds for the same 200-symbol five-year replay.

### Logging

Global CLI options:

```powershell
--log-level INFO
--log-json
--log-file logs/live-snapshot.jsonl
--log-max-bytes 10000000
--log-backup-count 5
```

Logging rules:

- command result JSON stays on stdout
- logs go to stderr and/or file
- file logs rotate by default
- JSON Lines are available for server log collection

Important event names:

- `market_data_snapshot.collect.start`
- `market_data_snapshot.collect.symbol_failed`
- `market_data_snapshot.collect.complete`
- `market_data_snapshot.collect.min_success_failed`
- `market_data_engine.call.start`
- `market_data_engine.call.success`
- `market_data_engine.call.rate_limited`
- `market_data_engine.call.failed`
- `indicator_snapshot.update.start`
- `indicator_snapshot.publish`
- `indicator_snapshot.update.complete`
- `live_indicator_snapshot.start`
- `live_indicator_snapshot.complete`
- `background_snapshot_worker.warmup.start`
- `background_snapshot_worker.warmup.complete`
- `background_snapshot_worker.cycle.start`
- `background_snapshot_worker.cycle.complete`
- `alpha_runtime.pending.stage`
- `alpha_runtime.pending.activate`
- `alpha_runtime.model.complete`
- `alpha_runtime.batch.publish`

`live_indicator_snapshot.complete` includes quality fields such as `quality_status`, `quality_complete_ratio`, and `quality_reasons`.

## Current Commands

Full tests:

```powershell
py -3 -m pytest -q
```

Expected current result:

```text
113 passed
```

Run sample:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli run-once sample_swing_kor_pipeline.json
```

Live snapshot with debug logs:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli --log-level INFO --log-json --log-file logs/live-snapshot.jsonl live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3 --rate-limit-per-second 20
```

## Known Limitations

- `BackgroundSnapshotWorker` exists, but it is not yet wrapped by a production process supervisor.
- Freshness/degraded-state reporting exists. Alpha can see quality through `SnapshotContext`, but formal risk gates are not wired yet.
- `PortfolioConstructionEngine` exists with Python Portfolio Construction Model loading, a v0 equal-weight model, and rebalance policy. Risk still only has a pass-through implementation.
- Framework alpha backtesting exists, but n-1 minute delayed indicator snapshot modeling is not implemented yet.
- Universe selection exists, but is not yet automatically scheduled into the live worker loop.
- No order ticket/order event state machine yet.
- Live 200-symbol polling is bounded by external KIS/market-data-engine throughput and should not be used as a high-frequency strategy loop.
- Current live US Top 200 universe generation was tested ad hoc; the committed fixture is a small smoke universe.

## Indicator Resolution Policy

Default strategy indicators should be treated as confirmed daily indicators.

For swing/low-frequency strategies:

```text
confirmed daily indicators
  -> update only when a daily bar is complete
  -> remain fixed during the next intraday session

live market snapshot
  -> current price
  -> current volume
  -> intraday return
  -> freshness / data quality
```

Example:

```text
2026-05-08 intraday alpha check
  SMA20 = value confirmed from daily bars through 2026-05-07 close
  current_price = latest live quote during 2026-05-08 session

condition:
  current_price > confirmed_daily_sma20
```

In this model, the SMA value does not move intraday, but the comparison result can change because current price moves.

Provisional daily indicators may be added later, but they must be explicitly named and replayable:

```text
sma_20_daily_confirmed
sma_20_daily_provisional = previous 19 confirmed daily closes + current live price
```

Do not mix daily bars, minute bars, and quote snapshots into the same indicator stream. Indicator definitions should eventually declare a resolution such as `daily`, `minute`, or `quote`, and the engine should reject mismatched updates unless a consolidator explicitly converts the data.

Current practical split:

```text
Confirmed daily / history-updated:
  SMA, EMA, momentum/ROC, ATR, rolling high/low/range,
  rolling volatility, drawdown, rolling dollar volume

Live quote / snapshot-updated:
  close/current price, volume, one-snapshot dollar volume,
  quote VWAP-like values, intraday return, spread/liquidity fields
```

`us_live_smoke.json` intentionally uses live quote indicators:

```text
close
volume
dollar_volume_1
vwap_1
```

These can be updated by active live snapshots. Confirmed daily indicators should not be advanced by quote snapshots unless the indicator is explicitly defined as provisional or intraday.

## Next Work

Recommended next vertical slice:

```text
UniverseSelectionRuntime
  -> run selection on schedule or operator trigger
  -> publish active UniverseDefinition
  -> restart/update BackgroundSnapshotWorker target symbols safely
  -> keep forced watchlist invariant
```

After that:

1. Define minimal `RiskManagementModel` quality gates.
2. Add configurable portfolio construction and risk module references.
3. Add risk quality gates and max exposure / max position clamps.
4. Persist and replay portfolio/risk state snapshots across process restarts.
5. Simulate n-1 minute delayed indicator snapshots in backtests.
6. Add idempotent order intent lineage before broker submission.
7. Add order ticket/order event state machine.

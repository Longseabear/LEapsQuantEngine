# Current Status

Last updated: 2026-05-08

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
- `IndicatorValue`
- `IndicatorSnapshot`
- `IndicatorSnapshotStore`

Snapshot collection supports best-effort operation:

- symbol-level quote failures are recorded
- `min_success` can guard snapshot quality
- successful bars can still be published when the threshold is satisfied

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

## Current Commands

Full tests:

```powershell
py -3 -m pytest -q
```

Expected current result:

```text
52 passed
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

- No continuous background snapshot worker yet.
- No freshness/degraded-state policy yet.
- No formal `UniverseSelectionModel`, `AlphaModel`, `PortfolioConstructionModel`, or `RiskManagementModel` interface yet.
- No order ticket/order event state machine yet.
- Live 200-symbol polling is bounded by external KIS/market-data-engine throughput and should not be used as a high-frequency strategy loop.
- Current live US Top 200 universe generation was tested ad hoc; the committed fixture is a small smoke universe.

## Next Work

Recommended next vertical slice:

```text
BackgroundSnapshotWorker
  -> paced active universe quote collection
  -> MarketDataSnapshot close
  -> IndicatorEngine update
  -> IndicatorSnapshotStore publish
  -> freshness/degraded-state report
```

After that:

1. Add snapshot freshness policy.
2. Add active universe selection from a larger research universe.
3. Define `AlphaModel` over `IndicatorSnapshot`.
4. Route alpha outputs into portfolio construction and risk.
5. Simulate n-1 minute indicator snapshots in backtests.
6. Add order intent lineage into portfolio state transitions.

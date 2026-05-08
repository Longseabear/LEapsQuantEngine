# DEVELOPMENT

## Principles

- Core engine code should stay deterministic and easy to replay.
- Strategy APIs should remain small, LEAN-like, and friendly to iterative research.
- Sleeves are not just folders; they are capital, policy, and responsibility boundaries.
- Execution starts with order intents. Broker-specific submission belongs behind adapters.

## Current Milestone

The v0 skeleton has moved past the initial engine loop. The active milestone is now:

- Keep the deterministic core small while adding executable vertical slices.
- Run live and replay data through the same normalized `Bar` / `DataSlice` / snapshot contracts.
- Keep KIS and local StockProgram services behind adapters.
- Make indicator state sleeve-namespaced, in-memory, and snapshot-readable.
- Measure performance separately for data collection, replay construction, and `IndicatorEngine.on_data`.
- Add enough structured logging that a long-running server can be debugged from logs without attaching a debugger.

## Implemented Slices

- Core engine/sleeve/algorithm/execution skeleton.
- Virtual backtesting provider and backtest metrics.
- 30+ indicator catalog and `IndicatorEngine`.
- `IndicatorSnapshot`, `IndicatorSnapshotStore`, `MarketDataSnapshot`, and best-effort snapshot collection.
- Daily 200-symbol indicator benchmark.
- Live market-data-engine quote snapshot runner with configurable pace and mixed US exchange metadata.
- JSON/rotating logging for provider calls and snapshot lifecycle events.

## Next Slices

1. Background snapshot worker:
   - continuously loops over active universe symbols
   - closes `MarketDataSnapshot`
   - updates indicators
   - publishes active `IndicatorSnapshot`

2. Snapshot freshness policy:
   - max age
   - min success ratio
   - failed symbol threshold
   - stale/degraded state exposed to alpha/risk

3. Pipeline model interfaces:
   - `UniverseSelectionModel`
   - `AlphaModel`
   - `PortfolioConstructionModel`
   - `RiskManagementModel`
   - `ExecutionModel`

4. Backtest/live alignment:
   - n-1 minute indicator snapshot for alpha/risk
   - current bar/quote for execution fill simulation
   - replayable snapshot timeline

## Commands

```powershell
py -3 -m pytest -q
```

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli run-once sample_swing_kor_pipeline.json
```

KIS adapter smoke check, when the local broker-engine bridge is running:

```powershell
$env:PYTHONPATH='src'
py -3 -c "from leaps_quant_engine.adapters.kis import KISBrokerEngineMarketDataProvider; p=KISBrokerEngineMarketDataProvider.from_env(); print(p.health_check())"
```

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli kis-health
```

Daily indicator benchmark:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli benchmark-indicators-daily configs/universes/benchmark_kor_200.json --sleeve-id benchmark-kor
```

Live snapshot smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3 --rate-limit-per-second 20
```

Live snapshot smoke with server/debug logs:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli --log-level INFO --log-json --log-file logs/live-snapshot.jsonl live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3
```

## Logging

Logs are designed for server debugging and should not pollute stdout JSON reports.

- stdout: command result JSON
- stderr: human or JSON logs
- `--log-file`: rotating file logs, default 10 MB x 5 backups
- `--log-json`: JSON Lines for machine parsing

Important event names:

- `market_data_snapshot.collect.start`
- `market_data_snapshot.collect.symbol_failed`
- `market_data_snapshot.collect.complete`
- `market_data_engine.call.rate_limited`
- `indicator_snapshot.publish`
- `live_indicator_snapshot.complete`

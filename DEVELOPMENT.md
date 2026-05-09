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
- `SnapshotFreshnessPolicy` and quality reports for `fresh/degraded/stale/invalid` snapshot states.
- `WarmupPolicy` and daily cache-first indicator warmup reports.
- `BackgroundSnapshotWorker` bounded/background cycle runner.
- Python alpha plugin loading, `AlphaRuntime`, and `InsightBatch` publication.
- Universe selection domain with momentum/liquidity active selector and forced watchlist merge.
- Fine universe cache refresh runtime between coarse and active selection.
- Daily 200-symbol indicator benchmark.
- Live market-data-engine quote snapshot runner with configurable pace and mixed US exchange metadata.
- JSON/rotating logging for provider calls and snapshot lifecycle events.

## Next Slices

1. Universe selection runtime:
   - schedule coarse-to-active selection
   - publish active `UniverseDefinition`
   - safely update worker target symbols at boundaries
   - preserve held/open-order/exit-watch forced inclusion

2. Portfolio construction minimum:
   - consumes `InsightBatch`
   - emits portfolio target proposals
   - preserves alpha id/source snapshot lineage
   - does not create broker orders

3. Production worker wrapper:
   - process/supervisor entrypoint
   - graceful stop/restart
   - heartbeat logs
   - operator-visible current active snapshot

4. Pipeline model interfaces:
   - `UniverseSelectionModel`
   - `AlphaModel`
   - `PortfolioConstructionModel`
   - `RiskManagementModel`
   - `ExecutionModel`

5. Backtest/live alignment:
   - n-1 minute indicator snapshot for alpha/risk
   - current bar/quote for execution fill simulation
   - replayable snapshot timeline

## Commands

```powershell
py -3 -m pytest -q
```

Runtime config validation:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/live_us_smoke.json
```

Configured one-cycle runtime smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-run-once configs/runtime/live_us_smoke.json --sleeve-id us-live --held IBM --skip-warmup --summary-only
```

Runtime config rules:

```text
keep config to settings and module references
keep strategy/ranking/risk logic in Python modules
reload config only through RuntimeControlCommand at cycle boundaries
```

Framework pipeline v0:

```text
IndicatorSnapshot
  -> AlphaRuntime
  -> InsightManager
  -> EqualWeightPortfolioConstructionModel
  -> PassThroughRiskManagementModel
  -> ImmediateExecutionModel
  -> OrderIntent
```

`Insight` is a prediction artifact. It may include expiry, magnitude, confidence, and weight hints, but it must not include broker order details. Portfolio, risk, and execution remain separate stages.

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

Daily indicator warmup:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli warmup-indicators-daily configs/universes/swing_kor_core.json --sleeve-id swing-kor --summary-only
```

Live snapshot smoke:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3 --rate-limit-per-second 20
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

Active universe selection:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli select-active-universe configs/universes/benchmark_kor_200.json --sleeve-id benchmark-kor --top-n 60 --summary-only
```

Active-only live snapshot worker:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli active-snapshot-worker-run configs/universes/us_live_smoke.json --sleeve-id us-live --top-n 2 --held IBM --cycles 1 --interval-seconds 0 --skip-worker-warmup --min-success 2 --rate-limit-per-second 20 --summary-only
```

Fine universe refresh:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli fine-universe-refresh configs/universes/us_live_smoke.json --rate-limit-per-second 20 --include-entries
```

Recent US smoke timing:

```text
fine refresh 4 symbols: about 857 ms
active worker 3 symbols: about 2,358 ms collection
indicator update + snapshot publish: about 0.206 ms
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
- `background_snapshot_worker.warmup.complete`
- `background_snapshot_worker.cycle.complete`
- `alpha_runtime.pending.activate`
- `alpha_runtime.batch.publish`

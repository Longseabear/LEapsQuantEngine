# Current Status

Last updated: 2026-05-11

This document records what is currently implemented and what should come next. It is intentionally operational: keep it close to the code and update it whenever a public runtime contract changes.

Model authoring contracts for universe selection, alpha, portfolio construction, risk, and execution live in `docs/model-authoring-guide.md`.

## Implemented

### Core Engine

- `Symbol`, `Bar`, `DataSlice`, `PortfolioTarget`, and `OrderIntent` domain models.
- `Algorithm` interface with a LEAN-like `on_data` boundary.
- `Engine` loop that routes `DataSlice` through sleeves.
- `Sleeve` and `SleevePolicy` for cash/policy compartments.
- `ExecutionModel` that creates order intents instead of broker orders,
  including order type, limit price, time-in-force, and optional slice lineage.
- Runtime config loaders and a sample swing pipeline.

### Backtesting

- `VirtualMarketDataProvider` for deterministic replay.
- `FinanceDataReaderMarketDataProvider` for multi-year daily historical backtests.
- `run_backtest(...)` with immediate fill simulation.
- Simulated fills support pluggable slippage:
  - default `ZeroSlippageModel`
  - `FixedBpsSlippageModel` for side-adjusted buy/sell fill prices
  - CLI option `--slippage-bps` on `framework-backtest-daily` and
    `runtime-backtest-daily`
  - `OrderEvent.metadata` records `reference_price`, `fill_price`,
    `slippage_bps`, and `slippage_cost`
- Simulated fills also support transaction-cost models:
  - default `ZeroFeeModel`
  - `FixedRateFeeModel` for configurable commission/tax/regulatory bps
  - `KisFeeModel` as a KIS-style preset for research simulations
  - CLI option `--fee-model kis` on daily framework/runtime backtests
  - fill metadata records `fee`, `commission`, `taxes`, `regulatory_fee`, and
    `fee_model`; backtest metrics report `fee_cost` and
    `total_friction_cost`
- `run_framework_backtest(...)` for LEAN-style alpha framework replay:
  - historical `DataSlice`
  - `IndicatorEngine`
  - `IndicatorSnapshot`
  - `FrameworkRunner`
  - immediate fills
  - `BacktestMetrics`
  - optional debug ledger via `--include-insights`, including cycle-level
    new/active insights, insight-manager changes, and selection cycle details
- `runtime-backtest-minute` for runtime-config-based minute replay from a local
  CSV/JSON/JSONL feed:
  - keeps runtime config, sleeve workspace, selection, alpha, portfolio, risk,
    and execution wiring intact
  - uses daily history only for warmup
  - treats unspecified indicator resolutions as confirmed daily indicators, so
    minute bars do not accidentally advance daily momentum/SMA/ATR windows
- `download-us-minute-feed` creates standard US minute replay CSV files from a
  runtime config or universe file. The v0 provider is optional `yfinance`; the
  downloader chunks 1-minute requests and writes
  `symbol,time,open,high,low,close,volume` for direct `runtime-backtest-minute`
  consumption.
- Recent US ETF minute feed import:
  - runtime config: `configs/runtime/us_etf_rotation_sleeve.json`
  - sleeve: `us_etf_rotation`
  - period: `2026-05-01` -> `2026-05-10`
  - symbols requested/downloaded: 16 / 16
  - output: `data/replay/us_etf_rotation_20260501_20260510_minute.csv`
  - rows: 37,376
- Recent US ETF minute replay smoke:
  - minute slices: 2,340
  - daily warmup bars: 992
  - final cash: 3,434.25 USD
  - insights/orders: 0 / 0
  - interpretation: the minute feed and replay path work; the configured ETF
    selection returned no active symbols in that window, so strategy input
    selection should be checked separately before treating this as a trading
    signal result.
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
  - slippage cost and realized slippage bps

### Market Replay

- `MarketReplayStore` records normalized `DataSlice` rows as JSONL under a session/sleeve path.
- `run_framework_replay(...)` replays stored `DataSlice` rows through the same `IndicatorEngine -> FrameworkRunner -> immediate fill` path used by framework backtests.
- Replay sessions reserve paths for agent-readable engine status and order intent JSONL:

```text
data/replay/sessions/<session_id>/<sleeve_id>/
  data_slices.jsonl
  engine_status.jsonl
  order_intents.jsonl
```

The v0 replay store is intentionally bar/snapshot oriented. It does not yet persist tick-level broker packets or full `FrameworkCycleResult` objects.

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

### Fundamentals

Fundamental data is intentionally separate from indicators. Indicators consume normalized `Bar` data such as OHLCV. Fundamentals represent point-in-time company facts such as PER, PBR, EPS, ROE, market cap, and dividend yield.

Implemented contracts:

- `FundamentalValue`
- `FundamentalSnapshot`
- `PointInTimeFundamentalStore`
- `FinanceDataReaderFundamentalProvider`
- `FundamentalArtifact`
- `FileFundamentalArtifactStore`

The first v0 flow is:

```text
PointInTimeFundamentalStore
  -> latest known value where value.as_of <= cycle time
  -> FundamentalSnapshot
  -> SnapshotContext.fundamental(symbol, name)
  -> AlphaModel.generate(context)
```

`SnapshotContext` can now carry both `IndicatorSnapshot` and optional `FundamentalSnapshot`. Alpha models should read values through `context.fundamental("KRX:005930", "per")`. They must not query KIS, FinanceDataReader, a database, or static metadata directly.

Backtest support is optional and point-in-time: `run_framework_backtest(...)` and `run_framework_replay(...)` can receive a `PointInTimeFundamentalStore`, and each cycle builds a snapshot from values already known at that cycle's `as_of`. This prevents a future PER from leaking into an earlier backtest date.

The first provider adapter is StockProgram-inspired:

```text
FinanceDataReader StockListing("KRX")
  -> market cap / listed shares / turnover / latest listing snapshot fields
  -> optional Naver market-sum valuation enrichment
  -> FundamentalArtifact JSON
  -> PointInTimeFundamentalStore
```

StockProgram used FDR for the base domestic research universe and Naver market-sum pages for valuation fields such as PER, PBR, EPS, BPS, ROE, ROA, DPS, and dividend yield. The new adapter keeps that provider-specific work outside alpha models. Important caveat: this is a current snapshot import unless the caller supplies archived snapshots with their historical `as_of`; it is not by itself a historical point-in-time PER database.

Fundamental artifacts are stored as date-stamped JSON snapshots under `data/fundamentals/{market}/{YYYY-MM-DD}.json` by default. The CLI surface is:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli fundamentals-import-fdr --market KRX --as-of 2026-05-08 --symbol 005930 --name per --name market_cap --summary-only

$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli fundamentals-import-fdr --universe configs/universes/swing_kor_core.json --as-of 2026-05-08 --name per --name pbr --name market_cap --summary-only

$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli fundamentals-status --market KRX --summary-only
```

`fundamentals-import-fdr` refuses to overwrite an existing same-market/same-date artifact unless `--overwrite` is supplied, so a current snapshot cannot silently replace a historical import.

`framework-backtest-daily` can now load archived fundamental artifacts into the point-in-time store:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli framework-backtest-daily configs/universes/swing_kor_core.json examples/alpha/value_momentum_alpha.py --sleeve-id LEaps --fundamentals-root data/fundamentals --fundamental-name per --fundamental-name pbr --summary-only
```

The backtest CLI loads all matching artifacts with `artifact.as_of <= --end` and lets `PointInTimeFundamentalStore` decide the latest value available at each cycle. This keeps archived PER/PBR snapshots out of earlier dates where they were not yet known.

`runtime-backtest-daily` can run a sleeve directly from runtime config. It builds
the configured alpha, portfolio, risk, execution, and selection models, replaces
live providers with a daily backtest provider, and runs `universe.active.selection_models`
inside each backtest cycle before alpha generation.
Runtime daily backtests now support a separate warmup window. If
`--warmup-start` is supplied, or if a runtime sleeve has
`indicators.warmup_enabled=true` and `extra_bars` configured, pre-start daily
bars are loaded into `IndicatorEngine` before the evaluation window begins.
Those bars increment `warmup_data_slice_count` and do not run
alpha/portfolio/risk/execution or affect performance metrics. This prevents
short backtests such as a one-day `2026-05-08` check from starting with cold
daily momentum indicators.
The LEaps workspace config now uses `configs/universes/leaps_kr_us_research_core.json`,
a KR/US mixed research universe with KRX stocks, US stocks, and defensive US
ETFs. The active thesis is KRW/KOSPI upside with a USD stability hedge:
`leaps-kospi-conviction` emits concentrated KRX growth insights,
`leaps-us-stability-hedge` emits defensive US ETF insights, and
`leaps-volatility-trailing-stop` remains the exit/risk-reduction alpha. The
default sleeve is still present in the same config with zero cash and no alpha
modules, so order/target activity should be isolated to LEaps when
`--sleeve-id LEaps` is used.
Backtest reports now include `final_cash_by_currency`,
`final_equity_by_currency`, and `metrics_by_currency`. For mixed KRW/USD runs,
the aggregate `metrics` block is marked `valid_without_fx=false` and
`currency_mode=multi_currency_native_sum` because there is no FX conversion
layer yet. Agents should use `metrics_by_currency` for sleeve health checks.
LEaps portfolio construction now points at a PPO-based RL constructor:
`sleeves/LEaps/portfolios/rl_ppo_constructor.py`. The trained policy artifacts
are local under `data/rl/` and the runtime model emits allocation percentages
only; order sizing, risk, execution, and fills remain deterministic engine
stages. The current profile follows FinRL Contest-style lessons: use multiple
PPO seeds with median-action ensemble inference to reduce policy instability,
and use market-feedback rewards that prefer Sharpe/drawdown shape over raw CAGR.
The current saved policy uses `AttentionPortfolioFeaturesExtractor`, so top-k
candidate tokens are passed through a Transformer encoder before PPO chooses the
direct top-k asset weights plus a cash weight. `risks/kospi_growth_us_hedge.py`
then applies currency-specific clamps: higher KRW exposure for the KOSPI thesis
and lower USD exposure for the hedge pocket.
The current chosen LEaps allocation profile is `allocation_mode=rl_weights`.
The previous attention-PPO gross-exposure controller remains available as a
comparison profile, but the active runtime now lets RL decide portfolio
percentages rather than equal-splitting selected signals. A second five-trial
turnover-aware search selected `identity_turnover_top8_compact`: top-k 8, seed
53, and an asset-identity turnover reward that penalizes actual symbol weight
changes rather than only token-position changes.
The current LEaps RL constructor also treats same-symbol FLAT/DOWN insights as
an override over same-or-older UP insights. This lets
`leaps-volatility-trailing-stop` force an exit target for held symbols without
alpha models creating orders directly.
LEaps alpha v0.2 upgrades the momentum and ETF rotation modules with
risk-adjusted scoring. Momentum now uses fast/slow trend confirmation,
20-session momentum, short acceleration, normalized volatility, and liquidity,
and it does not emit FLAT for every rejected candidate. ETF rotation now uses a
trend filter and normalized volatility penalty.

Recent LEaps engine-contract validation after warmup/reporting changes:

```text
2026-05-08 one-day runtime backtest:
  warmup_data_slice_count: 64
  data_slice_count: 1
  insights: 6
  orders: 2
  order collisions: 0

2023-05-10 -> 2026-05-08, KRW 2,000,000 only:
  warmup_data_slice_count: 65
  final equity: 2,351,274 KRW
  return: 17.56%
  MDD: 5.84%
  orders: 512
  order collisions: 0

2021-05-10 -> 2026-05-08, configured KRW/USD cash:
  KRW final equity: 12,037,393 KRW
  KRW return: 20.37%
  USD final equity: 3,220.60 USD
  USD return: -6.22%
  order collisions: 0
  aggregate metrics: valid_without_fx=false

default sleeve one-day isolation:
  insights: 0
  orders: 0
```

The strategy is functional but turnover-heavy in long runs. Treat turnover
reduction as portfolio/risk policy work, such as minimum rebalance drift,
cooldown, or turnover guards, rather than model side effects.

Recent mixed KR/US OOS backtest with 2026-05-08 included, using the direct
RL-weight allocator:

```text
train: 2021-05-10 -> 2024-12-31
test: 2025-01-02 -> 2026-05-08 inclusive
command end argument: --end 2026-05-09
source: FinanceDataReader
resolution: daily
initial cash: KRW 10,000,000 + USD 3,434.25

aggregate, currency-unconverted:
  return: 34.41%
  CAGR: 24.60%
  Sharpe: 2.05
  MDD: 5.98%
  turnover: 36.87
  average exposure: 21.52%

KRW bucket:
  final equity: 13,441,653 KRW
  return: 34.42%
  Sharpe: 2.05
  MDD: 5.98%

USD bucket:
  final equity: 3,545.93 USD
  return: 3.25%
  Sharpe: 0.57
  MDD: 4.28%

final holdings:
  KRX:000660 1
  KRX:005380 1
  US:SCHD 8
  US:USMV 6
```

This direct-weight allocator is conceptually closer to the intended RL portfolio
role and now improves held-out Sharpe, MDD, and turnover versus the previous
direct-weight baseline. The trade-off is lower total return than the more
aggressive top-16 search winner.

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --summary-only
```

Add simulated execution friction with:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --slippage-bps 5 --summary-only
```

Train the LEaps PPO constructor:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli train-rl-portfolio-constructor configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2021-05-10 --end 2024-12-31 --source finance-datareader --timesteps 8000 --seed 53 --output-dir data/rl --summary-only
```

Current ensemble command:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli train-rl-portfolio-constructor configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2021-05-10 --end 2024-12-31 --source finance-datareader --timesteps 8000 --seed 53 --output-dir data/rl --summary-only
```

Recent trained LEaps PPO allocator daily backtest:

```text
source: FinanceDataReader
train: 2021-05-10 -> 2024-12-31
test: 2025-01-02 -> 2026-05-08 inclusive
resolution: daily
initial cash: KRW 10,000,000 + USD 3,434.25
universe: leaps-kr-us-research-core
symbols with data: 31
policy ensemble:
  data/rl/leaps_ppo_portfolio_allocator_seed53.zip
reward profile: finrl_contest_shape_aware
allocation_mode: rl_weights
top_k: 8
data slices: 349
insights: 1,853
orders: 1,037
total return: 34.41%
CAGR: 24.60%
Sharpe: 2.05
MDD: 5.98%
turnover: 36.87
average exposure: 21.52%
trade count: 509
```

Recent US ETF sleeve virtual cash setup:

```text
runtime config: configs/runtime/us_etf_rotation_sleeve.json
logical sleeve: us_etf_rotation
broker account route: kis-overseas
virtual account store: data/virtual-accounts/kis_overseas.json
order runtime store: data/order-runtime/kis_overseas.jsonl
allocated cash: 3,434.25 USD
operator intent: 5,000,000 KRW-equivalent ETF allocation
status check: needs_attention=false, no warnings
```

This is a virtual sleeve allocation only. It does not execute a real KIS FX
conversion, bank transfer, or broker cash movement.

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

Runtime bootstrap now runs indicator warmup before indicator-based active selection when `indicators.warmup_enabled` is true. The warmed `IndicatorSnapshot` is passed into the sleeve's `UniverseSelectionModel`, then the same `IndicatorEngine` is narrowed to the selected live universe so the first live snapshot starts from the warmed state instead of a cold one.

If warmup does not satisfy `min_ready_ratio`, the runtime does not get stuck. It marks the next snapshot quality as degraded with `warmup_not_ready`, which blocks new entries through alpha/risk freshness gates while still allowing the loop, exits, reconciliation, and status reporting to continue.

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
  -> optional active-universe warmup
  -> collect MarketDataSnapshot best-effort
  -> evaluate SnapshotFreshnessPolicy
  -> attach warmup_not_ready entry block when needed
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

Model modules should follow the contracts in `docs/model-authoring-guide.md`. Config wires module references and simple parameters; strategy logic belongs in Python modules.

The running process should not poll and reload config files every cycle. It should keep the active `RuntimeConfigSnapshot` in memory, drain control commands at cycle boundaries, and load a config file only for explicit `reload_config` commands.

`RuntimeSleeveRuntime` can now stage a new sleeve runtime from a fresh config snapshot with `stage_reload(...)`. Staging rebuilds alpha, portfolio, risk, and execution models, then dry-runs the staged framework against the current active indicator snapshot when one exists. `activate_staged_reload()` swaps the staged runtime at a cycle boundary. This keeps reload behavior explicit instead of changing model objects in the middle of a framework cycle.

Runtime bootstrap is also implemented. `bootstrap_sleeve_runtime(...)` takes a validated `RuntimeConfigSnapshot` and builds:

- coarse `UniverseDefinition`
- market-data and history providers
- optional `FineUniverseRuntime` and fine refresh report
- configured `UniverseSelectionModel` or composite `universe.active.selection_models`
- active `UniverseDefinition`
- `AlphaRuntime` from alpha module references
- `PortfolioConstructionEngine` from Portfolio Construction Model references and rebalance policy settings
- sleeve `Portfolio` with configured cash allocation
- `FrameworkRunner` for alpha, insight state, portfolio construction, risk, and execution
- `BackgroundSnapshotWorker`

Sleeves can now declare a `workspace_path`. File-based strategy module references inside `alpha.modules`, `portfolio.model`, `risk.model`, `execution.model`, and active selection models resolve relative to that sleeve workspace. Shared universe paths remain runtime/global paths unless they are absolute.
Sleeve alpha modules can be managed from the CLI:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-alpha-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-enable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-disable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-portfolio-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-portfolio-set configs/runtime/leaps_workspace_smoke.json equal_weight --sleeve-id LEaps
```

These commands update the runtime config and emit a `reload_sleeve` control command payload so an operating agent can apply the change at a runtime boundary. Alpha modules are multi-active; portfolio construction model selection is single-active.

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

Read-only KIS account sync into a configured virtual sleeve account store:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli kis-account-sync configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start-date 20260508 --end-date 20260508
```

Historical/manual assignment of an unknown execution to the requested sleeve is explicit:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli kis-account-sync configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start-date 20260508 --end-date 20260508 --assign-unknown-to-sleeve
```

Allocate a previously recorded raw broker fill into sleeve projections:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli virtual-account-allocate-fill configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --fill-id kis:domestic:12345:20260508T093000:10:70000 --allocation LEaps=6 --allocation ETF=4 --reason initial-sleeve-split
```

Compare broker holdings to the aggregate virtual sleeve projection:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli virtual-account-reconcile configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --summary-only
```

Sync broker cash into the virtual account projection:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli virtual-account-sync-cash configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --residual-sleeve-id "default sleeve"
```

Move cash explicitly between virtual sleeves:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli virtual-account-transfer-cash configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --from-sleeve-id "default sleeve" --to-sleeve-id LEaps --amount 250000 --reason initial-allocation
```

Inspect current order runtime and virtual sleeve account state without touching the broker:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/leaps_workspace_smoke.json --summary-only
```

Persist a runtime cycle's execution output as a submit-ready artifact:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-run-once configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --summary-only --order-batch-output ../../data/order-intents/leaps_latest.json
```

Dry-run or commit order-intent batches into the order runtime:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-submit configs/runtime/leaps_workspace_smoke.json data/order-intents/sample.json --summary-only
py -3 -m leaps_quant_engine.cli order-runtime-submit configs/runtime/leaps_workspace_smoke.json data/order-intents/sample.json --commit --broker paper --summary-only
py -3 -m leaps_quant_engine.cli order-runtime-paper-smoke configs/runtime/leaps_workspace_smoke.json data/order-intents/sample.json --summary-only
```

Run one bounded order maintenance pass. This can poll open tickets, import execution history, reconcile holdings, and still return a warning report instead of stopping the operator loop:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-supervise configs/runtime/leaps_workspace_smoke.json --summary-only
```

### Runtime Journal, Recovery, And Health

The runtime now has an agent-readable stabilization line shared by backtest, paper, and live paths.

- `CycleJournalEntry` is the append-only JSONL cycle record. It captures runtime/config/sleeve/account route identity, snapshot quality, stage counts, timings, warnings, errors, and the current engine source hash.
- `runtime-run-once`, `framework-backtest-daily`, `order-runtime-submit`, `order-runtime-paper-smoke`, and `order-runtime-supervise` can write the same journal shape with `--journal` or `RuntimeConfig.journal_path`.
- `runtime-recovery-status` reads config, cycle journal, order runtime state, and virtual accounts to report `last_cycle`, `open_tickets`, `unallocated_fills`, `account_reconciliation`, blocked reasons, and recommended next actions.
- `runtime-health` is report-only watchdog support for stale cycles, repeated failures, stale snapshots, aged open tickets, unallocated fills, unsupported routes, missing runtime stores, and code changes since the last journaled cycle.
- `runtime-preflight` is the live/paper readiness gate before reload or market-open operation. It computes a runtime fingerprint from engine source, runtime config, universe files, and sleeve model files; verifies account/order store paths; compares the latest journaled config/code identity; and imports/bootstrap-checks each selected sleeve without submitting orders.
- `EngineGuard` is a core safety layer after strategy risk and before order submission. It blocks oversell, cash reservation overflow, missing/invalid prices, route mismatches, duplicate unsafe submit boundaries, and unsupported live overseas broker-engine submission.

Before live/paper operation after code or model edits:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --summary-only
```

Useful interpretation:

- `status=ok`: config/code/model paths/bootstrap and last journal identity line up.
- `status=needs_attention` with `config_changed_since_last_cycle`, `engine_code_changed_since_last_cycle`, or `latest_cycle_missing_code_identity`: stage/reload the runtime and run one journaled cycle before live submission.
- `status=blocked`: fix the config/model import/path issue before the live loop or order submit path continues.

Multi-market sleeves are logical at the sleeve boundary and routed internally by account route:

```json
{
  "sleeve_id": "LEaps",
  "broker_account_id": "kis-domestic",
  "broker_account_routes": {
    "domestic": "kis-domestic",
    "overseas": "kis-overseas"
  }
}
```

The virtual account and order runtime stay account-route separated. Status and recovery aggregate those route portfolios under the logical sleeve. KRW and USD cash are intentionally not merged in v0.

Virtual sleeve accounts now persist currency cash separately:

```json
{
  "cash": 100750.0,
  "cash_by_currency": {
    "KRW": 100000.0,
    "USD": 750.0
  }
}
```

`Portfolio` now has a `CashBook` view with per-currency `Cash` balances. `cash` remains only as a backward-compatible scalar view for existing single-currency surfaces and status output. Multi-currency engine decisions should use `cash_by_currency`, `CashBook`, `cash_by_currency_for(...)`, or `equity_by_currency(...)`.

For multi-market sleeves, global `Portfolio.equity(data)` is intentionally not a valid decision input because KRW and USD are not converted in v0. It raises when multiple currencies are present. Portfolio construction, sizing, risk, order runtime, engine guard, cash sync, and cash transfer use the symbol/account route currency when evaluating target value, exposure, or available cash. Sleeve runtime config can declare `cash_by_currency` so one logical sleeve can own separate KRW and USD balances without duplicating the same `cash` into every route.

Status commands:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-recovery-status configs/runtime/leaps_workspace_smoke.json --summary-only
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/leaps_workspace_smoke.json --summary-only
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

`SnapshotContext` now exposes both indicator and fundamental reads:

```python
price = context.value("KRX:005930", "close")
per = context.fundamental("KRX:005930", "per")
```

`SnapshotContext.symbol_keys` can now be scoped per alpha call by runtime wiring.
`AlphaRuntime.run(..., symbols_by_alpha={alpha_id: symbols})` gives each alpha
model only its selected input symbols while preserving the full underlying
`available_symbol_keys` from the `IndicatorSnapshot`. This keeps the dependency
between selection and alpha as config/runtime wiring, not Python imports between
models.

Runtime config exposes the wiring as `alpha.input_selections`, where each
`alpha_id` maps to a `selection_id`. `RuntimeSleeveRuntime` resolves that map
from the latest active universe selection result and passes it into
`FrameworkRunner.run_once(...)`.

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
  -> OrderSizingEngine
  -> OrderSizingBatch
  -> RiskManagementModel
  -> ExecutionModel
  -> OrderIntent
```

Implemented framework contracts:

- `FrameworkRunner`
- `FrameworkCycleResult`
- `StageTiming`
- `PortfolioAllocationTarget`
- `PortfolioTargetBatch`
- `PortfolioTargetPlan`
- `OrderSizingContext`
- `OrderSizingEngine`
- `OrderSizingBatch`
- `OrderSizingPlan`
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
- `RiskLimits`
- `BasicRiskManagementModel`
- `PassThroughRiskManagementModel`
- `PythonRiskManagementModelLoader`
- `ExecutionContext`
- `ExecutionEngine`
- `OrderIntentBatch`
- `ImmediateExecutionModel`
- `StandardExecutionModel`
- `LimitExecutionModel`
- `MarketExecutionModel`
- `SlicedExecutionModel`
- `PythonExecutionModelLoader`
- `OrderCoordinator`
- `OrderCoordinationResult`
- `OrderIntentCollision`
- `OrderTicket`
- `OrderTicketStatus`
- `OrderEvent`
- `OrderEventType`
- `SimulatedFillModel`
- `BrokerExecutionGateway`
- `BrokerExecutionService`
- `BrokerExecutionResult`
- `PaperBrokerExecutionGateway`
- `BrokerEngineExecutionGateway`
- `OrderAccountStore`
- `MultiSleeveOrderOrchestrator`
- `MultiSleeveOrderOrchestrationResult`
- `OrderRuntimeStateStore`
- `OrderRuntimeSnapshot`
- `FileOrderRuntimeStateStore`
- `OrderRuntimePaperSmokeRunner`
- `OrderRuntimePaperSmokeReport`
- `OrderRuntimeSubmitter`
- `OrderRuntimeSubmitReport`
- `OrderRuntimeStatusReport`
- `SleeveOrderRuntimeStatus`
- `OrderRuntimeSupervisor`
- `OrderSupervisorRunReport`
- `OpenTicketPollWorker`
- `OpenTicketPollReport`
- `ExecutionHistoryClient`
- `ExecutionReconcileAccountStore`
- `ExecutionHistoryReconcileWorker`
- `ExecutionHistoryReconcileReport`

Current v0 behavior:

- Alpha emits new insights only.
- `InsightManager` maintains active/inactive signal state. Active insight ordering is deterministic by generated time, sleeve, symbol, alpha, type, and id so replayed portfolio/risk decisions do not depend on random UUID ordering.
- `PortfolioConstructionEngine` reads active insights and the sleeve virtual account portfolio, then produces auditable `PortfolioTargetBatch` records with target percent and desired value. It no longer performs integer share rounding.
- Portfolio construction is currency-bucket aware. `EqualWeightPortfolioConstructionModel` still emits target percentages, but each percentage is applied inside the target symbol's currency bucket rather than against a mixed global sleeve value. For example, one logical `LEaps` sleeve can allocate 100% of the KRW bucket to a Korean stock while independently allocating 50% / 50% of the USD bucket across two US stocks.
- `PortfolioTargetPlan` records current value, target percent, desired value, and insight lineage for each allocation target.
- `OrderSizingEngine` converts allocation plans into quantity-based `PortfolioTarget` records before risk sees them. It records target quantity, delta quantity, rounded value, and rounding loss, and is the first home for lot-size/min-order discretization.
- A 2,000,000 KRW framework backtest on `swing_kor_core` from 2021-05-10 through 2026-05-08 now makes the discretization boundary visible: `order_count=782`, `avg_exposure=60.61%`, and `raw_target_but_no_sized_order_cycles=426`. Before this split, equal-weight construction was accidentally honoring small alpha weight hints as sizing and produced only `avg_exposure=1.49%`.
- `PortfolioEngineState` folds a framework cycle into an agent-readable portfolio state snapshot:
  - current sleeve cash, equity, exposure, holdings, mark price, and unrealized PnL
  - allocation batch and sized target batch
  - risk decisions
  - pending order intents with reserved buy cash and reserved sell quantities
- The core assumes sleeve-level virtual accounts already exist. KIS account-level holdings are a broker adapter concern, not the portfolio construction source of truth.
- Runtime bootstrap can now read the current sleeve portfolio through a `PortfolioProvider` before each framework cycle. The default provider preserves the configured starting cash behavior, while tests prove a `LEaps` sleeve can supply its own current virtual portfolio.
- `VirtualSleeveAccountStore` is the first file-backed virtual account provider for live/paper ownership state. It stores sleeve cash/holdings, `order_id -> sleeve` ownership, broker order aliases, and idempotent fill events. Unknown external fills are routed to the `unassigned` sleeve.
- Runtime config can opt a sleeve into this store with `portfolio.account_store_path`. For account-level operations it can also define top-level `broker_accounts` and let each sleeve set `broker_account_id`. `configs/runtime/leaps_workspace_smoke.json` now routes `LEaps` and `default sleeve` to `kis-overseas`, with a separate `kis-domestic` profile available for Korean-account sleeves.
- `BrokerAccountRuntimeConfig` separates account identity from sleeve strategy config: `account_id`, `market_scope`, virtual account store path, order runtime store path, and gateway choice. Order runtime commands resolve stores from this profile first, then fall back to legacy sleeve `portfolio.account_store_path`.
- Real KIS account attachment is now a read-only sync path, not a direct portfolio source. `KISVirtualAccountSync` calls broker-engine operations for balance, holdings, and execution history, converts executions into `VirtualFillEvent` records, applies owned or explicitly assigned fills into `VirtualSleeveAccountStore`, and records unknown broker fills for later allocation. KIS holdings are reported for reconciliation but do not overwrite sleeve holdings.
- `FillAllocation` supports partial or full splitting of one broker fill across multiple virtual sleeves by quantity. The broker fill stays in the raw fill ledger, and each sleeve portfolio projection gets only its allocated quantity and proportional fee. This is intentionally lighter than StockProgram's order-chain-lot model: the LEAN engine still sees only `PortfolioProvider.current_portfolio(sleeve_id)`.
- Unknown broker executions are not silently turned into strategy positions. The operator can intentionally assign unknown fills to a sleeve with `--assign-unknown-to-sleeve`, or record them first and later distribute them with `virtual-account-allocate-fill`.
- `VirtualSleeveAccountStore` can report allocation status per broker fill (`unallocated`, `partially_allocated`, `fully_allocated`) and reconcile KIS current holdings against the aggregate virtual sleeve projection. This gives operators a bounded daily check instead of replaying all historical fills during every engine cycle.
- KIS cash balance sync follows the StockProgram lesson without copying the whole fund interface. `virtual-account-sync-cash` stores the KIS account cash snapshot, keeps strategy sleeve cash as internal allocation state, and assigns residual cash to `default sleeve`. `virtual-account-transfer-cash` moves cash between virtual sleeves explicitly.
- Backtesting remains separated: `run_backtest(...)`, `run_framework_backtest(...)`, and `run_framework_replay(...)` still receive an explicit in-memory `Portfolio` and do not read or write the live/paper virtual account store.
- The `LEaps` sleeve has an initial workspace at `sleeves/LEaps` with sleeve-local `selections/stock_momentum.py`, `selections/etf_rotation.py`, `selections/operational_symbols.py`, `alphas/momentum.py`, `alphas/volatility_trailing_stop.py`, `alphas/etf_rotation.py`, `portfolios/equal_weight.py`, `risks/basic.py`, and `executions/immediate.py` modules, plus `configs/runtime/leaps_workspace_smoke.json` wired to the USD research universe `configs/universes/leaps_us_research_core.json`.
- `configs/runtime/leaps_workspace_smoke.json` wires those selectors through `alpha.input_selections`: momentum alpha receives stock-momentum candidates, ETF rotation receives ETF candidates, and the trailing-stop alpha receives operational symbols such as held/open/manual symbols.
- Each runtime cycle emits an agent-readable `engine_status` log line through the `leaps_quant_engine.agent_status` logger. The status includes snapshot quality, symbol update counts, portfolio cash/equity, active insight count, target/plan counts, risk approval count, and order intent count.
- `runtime-run-once --order-batch-output` writes the framework `execution_batch` as an `order_intent_batches.v1` JSON artifact. The file is intentionally the same shape consumed by `order-runtime-submit`, so the paper/live order path can be tested from a captured strategy output rather than hand-written order JSON.
- `order-runtime-paper-smoke` runs the paper lifecycle from an artifact in one command. It commits through `order-runtime-submit` with paper broker submission but no immediate poll, then runs the supervisor's paper poll to produce fill events and final status. This proves the submit/supervise boundary instead of hiding fill application inside submit.
- `order-runtime-submit` is the explicit boundary between strategy order intent and account-level order lifecycle. It reads `OrderIntentBatch` JSON, validates sleeve/symbol/notional guards, dry-runs `OrderCoordinator` by default, and commits only with `--commit`. Broker-engine submit is blocked unless `--confirm-live-submit` is also present.
- `EngineGuard` now checks the order runtime store before commit and rejects duplicate `batch_id:index` order intent ids or ticket ids that were already recorded. Dry-run reports the same condition as a warning so an operator can see that a captured artifact was already submitted.
- `order-runtime-status` provides an explicit operator/agent read model for order operations. It combines the append-only order ticket/event store with the virtual sleeve account store and reports broker account route, market scope, open tickets, ticket/event counts, sleeve cash/holdings, pending buy notional, pending sell quantities, and unallocated broker fills. It does not submit, poll, or reconcile broker orders.
- `order-runtime-supervise` is the first bounded order maintenance command. It can poll stored open tickets through paper or broker-engine gateways, import recent execution history through broker-engine account operations, run holdings reconciliation, and then attach the final `order-runtime-status` view. Setup, poll, and reconcile failures are returned as warnings so an agent loop can continue. Overseas broker-engine poll/reconcile is blocked explicitly until an overseas broker-engine adapter exists.
- `EqualWeightPortfolioConstructionModel` remains the first model implementation. It emits equal target percentages for active insights instead of quantity targets.
- `RebalancePolicy` can reserve cash, filter tiny quantity deltas, and suppress tiny non-exit order notionals through `OrderSizingEngine`.
- Portfolio Construction Models can be loaded from Python model modules through `PythonPortfolioConstructionModelLoader`.
- When previously managed or currently held symbols lose active insight support, portfolio construction can emit flatten targets.
- Risk runs every framework cycle. The default `FrameworkRunner` risk model is now `BasicRiskManagementModel`, which enforces v0 long-only behavior, per-symbol max position percentage, portfolio-level gross exposure percentage, available-cash clamps, and snapshot-quality entry gates before execution sees targets.
- Sleeves can inject risk models through `risk.model` and `risk.parameters`. File-based risk model references resolve relative to `workspace_path`, matching alpha and portfolio model loading.
- The `LEaps` sleeve workspace now includes `risks/basic.py`, a minimal example using `BasicRiskManagementModel` with `long_only`, `max_position_pct`, `max_total_exposure_pct`, `cash_buffer_pct`, and snapshot-quality gate parameters.
- `PassThroughRiskManagementModel` remains available for tests and controlled smoke scenarios.
- Execution now has a small `ExecutionEngine` that wraps a sleeve execution model and returns an auditable `OrderIntentBatch`.
- Sleeves can inject execution models through `execution.model` and `execution.parameters`. File-based execution model references resolve relative to `workspace_path`, matching alpha, portfolio, and risk loading.
- Execution models convert approved targets into `OrderIntent` records only. `ImmediateExecutionModel` remains the default one-ticket limit model, while `StandardExecutionModel`, `LimitExecutionModel`, `MarketExecutionModel`, and `SlicedExecutionModel` can express market/limit style, time-in-force, limit offsets, and quantity/notional slicing. They do not submit broker orders.
- `OrderIntent` and `OrderTicket` now preserve execution instructions: `order_type`, `limit_price`, `time_in_force`, and metadata. Simulated fills can enforce explicit limit prices with `SimulatedFillModel(enforce_limit_price=True)`, while the default research fill path remains immediate-fill for compatibility.
- `market_rules.py` centralizes KIS/KRX-style order constraints. Domestic KRX
  limit prices use the KRX tick table, whole-share quantity is required, and
  `MarketSession` separates regular-open from orderable after-hours phases.
  Confirmed live broker-engine submit can require an orderable session before
  commands are sent to KIS.
- `BrokerEngineExecutionGateway` rounds domestic limit prices to the side-safe
  KRX tick grid before submission: buys round up, sells round down. Market
  orders submit with price `0`.
- `EngineGuard` now checks whole-share quantity, route-supported order style,
  optional orderable market session, and off-tick KRX limit prices. Off-tick
  prices are warnings because the broker gateway can side-safe round them.
- `OrderCoordinator` can collect one or more `OrderIntentBatch` records, create `OrderTicket` records, emit created events, and record same-symbol cross-sleeve buy/sell collisions without rejecting the tickets.
- Sleeve-level execution may legitimately produce same-symbol buy and sell intents across different sleeves. The global order coordinator treats that as a collision/arbitration case, not as an automatic model error.
- `OrderTicket` is the order lifecycle synchronization object. It preserves the link from sleeve intent to broker identity and applies normalized order events to its own ticket state. Portfolio and virtual sleeve account state change from fill/reconciliation events, not from the ticket object itself.
- `SimulatedFillModel` is the current backtest fill model. It emits immediate filled events from tickets; backtests now apply those fill events to portfolios instead of mutating holdings directly from order intents.
- `BrokerExecutionGateway` is now the live/paper side-effect boundary after `OrderTicket`. The gateway returns `OrderEvent` records only; it does not mutate portfolio holdings.
- `PaperBrokerExecutionGateway` can submit tickets and emit deterministic paper fills through the same broker-event surface.
- 2026-05-10 update: the default live KIS boundary has moved in-process. `KISDirectClient` now owns KIS REST calls, token reuse, local daily/minute cache files, domestic account reads, execution-history reads, and domestic order submit/cancel. The legacy broker-engine path remains a compatibility/reference concept.
- `BrokerEngineExecutionGateway` is the first StockProgram-inspired live broker adapter. It can submit domestic KRX tickets through the local broker-engine `place_domestic_cash_order` operation, prefers the broker-engine command queue when available, includes StockProgram-style dedupe metadata (`consumer_id`, `plan_id`, `chain_id`, `strategy_leg_id`, `intent_id`), and can poll broker-engine `command_status` snapshots to turn queued commands into accepted or rejected order events. It is domestic-only today; overseas sleeve routes are accepted for paper/status but blocked for broker-engine side effects.
- `VirtualSleeveAccountStore` can register `OrderTicket` ownership, bind later broker order aliases, and apply `OrderEvent` fills into the sleeve portfolio. This keeps the coupling one-way: broker/order events feed the virtual account ledger, while strategy and portfolio construction continue to read only `PortfolioProvider.current_portfolio(sleeve_id)`.
- Broker fills are still synchronized from KIS execution history or broker-engine events after submission. The broker gateway does not treat submit acceptance as a holding change.
- `MultiSleeveOrderOrchestrator` is now the account-level bridge after sleeve execution models run. It collects one or more sleeve `OrderIntentBatch` records, runs `OrderCoordinator`, registers ticket ownership in `VirtualSleeveAccountStore`, submits tickets through a `BrokerExecutionService`, optionally polls immediately, applies broker/order events back to the virtual account ledger, and returns an agent-readable orchestration report.
- The live-style broker-engine ownership path is covered by a test: command-queue submit records the local order intent under the sleeve, command-status polling binds the actual KIS branch/order number alias, and later execution-history sync resolves that broker order id back to the original sleeve before applying the fill.
- Paper order orchestration is executable end-to-end: a paper gateway can submit tickets, emit fill events on poll, and update only the affected virtual sleeve portfolios. Created/submitted/accepted events can bind ownership and broker identity, but holdings still change only from fill events.
- `FileOrderRuntimeStateStore` records tickets and order events as append-only JSONL. It can reconstruct ticket status from stored lifecycle events after restart, deduplicates repeated event ids during replay, and exposes open tickets for continued broker polling.
- `MultiSleeveOrderOrchestrator` can now write coordination, submit, and poll events to an optional `OrderRuntimeStateStore` as it runs. This makes paper/live order handling restart-observable without letting the order store mutate portfolio holdings.
- `OrderRuntimeSubmitter` wraps this orchestrator behind CLI-friendly submit guards. Dry-run produces tickets/collision visibility without writing runtime state. Paper commit can submit and optionally poll immediate fills. Broker-engine commit requires explicit live confirmation.
- `OrderRuntimePaperSmokeRunner` composes submitter and supervisor for a paper-only end-to-end smoke. It uses the same order runtime store and virtual account store that live/paper runtime uses, so it exercises restart-observable ticket/event state without touching broker-engine.
- `OpenTicketPollWorker` reads stored open tickets after a process restart, optionally filters by sleeve, calls `BrokerExecutionService.poll(...)`, appends newly observed broker/order events to the order runtime store, and applies those events to the virtual sleeve account ledger. Paper mode now proves that a submitted ticket can survive restart and then become a filled holding on the next poll.
- `ExecutionHistoryReconcileWorker` imports recent broker execution history through broker-engine account operations and is designed not to get stuck on bad broker data. It catches history/holdings fetch failures, skips malformed execution rows, records unknown fills for later sleeve allocation, applies owned fills to the virtual sleeve account, and reports warnings instead of stopping the loop.
- `OrderRuntimeSupervisor` composes the poll worker, execution-history reconcile worker, and final status report into one bounded operation. This is the CLI-first control surface that a later UI or agent supervisor can call.
- `OrderRuntimeSupervisor` also has an `OrderMaintenancePolicy`. It can report
  stale open tickets or cancel them through the same broker gateway, including
  stale partial fills when policy allows it. This keeps cancel/replace-style
  maintenance out of alpha/portfolio/risk code.
- `NotificationService` brings over the StockProgram local-first Telegram alert pattern. It writes outbox/history JSON records under `data/notification-engine`, sends Telegram only when `LEAPS_TELEGRAM_BOT_TOKEN` and chat id are available, accepts StockProgram env names as migration fallback, and never lets alert failure block order reflection.
- `order-runtime-submit --notify` and `order-runtime-supervise --notify` can emit compact mechanical order alerts. These alerts contain runtime, account, order/ticket/fill counts, and errors/warnings only; strategy reasoning remains in agent notes and journals.
- Execution-history reconciliation also checks the order runtime store's existing fill events before applying broker history fills. This prevents a fill that was already reflected from a broker/order event from being applied a second time when the same execution later appears in KIS history.
- KIS execution-history fill ids prefer explicit execution/fill ids when the broker payload provides them. If the broker-engine row is an order-level execution summary (`source_granularity=order_execution_summary`), the fill id is stable by order and timestamp instead of quantity/average price, preventing a revised summary row from double-applying as a new fill.
- `VirtualSleeveAccountStore.ownership_for_order(...)` now resolves broker order aliases too, so KIS execution rows keyed by broker order number can map back to the sleeve-owned order intent when the local store knows the broker id.
- Research backtests should be able to run one sleeve at a time with isolated cash, holdings, alpha, portfolio, risk, and execution settings. Live/paper orchestration should run all active sleeves together and then coordinate their order intents through the account-level buy/sell layer.
- `runtime-run-once` now runs this framework path after `BackgroundSnapshotWorker` publishes the active indicator snapshot.
- `bootstrap_sleeve_runtime(...)` pre-warms indicators before active selection when `indicators.warmup_enabled=true`, so indicator-based selection models do not start from an empty snapshot on the first live cycle.

### Universe Selection

Universe selection now has a v0 domain structure.

Implemented contracts:

- `UniverseSelectionContext`
- `UniverseSelectionCandidate`
- `UniverseSelectionResult`
- `CompositeUniverseSelectionResult`
- `CompositeUniverseSelectionRuntime`
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

Multiple selection models can now run for one sleeve cycle. Each model result
keeps its `selection_id`, and `CompositeUniverseSelectionRuntime` unions their
selected symbols into the live universe while still preserving per-model inputs:

```text
stock_momentum_top_n -> symbols for momentum alpha
etf_rotation_top_20  -> symbols for ETF rotation alpha
operational_symbols  -> symbols for exit/stop alpha

live_universe =
  union(all selected symbols)
  + held/open/exit/manual forced symbols
```

The alpha runtime receives a separate `alpha_id -> symbols` mapping, so one
selection can feed many alpha models or one alpha can be wired to any selection
result without code-level coupling. This keeps model code independent: selectors
choose candidate symbols, alpha models read only their assigned input symbols,
and config decides which selector feeds which alpha.

Runtime config supports this with `universe.active.selection_models`. If that
list is absent, the existing single `universe.active.selection_model` path is
used for backward compatibility.

Backtests now use the same wiring when `selection_models` and
`alpha_input_selections` are passed to `run_framework_backtest(...)` or when the
operator uses `runtime-backtest-daily`.

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
KISDirectClient / local KIS file cache
  -> MarketDataEngineLiveQuoteProvider / KISCachedMarketDataProvider
  -> normalized Bar
  -> MarketDataSnapshot
```

Implemented adapters:

- `KISBrokerEngineMarketDataProvider`
- `KISCachedMarketDataProvider`
- `MarketDataEngineLiveQuoteProvider`
- `KISDirectClient`

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

Recent LEaps framework backtest using the CLI default long-history source:

```text
command:
  framework-backtest-daily configs/universes/swing_kor_core.json examples/alpha/price_above_sma_alpha.py --sleeve-id LEaps --start 2021-05-10 --end 2026-05-08 --cash 6385012 --source finance-datareader --summary-only

source: finance-datareader
period: 2021-05-10 -> 2026-05-08
data slices: 1,224
insights: 1,600
orders: 653
final cash: 730,312
final positions:
  KRX:005930 27
  KRX:035420 34
  KRX:000660 4
total return: 245.09%
CAGR: 28.15%
Sharpe: 1.02
MDD: 41.91%
turnover: 18.39
trade count: 318
```

After wiring the default risk model to `BasicRiskManagementModel`, the same run produced:

```text
period: 2021-05-10 -> 2026-05-08
data slices: 1,224
insights: 1,600
orders: 650
final cash: 779,312
final positions:
  KRX:005930 27
  KRX:000660 4
  KRX:035420 34
total return: 245.85%
CAGR: 28.21%
Sharpe: 1.02
MDD: 41.52%
turnover: 18.02
trade count: 315
```

The 2026-05-08 one-day smoke with the same source produced no insights/orders and kept equity unchanged at 6,385,012.

KIS/broker-engine daily cache limitation observed on 2026-05-09:

```text
requested period: 2021-05-10 -> 2026-05-08
returned period: 2026-03-26 -> 2026-05-08
returned sessions per symbol: 30
```

The legacy broker operation currently applies `start_date` / `end_date` filtering after receiving the KIS daily payload. If the provider payload contains only recent rows, the new engine cannot expand that into a five-year replay. Treat KIS daily cache as a recent-cache smoke until a paged KIS history path or a dedicated historical provider adapter is implemented.

`framework-backtest-daily` defaults to `--source finance-datareader` for long-horizon daily backtests. Use `--source kis-cache` only for recent smoke tests of the broker-engine cached-history path.

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
322 passed, 1 warning
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
- Freshness/degraded-state reporting exists. Alpha can see quality through `SnapshotContext`, and `BasicRiskManagementModel` can block new entries when the active snapshot is not fresh. More nuanced degraded/stale handling is still open.
- `PortfolioConstructionEngine` exists with Python Portfolio Construction Model loading and a v0 equal-weight allocation model. `OrderSizingEngine` now owns integer quantity conversion, rounding-loss visibility, and rebalance noise filtering before risk and execution. Risk and execution now have basic deterministic models and sleeve-level Python module loading. The order lifecycle, broker gateway, order runtime store, open-ticket polling worker, execution-history reconcile worker, and paper multi-sleeve order orchestrator exist, but the long-running daemon that continuously runs framework cycles, submits, polls, imports broker fills, and reconciles all sleeves is not implemented yet.
- Framework alpha backtesting exists, but n-1 minute delayed indicator snapshot modeling is not implemented yet.
- Universe selection exists, but is not yet automatically scheduled into the live worker loop.
- `OrderTicket` / `OrderEvent` exists with paper and broker-engine submission boundaries. Duplicate submit and common fill double-apply paths are guarded, but cancel/replace is still minimal. Broker fill polling remains intentionally conservative and is reconciled through execution-history/event sync.
- Telegram notifications are outbound-only in the new engine today. Inbound Telegram commands, approval requests, and webhook processing remain in the legacy notification-engine reference and have not been copied into LEapsQuantEngine.
- KIS order-level execution-summary rows are treated as one stable summary fill id for safety. If a summary quantity changes after a partial sync, the engine avoids double-applying it and relies on reconciliation status/operator follow-up rather than inventing missing fill deltas.
- Live 200-symbol polling is bounded by external KIS/market-data-engine throughput and should not be used as a high-frequency strategy loop.
- Current live US Top 200 universe generation was tested ad hoc; the committed fixture is a small smoke universe.

## Indicator Resolution Policy

The full operator/model-author contract is documented in
`docs/runtime-cadence-resolution.md`.

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

Do not mix daily bars, minute bars, and quote snapshots into the same indicator stream. Indicator definitions now support a `resolution` field such as `daily`, `minute`, or `quote`. `IndicatorRegistry` skips bar updates whose resolution does not match the indicator plan, so a live quote cannot accidentally advance a confirmed daily SMA or momentum window. `MarketDataSnapshotEngine` stamps latest bars as `live` when the provider did not specify a resolution, and daily history loaders stamp warmed/backtest bars as `daily`.

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

## Cadence And Target Persistence

Daily or swing alpha modules can declare:

```python
EVALUATION_CADENCE = "once_per_day"
INPUT_RESOLUTION = "daily"
```

`AlphaRuntime` tracks the last run per `alpha_id`. If a same-day cycle is skipped, it publishes an empty `InsightBatch` with `metadata.ran_alpha_ids` and `metadata.skipped_alpha_ids`, while `InsightManager` keeps existing active insights alive until their normal expiry.

Portfolio construction now uses `portfolio.rebalance.cadence`. When cadence is not due, `FrameworkRunner` reuses the last allocation targets instead of rebuilding a new target set from a minute-level context. `OrderSizingEngine` still recomputes desired value, target quantity, and delta quantity from the current virtual portfolio, current price, and current cash/equity every cycle. Risk and execution then run against those freshly sized targets. Urgent exits should be modeled as always-on risk or explicitly quote-resolution exit models; daily portfolio cadence should not be the only safety path.

## Next Work

Recommended next vertical slice:

```text
Paper-to-broker-engine submit smoke
  -> record paper smoke command outputs as ignored runtime artifacts
  -> add a broker-engine dry-run/preflight report
  -> then run broker-engine 모의계좌 submit with explicit confirmation
```

After that:

1. Add richer cancel/replace lifecycle handling.
2. Persist and replay portfolio/risk/order state snapshots across process restarts.
3. Schedule `UniverseSelectionRuntime` and safely update worker target symbols.
4. Simulate n-1 minute delayed indicator snapshots in backtests.

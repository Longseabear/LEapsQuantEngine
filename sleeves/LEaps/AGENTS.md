# AGENTS.md

## Scope

`LEaps` is an active sleeve workspace. Its models should be runnable by the engine through configured module references.

## Folder Roles

- `alphas/`: prediction models that emit insights.
- `portfolios/`: portfolio construction models that emit target allocations.
- `risks/`: sleeve-level risk models that approve, reject, or clamp sized targets.
- `executions/`: sleeve-level execution models that convert approved targets into order intents.

## Rules

- Keep model code deterministic for the same snapshot/context input.
- Keep broker-specific behavior out of model code.
- Include clear model ids or class names so runtime status can explain which model acted.
- Preserve lineage fields when passing records downstream.
- Update local README or tests when a model's public behavior changes.

## Current Engine Contract For Sleeve Agents

LEaps is a mixed KRW/USD logical sleeve. Treat KRW and USD as separate cash and
equity buckets unless an explicit FX conversion layer is added later. In
backtest reports, prefer `metrics_by_currency`. The aggregate `metrics` block is
only a native-currency sum when both KRW and USD are present and should carry
`valid_without_fx=false`.

Runtime backtests now separate indicator warmup from the evaluation window. When
running `runtime-backtest-daily`, the engine can load pre-start daily bars into
`IndicatorEngine` and only starts alpha/portfolio/risk/execution cycles at
`--start`. Use `warmup_data_slice_count` in the report to confirm this happened.
Do not diagnose a one-day or short-window test as weak signal until warmup is
non-zero or an explicit `--warmup-start` was provided.

Current LEaps alpha input wiring is intentional:

```text
leaps-stock-momentum       -> leaps-kospi-conviction
leaps-etf-rotation         -> leaps-us-stability-hedge
leaps-operational-symbols  -> leaps-volatility-trailing-stop
```

`leaps-stock-momentum` is KRX-only so US single stocks cannot consume KOSPI
alpha candidate slots. ETF rotation is for US ETF hedge/stability candidates.
Operational symbols exist so held/open/manual symbols remain visible to exit or
stop logic even when they are not selected as fresh entries.

`leaps-volatility-trailing-stop` emits `InsightDirection.FLAT`. Portfolio
construction must respect active FLAT/DOWN insights for a symbol over UP
insights from another alpha at the same or later timestamp. Do not work around
exit signals by emitting broker orders from alpha code.

The active RL portfolio constructor is configured as a complete target
portfolio allocator for KRW buckets. When active KRW UP insights produce a new
target set, any currently held KRW symbol missing from that set should receive
a 0% target tagged `no_longer_in_target_portfolio`. If no actionable KRW
insights exist, do not interpret that as an implicit all-sell signal; rely on
explicit FLAT/DOWN insights or the next valid target set.

Known current strategy risk: the active RL allocator plus daily rebalance can
produce high turnover. If tuning this, adjust portfolio/risk policy such as
rebalance cadence, minimum drift, cooldown, or turnover guard. Do not add hidden
state mutations inside alpha/portfolio/execution modules.

Backtests default to zero simulated slippage. Use `--slippage-bps` when checking
execution friction. The report's `metrics.slippage_cost` and
`metrics.slippage_bps` come from fill events, not from alpha or portfolio code.

Execution models are the only sleeve models that should choose order style.
Use `order_type`, `limit_price`, `time_in_force`, and execution metadata on
`OrderIntent`; do not call broker APIs or encode KIS order codes in alpha,
portfolio, or risk modules. `executions/leaps_immediate.py` wraps the engine's
`StandardExecutionModel`, so it can be configured as limit, market, or sliced
execution through `execution.parameters` without changing portfolio logic.
New execution models can accept `execution_context` or `market_session` in
`create_orders`. Use `execution_context.session_for_symbol(symbol)` when the
policy must differ between KRX regular, KRX after-hours, US pre-market, and US
after-market.

KIS-style execution constraints are now engine-owned. Domestic KRX orders are
whole-share only, limit prices must fit the KRX tick grid, and the broker
gateway rounds domestic limit prices to the nearest side-safe tick before
submission. Confirmed live submit can require an orderable market session. Do
not bypass these checks from sleeve code. KRX after-hours submit is supported
through runtime-stamped `order_session` metadata and gateway-owned KIS order
division mapping, so sleeve execution models should keep choosing only
`order_type`, `limit_price`, and `time_in_force`. Do not opt into
`allow_after_hours_single_price` unless the symbol/venue combination has been
verified; KIS rejects some NXT-traded symbols in that phase.

Backtests can simulate KIS-like transaction costs with `--fee-model kis`.
Treat this as a configurable preset, not a promise that a specific account's
promotion rate is known. Fee metadata is recorded on fill events and included
in backtest metrics.

Open-ticket maintenance is owned by the order supervisor. Stale tickets can be
reported or cancelled by policy, including partially filled tickets. Sleeve
models should express desired holdings and execution style, not direct cancel
or replace side effects.

Before live/paper operation after engine, config, universe, or sleeve model
changes, run `runtime-preflight`. It fingerprints engine source plus the
configured universe/model files, compares the latest cycle journal identity,
checks account/order store paths, and bootstrap-loads this sleeve without
submitting orders. If it reports `config_changed_since_last_cycle`,
`engine_code_changed_since_last_cycle`, or `latest_cycle_missing_code_identity`,
stage/reload and run one journaled cycle before live order submission.

Useful verification commands:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2026-05-08 --end 2026-05-08 --source finance-datareader --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2026-05-08 --end 2026-05-08 --source finance-datareader --fee-model kis --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --currency KRW --source finance-datareader --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id "default sleeve" --start 2026-05-08 --end 2026-05-08 --source finance-datareader --summary-only
```

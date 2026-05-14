# Backtesting Guide

This guide is the operational entry point for LEapsQuantEngine backtests.

Use it when you want to answer:

- Does a sleeve strategy produce the expected selection, insights, targets, risk decisions, and orders?
- Did warmup, calendar gating, whole-share rounding, slippage, fees, or missing fundamentals change the result?
- Is a runtime config still executable after model or engine changes?

Backtests must stay deterministic and must not submit broker orders.

## Core Rule

Research backtests should run one sleeve at a time. Paper and live runtime can
orchestrate multiple sleeves together, but research should isolate sleeve
capital, model wiring, and route assumptions.

The replay path should match the live framework shape:

```text
UniverseSelection
  -> AlphaModel
  -> InsightManager
  -> PortfolioConstructionModel
  -> OrderSizingEngine
  -> RiskManagementModel
  -> ExecutionModel
  -> simulated fill
  -> PortfolioState / report
```

Do not special-case strategy code for backtests. Strategy models should see the
same normalized snapshot/context objects they see in live or paper mode.

## Command Choice

Use `runtime-backtest-daily` for normal sleeve work. It loads the runtime config,
sleeve workspace, selection models, alpha modules, portfolio model, risk model,
and execution model.

Use `runtime-backtest-minute` when validating minute-cycle behavior with a
local replay feed. It still loads the runtime config and sleeve workspace, but
minute bars must come from an explicit CSV/JSON/JSONL file.

Use `framework-backtest-daily` for a narrow alpha/module smoke. It is useful when
you want to test one alpha file against one universe without bootstrapping the
whole sleeve runtime.

Use `warmup-indicators-daily` when the question is indicator readiness, not
portfolio behavior.

Use `benchmark-indicators-daily` when the question is daily indicator throughput
or cached history health.

## Setup

From the repository root:

```powershell
$env:PYTHONPATH='src'
```

Then run commands as Python modules:

```powershell
py -3 -m leaps_quant_engine.cli <command> ...
```

## Data Sources

Prefer `--source finance-datareader` for 3-year or 5-year research backtests.
This is the default for `framework-backtest-daily` and
`runtime-backtest-daily`.

Use `--source kis-cache` for short integration smokes against already-cached KIS
history. Do not assume KIS cache has a complete multi-year history unless it was
explicitly populated.

KIS and broker payloads must stay behind adapters. Backtest models should not
call KIS, broker-engine, market-data-engine, or FinanceDataReader directly.

## Warmup

Separate warmup from the evaluation period.

Daily momentum, SMA, ATR, volatility, and rolling liquidity indicators need
confirmed daily bars before the strategy can make valid decisions. Short
evaluation windows around Korean holidays can otherwise look like "no signal"
when the real issue is cold indicators.

Example:

```text
evaluation start: 2026-05-01
evaluation end:   2026-05-08
warmup start:     2026-04-01
```

Use `--warmup-start` before `--start`:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json `
  --sleeve-id LEaps `
  --start 2026-05-01 `
  --end 2026-05-08 `
  --warmup-start 2026-04-01 `
  --cash 2000000 `
  --source finance-datareader `
  --summary-only
```

Warmup bars prepare indicators. Metrics and report cycles should still be read
from the requested `--start` to `--end` evaluation window.

## Model State Replay

Stateful models are replayable in backtests. Runtime backtest commands attach an
in-memory `RuntimeStateStore`, so models can read `context.model_state`, return
`StatePatch` records, and receive the projected state on the next replay cycle.
Summary reports include `model_state_patch_count` and
`model_state_event_count`.

## Runtime Sleeve Backtest

This is the default research command for `LEaps`:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json `
  --sleeve-id LEaps `
  --start 2023-05-10 `
  --end 2026-05-08 `
  --warmup-start 2023-04-03 `
  --cash 2000000 `
  --currency KRW `
  --source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --summary-only
```

Notes:

- `--cash` overrides the sleeve's configured starting cash for the replay.
- `--currency` should match the sleeve route being tested. Do not mix KRW and
  USD as one cash pool in v0.
- `--fee-model kis` applies simulated KIS-style costs.
- `--slippage-bps` shifts simulated fill prices against the order side.
- `--summary-only` keeps output compact.

## Debug Report

When the result is surprising, include insights and a cycle journal:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json `
  --sleeve-id LEaps `
  --start 2026-05-01 `
  --end 2026-05-08 `
  --warmup-start 2026-04-01 `
  --cash 2000000 `
  --source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --include-insights `
  --journal artifacts/backtests/leaps_20260501_20260508.jsonl
```

`--include-insights` keeps the normal report shape but adds cycle-level new and
active insight ledgers plus selection details. This is the first option to use
when insights exist but orders are zero.

`--journal` writes append-only JSONL cycle entries. Use it when an agent or
operator needs to inspect selection, alpha, portfolio, risk, execution, timings,
warnings, and errors after the run.

## One-Alpha Framework Backtest

Use this when isolating a single alpha file:

```powershell
py -3 -m leaps_quant_engine.cli framework-backtest-daily configs/universes/leaps_kr_research_core.json sleeves/LEaps/alphas/kospi_conviction.py `
  --sleeve-id LEaps `
  --start 2026-05-01 `
  --end 2026-05-08 `
  --warmup-start 2026-04-01 `
  --cash 2000000 `
  --source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --include-insights
```

This path loads one universe and one alpha module. It does not prove that the
full sleeve workspace wiring is correct; use `runtime-backtest-daily` for that.

## Runtime Minute Backtest

Use this command when the question is whether a runtime config behaves correctly
on minute replay cycles:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-minute configs/runtime/us_etf_rotation_sleeve.json `
  --sleeve-id us_etf_rotation `
  --minute-feed data/replay/us_etf_rotation_20260501_20260510_minute.csv `
  --start 2026-05-01T09:30:00 `
  --end 2026-05-10T16:00:00 `
  --warmup-start 2025-05-01 `
  --cash 3434.25 `
  --currency USD `
  --daily-source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --include-insights `
  --summary-only
```

Feed files may be CSV, JSON, or JSONL. Required row fields are symbol, time,
open, high, low, close, and volume. Symbols can be full keys such as `US:SPY`
or raw tickers when the feed uses one market.

The command uses the minute feed for evaluation cycles and `--daily-source` for
daily indicator warmup. In this command, universe indicators without an explicit
resolution are treated as confirmed `daily` indicators, so minute bars do not
accidentally advance a daily SMA, momentum, ATR, or volatility window. Explicit
`resolution: minute` indicators still update from minute bars.

If a requested US ETF rotation minute feed is missing locally, the CLI can run
but the data still has to be supplied first. Use the daily command for research
until a replay feed exists.

Create a US minute feed from a runtime config:

```powershell
py -3 -m leaps_quant_engine.cli download-us-minute-feed configs/runtime/us_etf_rotation_sleeve.json `
  --sleeve-id us_etf_rotation `
  --output data/replay/us_etf_rotation_20260501_20260510_minute.csv `
  --start 2026-05-01 `
  --end 2026-05-10 `
  --provider yfinance `
  --overwrite `
  --summary-only
```

This command reads the sleeve's coarse universe, downloads one-minute bars for
the universe symbols, chunks yfinance requests to stay under the provider's
short 1-minute range limit, and writes the standard replay columns:

```text
symbol,time,open,high,low,close,volume
```

The output timestamps are normalized to US market local time
`America/New_York` by default and written without timezone offsets so the
minute backtest CLI can compare them with normal `--start` / `--end` values.
Free providers can have retention limits or missing symbols; if the report is
`empty` or `partial`, treat it as a data availability issue.

## Fundamentals

Fundamentals are point-in-time artifacts, separate from indicators. They must
carry `as_of` dates so backtests can avoid lookahead.

Import or inspect artifacts:

```powershell
py -3 -m leaps_quant_engine.cli fundamentals-import-fdr `
  --root data/fundamentals `
  --market KRX `
  --universe configs/universes/leaps_kr_research_core.json `
  --as-of 2026-05-08 `
  --name per `
  --name market_cap `
  --include-naver-valuation `
  --summary-only

py -3 -m leaps_quant_engine.cli fundamentals-status `
  --root data/fundamentals `
  --market KRX `
  --summary-only
```

Replay with artifacts:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json `
  --sleeve-id LEaps `
  --start 2026-05-01 `
  --end 2026-05-08 `
  --warmup-start 2026-04-01 `
  --fundamentals-root data/fundamentals `
  --fundamentals-market KRX `
  --fundamental-name per `
  --fundamental-name market_cap `
  --include-insights `
  --summary-only
```

Missing fundamentals are normal for ETFs. Models should skip or degrade
gracefully when PER, PBR, or similar company fundamentals are absent.

## Multi-Market Research

In v0, do not treat KRW and USD as a single spendable cash pool. A logical sleeve
can contain domestic and overseas routes for live operations, but research
backtests are cleaner when capital is tested per market route or per sleeve.

Practical rule:

- Korean sleeve research: run KRW cash against Korean universe/config.
- US sleeve research: run USD cash against US universe/config.
- Mixed live sleeve: allowed operationally, but interpret route-level cash and
  orders separately until FX/equity aggregation is explicitly modeled.

## Debug Checklist

If insights are zero:

- Check `--warmup-start`.
- Confirm required indicators are ready.
- Confirm the symbol is in the active or forced live universe.
- Confirm market calendar gating did not reuse a stale market's bar on another
  market's open day.
- Check missing fundamentals. ETFs often have no PER/PBR.

If insights exist but orders are zero:

- Re-run with `--include-insights`.
- Check portfolio rebalance cadence and persisted targets.
- Check `portfolio_target_batch.metadata.portfolio_blend`: a transition may be
  intentionally holding the sleeve between the previous target snapshot and the
  new raw target.
- Check current holdings, cash, target deltas, and minimum rebalance filters.
- Check whole-share rounding loss, especially with small capital such as
  2,000,000 KRW.
- Check risk decisions and engine guard blocks.
- Check execution model minimum quantity/notional constraints.

When Portfolio Blend is enabled, detailed framework output and cycle journals
include `portfolio_blend.status`, `progress`, `transition_id`, `elapsed_minutes`,
`duration_minutes`, `target_drift`, and `bypassed_symbols`. Explicit flat/down,
stop, urgent, manual, operator, force, or risk tags should bypass the blend for
that symbol.

If performance changes after costs:

- Compare `--fee-model none` vs `--fee-model kis`.
- Compare `--slippage-bps 0` vs a conservative value such as `5`.
- Inspect simulated fill prices, gross trade value, fees, and net cash changes.

If a Korean holiday creates a Korean signal on a US-only data day:

- Treat it as a calendar/freshness issue until proven otherwise.
- Confirm alpha and indicator gates are scoped to the symbol's market session.
- Do not interpret repeated prior Korean values as fresh Korean conviction.

## Safety

Backtest commands simulate fills and do not submit live broker orders.

Live side effects live behind order-runtime commands and explicit commit guards.
Do not use backtest output as a live order unless it has passed the runtime
preflight, order-runtime submit dry-run, and the intended broker confirmation
path.

## Verification

After engine code changes, run:

```powershell
py -3 -m pytest -q
```

For docs-only changes, validate the changed skill or markdown where applicable
and state that pytest was not run because no Python behavior changed.

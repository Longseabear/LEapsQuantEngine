---
name: leaps-backtesting
description: Use when running, debugging, or documenting LEapsQuantEngine daily backtests, including sleeve-specific runtime replays, alpha framework smokes, indicator warmup, fundamentals artifacts, insight ledgers, cycle journals, fee/slippage simulation, and no-order diagnostics.
---

# LEaps Backtesting

## Overview

Use this skill when the task is to run or explain LEapsQuantEngine backtests.
The canonical guide is `docs/backtesting-guide.md`; use this skill as the
operator checklist before touching commands.

## First Choice

Prefer `runtime-backtest-daily` for sleeve research because it loads the runtime
config, sleeve workspace, selection models, alpha modules, portfolio model, risk
model, and execution model.

Use `runtime-backtest-minute` for minute-cycle runtime validation when a local
CSV/JSON/JSONL replay feed exists. It uses the same runtime config wiring, uses
the feed for minute evaluation cycles, and uses `--daily-source` only for daily
indicator warmup.

Use `framework-backtest-daily` only when isolating one alpha file against one
universe.

Research backtests should run one sleeve at a time. Paper and live orchestration
can run multiple sleeves together, but research should isolate sleeve capital
and model wiring.

## Required Setup

Work from the repo root:

```powershell
$env:PYTHONPATH='src'
```

Run commands as:

```powershell
py -3 -m leaps_quant_engine.cli <command> ...
```

## Default Runtime Backtest

Use FinanceDataReader for multi-year research:

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

Use `--source kis-cache` only for short cache/integration smokes where cached
history is known to exist.

## Runtime Minute Backtest

Use this when debugging daily alpha or portfolio cadence on minute cycles:

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
  --include-insights `
  --summary-only
```

Minute feed rows need symbol, time, open, high, low, close, and volume. Universe
indicators without an explicit resolution are treated as confirmed daily
indicators in this command, so minute bars do not advance daily momentum/SMA
windows by accident. Explicit minute indicators still update on minute bars.

When the local feed is missing, create it first:

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

The downloader reads the sleeve coarse universe and chunks yfinance 1-minute
requests before writing one standard replay CSV.

If the downloader returns `empty` or `partial`, report data availability rather
than a backtest CLI limitation.

## Warmup Rule

Always separate indicator warmup from evaluation. For a short test such as
`2026-05-01` to `2026-05-08`, use a warmup start such as `2026-04-01`.

Cold daily momentum, SMA, ATR, volatility, or liquidity indicators can make a
strategy look inactive even when the model would have produced signals after
warmup.

## Debug Options

When orders are surprising, re-run with:

```powershell
--include-insights --journal artifacts/backtests/<name>.jsonl
```

`--include-insights` adds selection details and cycle-level new/active insight
ledgers. `--journal` writes append-only JSONL cycle entries for agent/operator
inspection.

If insights are zero, check warmup, indicator readiness, active/forced universe,
market calendar gating, and missing fundamentals.

If insights exist but orders are zero, check portfolio cadence, persisted
targets, `portfolio_target_batch.metadata.portfolio_blend`, current holdings,
cash, whole-share rounding, risk/guard decisions, and execution constraints.

When Portfolio Blend is enabled, a raw target change can be intentionally
converted into a smaller blended target until progress reaches 100%. Explicit
flat/down, stop, urgent, manual, operator, force, or risk tags should appear in
`bypassed_symbols` and should not be delayed.

## Live-State Sandbox Probe

When testing new engine/model code against real live runtime state during market
hours, do not write to the live SQLite state DB. Fork it first:

```powershell
py -3 -m leaps_quant_engine.cli runtime-state-fork `
  --source data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --target data/runtime/runtime-state/sandbox/probe.sqlite `
  --overwrite
```

Point `runtime-run-once`, `runtime-run-multi-once`, or a diagnostic replay at
the sandbox path. Omit `--runtime-state-read-only` only on the sandbox DB when
the goal is to inspect model `StatePatch` writes.

## Fundamentals

Fundamentals are point-in-time artifacts under `data/fundamentals` and must not
be fetched directly inside models.

Common import:

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
```

Replay with `--fundamentals-root`, `--fundamentals-market`, and repeated
`--fundamental-name` flags.

Missing PER/PBR for ETFs is normal. Models should skip or degrade gracefully.

## Multi-Market Rule

Do not mix KRW and USD as one spendable cash pool in v0. Backtest Korean and US
capital per route or per sleeve, even when live operations use one logical
sleeve with multiple account routes.

## Verification

After engine code changes, run:

```powershell
py -3 -m pytest -q
```

For docs-only or skill-only changes, validate the skill and state that pytest was
not run because no Python behavior changed.

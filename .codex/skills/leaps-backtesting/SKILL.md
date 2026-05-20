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
  --daily-bar-time 09:00 `
  --cash 2000000 `
  --currency KRW `
  --source finance-datareader `
  --fee-model kis `
  --slippage-bps 5 `
  --summary-only
```

Daily bars from FinanceDataReader or cached daily history may be timestamped at
`00:00:00`. Use `--daily-bar-time HH:MM` on `runtime-backtest-daily` or
`framework-backtest-daily` when a daily run should simulate the engine cycle at
market open, for example `09:00` for KRX or `09:30` for US. This changes replay
cycle timestamps, cadence checks, fills, journals, and reports while preserving
the daily OHLCV values.

Use `--source kis-cache` only for short cache/integration smokes where cached
history is known to exist.

Runtime backtest reports include a `timings` block. Use it to separate
`config_bootstrap_ms`, daily `history_feed_build_ms` or minute `feed_load_ms`,
`daily_warmup_ms`, `framework_replay_ms`, `report_generation_ms`, and
`total_ms`. For runtime minute replay, also inspect the `replay_*_ms` fields
such as `replay_indicator_update_ms`, `replay_indicator_snapshot_ms`,
`replay_universe_selection_ms`, `replay_framework_runner_ms`,
`replay_journal_append_ms`, `replay_fill_model_ms`, and
`replay_snapshot_record_ms`; these explain wall-clock overhead outside the
narrow alpha/portfolio/risk/execution model timings.

When writing a cycle journal during repeated research runs, prefer the default
`--journal-mode auto`. It uses full lineage for detailed reports and light
cycle entries for `--summary-only`. Use `--journal-mode full` only when lineage
itself is the debugging target.

FinanceDataReader daily history is cache-first by default under
`data/runtime/cache/finance-datareader/daily`. Delete that ignored cache
directory to rebuild from scratch, or pass `--refresh-history` to force a fresh
download for the requested date ranges.

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

For recent US/overseas KIS bars, `download-us-minute-feed` also accepts
`--provider kis-cache`. The sleeve universe must carry an overseas exchange
such as `"exchange": "NAS"` unless the symbol market is already `NAS`, `NYS`,
or `AMS`. Treat this as a same-day/recent collector path; KIS overseas minute
lookup is not a deep historical minute vendor feed.

## Minute Cache

For KRX research universes, prefer a rolling minute cache over ad hoc one-off
feeds:

```powershell
py -3 -m leaps_quant_engine.cli minute-cache-build configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --cache-root data/replay/minute-cache `
  --start 2026-04-16 `
  --end 2026-05-15 `
  --provider yfinance `
  --max-symbols 200 `
  --overwrite `
  --summary-only
```

The cache writes per-day `YYYY-MM-DD.csv.gz` files under
`data/replay/minute-cache/<universe-id>/`. KRX yfinance requests use universe
metadata: KOSPI symbols map to `.KS`, KOSDAQ symbols map to `.KQ`, while replay
rows stay normalized as `KRX:<ticker>`.

`minute-cache-build --provider kis-cache` supports KRX and overseas universes.
For overseas universes, confirm the exchange map before running large pulls:
the provider routes `US:SMH` plus `"exchange": "NAS"` to the KIS overseas
intraday endpoint and writes normalized `US:SMH` rows.

Export cached bars to a standard minute feed:

```powershell
py -3 -m leaps_quant_engine.cli minute-cache-export configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --cache-root data/replay/minute-cache `
  --output data/replay/leaps_krx_20260416_20260515_minute.csv `
  --start 2026-04-16T09:00:00 `
  --end 2026-05-15T15:30:00 `
  --summary-only
```

`runtime-backtest-minute` can read the cache directly with
`--minute-cache-root`; it loads cache day files directly and preserves the
standard sorted `DataSlice` replay shape without an intermediate feed file.
Missing weekdays are reported as `missing_weekday_cache_day`; KRX holidays can
appear there until a full exchange calendar is attached.

For repeated minute research over the same feed/range, add
`--compiled-replay-cache <path>.json.gz`. The first run writes a pre-grouped
minute replay artifact; later runs can use only `--compiled-replay-cache` to
skip CSV/day-file parsing and time-bucket grouping. Use
`--refresh-compiled-replay-cache` after changing the underlying minute data.
This cache is replay data only, not model state.

Add `--daily-warmup-cache <path>.json.gz` for repeated minute tests over the
same confirmed daily warmup window. This stores daily warmup bars, not
serialized indicator objects, so the engine still replays through
`IndicatorEngine.warm_up(...)`. Use `--refresh-daily-warmup-cache` when the
warmup window or daily source changes.

## Warmup Rule

Always separate indicator warmup from evaluation. For a short test such as
`2026-05-01` to `2026-05-08`, use a warmup start such as `2026-04-01`.

Cold daily momentum, SMA, ATR, volatility, or liquidity indicators can make a
strategy look inactive even when the model would have produced signals after
warmup.

Temporal PPO has an additional requirement: the alpha-gated insight must carry a
point-in-time `rl_temporal_features` window. Runtime backtests create that
window automatically when the portfolio parameters use `feature_schema` values
such as `v2_temporal` or `v2_temporal_residual`, but `--warmup-start` still has
to reach far enough back for the daily window. Use at least 84 daily bars for
`v2_temporal` and at least 144 daily bars for `v2_temporal_residual`.

In `runtime-backtest-minute`, the temporal window still comes from
`--daily-source`; minute bars should not advance confirmed daily temporal
features.

## Opening Gap Proxy

Daily runtime/framework backtests attach a `daily_ohlc_proxy` opening context
to daily `Bar.metadata`. When a previous close exists, models can read
`previous_close`, `opening_gap_pct`, `open_to_close_return_pct`,
`open_to_low_drawdown_pct`, `open_to_high_runup_pct`, and `gap_filled`.

Treat these as long-horizon daily OHLC proxies for overnight/opening behavior,
not as proof that the model saw historical pre-open order-book data.

Alpha models read the proxy through `context.metadata(symbol)` or
`context.metadata_value(symbol, "opening_gap_pct")`.

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

## Opening / Extended Session Replay

When debugging KRX opening or after-hours behavior, build a session-tagged KIS
minute cache instead of mixing those rows into daily indicators:

```powershell
py -3 -m leaps_quant_engine.cli minute-cache-build configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --cache-root data/replay/minute-cache `
  --start 2026-05-15 `
  --end 2026-05-15 `
  --provider kis-cache `
  --include-extended-hours `
  --refresh-provider-cache `
  --summary-only
```

`--include-extended-hours` writes session metadata columns and, for date-only
KRX ranges, normalizes the day to `08:30-18:00`. Replay feeds preserve
`market_session_phase`, `is_regular_market_open`, `is_orderable_session`, and
`is_extended_market_hours` in `Bar.metadata`.

Treat this as opening/execution context. Daily-confirmed indicators should still
warm up from daily history and should not update from pre-open or after-hours
minute rows.

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

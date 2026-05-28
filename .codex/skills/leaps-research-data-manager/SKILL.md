---
name: leaps-research-data-manager
description: "Use when collecting, rebuilding, inspecting, validating, documenting, or wiring LEapsQuantEngine research data resources: news raw/evidence/context files, KIS or Google News title evidence, monthly daily-bar Parquet files, coverage universes, parquet-daily backtest sources, and minute-cache/minute-feed handoffs for backtesting."
---

# LEaps Research Data Manager

## Overview

Use this skill for research data operations, not strategy judgment. Keep data
provider payloads behind adapters or raw stores, normalize before backtests, and
never fabricate missing news or market bars.

Primary docs:

- `docs/news-data-manager-runbook.md`
- `docs/news-evidence-contract.md`
- `docs/market-data-manager-runbook.md`
- `docs/backtesting-guide.md`

## News Workflow

Use this storage shape:

```text
data/research/news_raw/<provider>/<market>/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_evidence/<market>/YYYY-MM-DD.json
sleeves/<sleeve_id>/agent_state/news_context/YYYY-MM-DD.json  # optional, on explicit request only
```

Current KRX providers:

```text
data/research/news_raw/google-news-rss/krx/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_raw/kis/domestic/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_evidence/krx/YYYY-MM-DD.json
```

Rules:

- Use real search/provider records only. Do not invent or summarize nonexistent
  articles.
- Preserve provider rows under `raw` in JSONL.
- Google News RSS rows may carry public URLs.
- KIS domestic news is title-level only. Use stable synthetic canonical URLs
  such as `kis://domestic/news-title/<date>/<id>` when KIS does not provide a
  public URL.
- Shared evidence may use `news_evidence.provider: "multi_source"` with
  `provider_sources` when Google and KIS are merged.
- Backtests and pseudo portfolios read stored evidence; they should not fetch
  fresh news during replay.
- For backtesting, treat `data/research/news_evidence/krx/YYYY-MM-DD.json` as
  the pre-open evidence for `decision_date=YYYY-MM-DD`. The preferred KRX
  window is previous day `08:00` through decision date `09:00` Asia/Seoul,
  stored as `news_window_start_at` and `decision_cutoff_at`.
- Do not pre-build sleeve-specific `news_context` files by default. When the
  user asks what a sleeve should look at, inspect the shared evidence directly
  and filter/rank it in the answer. Write sleeve `news_context` files only when
  the user explicitly asks for persisted sleeve-specific artifacts.

Before reporting news freshness, inspect both evidence and raw timestamps. For
today, answer separately for Google raw, KIS raw, Google evidence, and KIS
evidence when possible.

## Daily Bar Workflow

Monthly research daily bars live here:

```text
data/research/market_data/daily_bars/<market>_YYYY_MM.parquet
data/research/market_data/daily_bars/<market>_YYYY_MM.manifest.json
```

Coverage universes live here:

```text
data/research/market_data/coverage_universes/krx_stock_top500_by_amount.json
data/research/market_data/coverage_universes/krx_etf_top500_by_amount.json
data/research/market_data/coverage_universes/us_stock_top500_by_dollar_volume.json
data/research/market_data/coverage_universes/us_etf_top500_by_dollar_volume.json
```

Required Parquet columns:

```text
market, symbol, ticker, asset_type, name, date, time,
open, high, low, close, volume, adjusted, source, collected_at,
liquidity_rank, liquidity_score, metadata_json
```

Rules:

- Keep `symbol` in engine format such as `KRX:005930`, `KRX:069500`,
  `US:SPY`, or `US:NVDA`.
- Treat missing early-month rows for newly listed instruments as valid sparse
  history. Do not forward-fill pre-listing bars.
- Use `--source parquet-daily` for daily backtests when the date range is
  covered by local monthly Parquet files.
- Use `--daily-bar-time` to set replay cycle time; Parquet daily `time` may be
  `YYYY-MM-DDT00:00:00` as a date placeholder.

Quick inspection:

```powershell
py -3 -c "import pandas as pd; df=pd.read_parquet('data/research/market_data/daily_bars/krx_2026_05.parquet'); print(df.head(20).to_string(index=False))"
```

## Minute Data Handoff

Daily Parquet is not minute data. For minute backtests, use one of:

```text
data/replay/minute-cache/<universe-id>/YYYY-MM-DD.csv.gz
data/replay/<name>_minute.csv
```

Use `runtime-backtest-minute` with minute data for evaluation cycles and a daily
source for daily indicator warmup:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-minute configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --minute-cache-root data/replay/minute-cache `
  --daily-source parquet-daily `
  --summary-only
```

Use `--daily-source parquet-daily` for April/May 2026 deterministic smokes.
Use `finance-datareader` when warmup extends beyond the Parquet store. Use
`kis-cache` when validating recent broker-adapter cached data.

Minute bars must not advance confirmed daily SMA, momentum, ATR, liquidity, or
temporal windows.

## Validation Checklist

For news:

- Confirm files are UTF-8 JSON/JSONL.
- Confirm timestamps are at or before `decision_cutoff_at`.
- Confirm no target weights, orders, broker payloads, or strategy decisions are
  embedded in evidence/context files.
- Confirm `provenance.raw_sources` points to raw files.

For daily bars:

- Confirm each monthly file has a manifest.
- Confirm `symbol,date` uniqueness.
- Confirm asset counts by `asset_type`.
- Confirm representative symbols load through the backtest provider.

For docs-only or skill-only changes, state that pytest was not run because no
Python behavior changed.

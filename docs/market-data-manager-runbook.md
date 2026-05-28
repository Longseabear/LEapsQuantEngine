# Market Data Manager Runbook

This runbook defines the research storage contract for daily and minute market
data used by LEapsQuantEngine backtests.

## Daily Bars

Monthly research daily bars live here:

```text
data/research/market_data/daily_bars/<market>_YYYY_MM.parquet
data/research/market_data/daily_bars/<market>_YYYY_MM.manifest.json
data/research/market_data/daily_bars/build_summary_YYYY_MM_YYYY_MM.json
```

Current market prefixes:

- `krx`: Korean stocks and domestic ETFs.
- `us`: US stocks and US ETFs.

Rows must stay normalized so backtests can use `--source parquet-daily` without
special-case strategy code. Required columns:

```text
market, symbol, ticker, asset_type, name, date, time,
open, high, low, close, volume, adjusted, source, collected_at,
liquidity_rank, liquidity_score, metadata_json
```

Column meanings:

- `symbol`: engine key such as `KRX:005930`, `KRX:069500`, `US:SPY`, or
  `US:NVDA`.
- `asset_type`: `stock` or `etf`.
- `date`: exchange-local daily bar date in `YYYY-MM-DD`.
- `time`: daily timestamp placeholder, normally `YYYY-MM-DDT00:00:00`; use
  backtest `--daily-bar-time` to stamp replay cycles at market open.
- `liquidity_rank` and `liquidity_score`: point-in-build universe ranking
  fields, normally recent traded value or dollar volume.
- `metadata_json`: provider and universe metadata as JSON text.

Coverage universes live here:

```text
data/research/market_data/coverage_universes/krx_stock_top500_by_amount.json
data/research/market_data/coverage_universes/krx_etf_top500_by_amount.json
data/research/market_data/coverage_universes/us_stock_top500_by_dollar_volume.json
data/research/market_data/coverage_universes/us_etf_top500_by_dollar_volume.json
```

The monthly files contain sparse history when an instrument listed or entered
the provider history mid-month. Do not forward-fill missing pre-listing bars.
Backtests should treat absence on a date as not tradable for that date.

## Daily Backtest Usage

Use the Parquet daily provider for fixed local April/May 2026 research data:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json `
  --sleeve-id LEaps `
  --start 2026-04-01 `
  --end 2026-05-22 `
  --source parquet-daily `
  --daily-bar-time 09:00 `
  --summary-only
```

For framework smokes:

```powershell
py -3 -m leaps_quant_engine.cli framework-backtest-daily configs/universes/swing_kor_core.json examples/alpha/price_above_sma_alpha.py `
  --sleeve-id smoke `
  --start 2026-04-01 `
  --end 2026-05-22 `
  --source parquet-daily `
  --daily-bar-time 09:00 `
  --summary-only
```

Use FinanceDataReader or KIS cache for longer historical ranges that are not in
the local monthly Parquet store.

## Inspection

Preview a monthly file:

```powershell
py -3 -c "import pandas as pd; df=pd.read_parquet('data/research/market_data/daily_bars/krx_2026_05.parquet'); print(df.head(20).to_string(index=False))"
```

Export a small CSV preview:

```powershell
py -3 -c "from pathlib import Path; import pandas as pd; out=Path('data/research/market_data/daily_bars/preview.csv'); df=pd.read_parquet('data/research/market_data/daily_bars/krx_2026_05.parquet'); df.head(200).to_csv(out,index=False,encoding='utf-8-sig'); print(out.resolve(), out.stat().st_size)"
```

Check one symbol:

```powershell
py -3 -c "import pandas as pd; df=pd.read_parquet('data/research/market_data/daily_bars/krx_2026_05.parquet'); print(df[df.symbol=='KRX:005930'].to_string(index=False))"
```

## Minute Data

Daily Parquet files are not minute data. Minute replay uses explicit feeds or a
rolling cache:

```text
data/replay/minute-cache/<universe-id>/YYYY-MM-DD.csv.gz
data/replay/minute-cache/<universe-id>/YYYY-MM-DD.manifest.json
data/replay/minute-cache/<universe-id>/manifest.json
```

Prefer `minute-cache-build` plus `runtime-backtest-minute --minute-cache-root`
for repeated KRX or multi-day research. Use `download-us-minute-feed` for US
minute replay CSVs. Keep daily indicators warmed from daily history with
`--daily-source parquet-daily`, `finance-datareader`, or `kis-cache`; minute
bars must not advance confirmed daily SMA, momentum, ATR, or temporal windows.

## Quality Checks

- Each monthly file must have a manifest with row count, symbol count, asset
  type counts, start date, end date, and column list.
- `symbol` and `date` should be unique per row.
- Missing early-month rows for newly listed instruments are valid.
- Provider payloads belong in `metadata_json` or raw caches, not strategy code.
- Backtests should consume normalized providers only, never direct KIS/FDR/Yahoo
  calls from models.

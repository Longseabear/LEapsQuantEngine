# Fundamentals

Fundamentals are point-in-time company facts such as PER, PBR, EPS, ROE, market cap, and dividend yield.

They are intentionally separate from indicators. Indicators consume normalized `Bar` data and maintain rolling price or volume state. Fundamentals must carry an `as_of` timestamp that represents when the engine was allowed to know the value, so backtests can avoid lookahead.

Alpha and universe models should read fundamentals through `SnapshotContext`, not by querying a provider directly.

## FinanceDataReader Adapter

`FinanceDataReaderFundamentalProvider` loads current FDR listing fields into `PointInTimeFundamentalStore` with an explicit `as_of`.

Supported normalized names include:

- `market_cap`
- `listed_shares`
- `turnover_krw`
- `last_price`
- `volume`
- `per`, `pbr`, `eps`, `bps`, `dps`, `dividend_yield`, `roe`, `roa`

FDR's plain `StockListing("KRX")` usually provides market cap and listing snapshot fields. StockProgram's PER/PBR-style values came from a valuation enrichment layer over Naver market-sum pages, so the adapter exposes optional Naver enrichment while keeping that provider detail outside alpha code.

For backtests, import archived snapshots with their real historical `as_of`. Do not stamp today's FDR/Naver snapshot onto past dates.

## Artifact Store

`FileFundamentalArtifactStore` writes date-stamped JSON snapshots:

```text
data/fundamentals/{market}/{YYYY-MM-DD}.json
```

Artifacts include schema version, market, `as_of`, source, symbol count, value count, names, and per-symbol `FundamentalValue` payloads. They can be converted back into `PointInTimeFundamentalStore` for deterministic replay.

CLI:

```powershell
py -3 -m leaps_quant_engine.cli fundamentals-import-fdr --market KRX --as-of 2026-05-08 --summary-only
py -3 -m leaps_quant_engine.cli fundamentals-import-fdr --universe configs/universes/swing_kor_core.json --as-of 2026-05-08 --name per --name market_cap --summary-only
py -3 -m leaps_quant_engine.cli fundamentals-status --market KRX --summary-only
py -3 -m leaps_quant_engine.cli framework-backtest-daily configs/universes/swing_kor_core.json examples/alpha/value_alpha.py --sleeve-id LEaps --fundamentals-root data/fundamentals --fundamental-name per --summary-only
```

Backtests load matching artifacts into `PointInTimeFundamentalStore`; the store still enforces `value.as_of <= cycle time` on every cycle.

# Adapter Layer

Adapters isolate external providers from the deterministic engine core.

## Main Files

- `kis.py`: compatibility provider surface for KIS-backed live and cached data.
- `kis_direct.py`: in-process KIS REST boundary for quotes, cache-first history, news-title snapshots, account sync, and order submission.
- `finance_datareader.py`: FinanceDataReader historical daily provider and FDR/Naver-backed fundamental snapshot importer for long-horizon research/backtests.

## Rules

- Provider-specific payloads must be normalized before reaching universe, alpha, portfolio, risk, or execution models.
- KIS access should go through the engine-owned adapter boundary, not direct calls from strategy code. The legacy broker-engine and market-data-engine are reference/compatibility concepts, not required runtime servers.
- Backtests should prefer deterministic providers such as cached data, CSV, FinanceDataReader history, or archived fundamental snapshots.

`FinanceDataReaderFundamentalProvider` is a snapshot importer. It can load `StockListing("KRX")` values such as market cap, listed shares, turnover, and latest price, and can optionally enrich KRX valuation fields from the same Naver market-sum pattern used by StockProgram. The caller must provide the correct `as_of` date; adapters must not pretend a current valuation snapshot is historical data.

Adapters may deal with authentication, rate limits, provider quirks, and cache policy. Engine stages should only see normalized `Bar` / `DataSlice` data.

KIS news title operations are exposed as read-only adapter calls:

- `get_domestic_news_titles`
- `get_overseas_news_titles`
- `get_overseas_breaking_news_titles`

These return normalized title-level records with raw KIS rows preserved under
`raw_output`. They are context inputs for agents or future research pipelines,
not trading decisions by themselves.

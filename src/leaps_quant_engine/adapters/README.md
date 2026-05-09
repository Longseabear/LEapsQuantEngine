# Adapter Layer

Adapters isolate external providers from the deterministic engine core.

## Main Files

- `kis.py`: broker-engine / market-data-engine adapter surface for KIS-backed live and cached data.
- `finance_datareader.py`: FinanceDataReader historical daily provider for long-horizon research/backtests.

## Rules

- Provider-specific payloads must be normalized before reaching universe, alpha, portfolio, risk, or execution models.
- KIS access should go through broker-engine / market-data-engine boundaries, not direct calls from strategy code.
- Backtests should prefer deterministic providers such as cached data, CSV, or FinanceDataReader history.

Adapters may deal with authentication, rate limits, provider quirks, and cache policy. Engine stages should only see normalized `Bar` / `DataSlice` data.


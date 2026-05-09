# AGENTS.md

## Scope

Adapters translate external or local provider data into normalized engine interfaces.

## Responsibilities

- Normalize provider payloads into `Bar`, `DataSlice`, market snapshots, or provider-neutral records.
- Keep KIS access behind broker-engine, cache, or an explicit adapter boundary.
- Prefer cache-first historical workflows for KIS-derived data.
- Keep FinanceDataReader-style providers suitable for deterministic research and backtesting.
- Preserve provider metadata that affects freshness, exchange, market scope, adjusted prices, or rate limits.

## Do Not

- Do not place strategy, alpha, portfolio, risk, or execution decisions in adapters.
- Do not leak raw provider payloads into deterministic core models unless the field is explicitly marked as adapter metadata.
- Do not bypass broker-engine for live KIS operations.

## Tests

Mock external dependencies. Adapter tests should prove normalization, sorting, freshness, and failure handling without requiring live credentials.

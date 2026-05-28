# kr-core-compass

Operator-facing name for the `kr-domestic-4401` system sleeve.

This workspace keeps `kr-domestic-4401` as the stable system ID because account
routing, runtime artifacts, and order lineage depend on it. `kr-core-compass`
is the strategy alias used in operator-facing docs and discussion.

This workspace is intentionally core-oriented:

- it has a fixed sleeve workspace path
- it has a live-capable broker route in its own runtime config
- it is enrolled in the main live multi-sleeve runner
- its active alpha is a KRX ETF / liquid large-cap core regime allocator
- it rotates toward cash-like ETFs and a small inverse hedge in shock regimes

The strategy should remain top-down and low-turnover: first decide how much
KRX equity beta the 4401 account should carry, then allocate to broad-market
ETFs and liquid large-cap stocks only when the regime allows risk.

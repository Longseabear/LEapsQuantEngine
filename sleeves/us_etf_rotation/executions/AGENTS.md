# AGENTS.md

## Scope

Execution models for `us_etf_rotation`.

## Rules

- Convert approved targets into `OrderIntent` records only.
- Do not submit orders or call broker APIs.
- Preserve sleeve lineage and use clear ETF rotation tags.

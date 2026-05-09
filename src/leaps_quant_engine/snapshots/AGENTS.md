# AGENTS.md

## Scope

Snapshots are immutable read models passed from indicator/data runtime into alpha, portfolio, risk, and agent status.

## Responsibilities

- Represent point-in-time indicator and market context.
- Carry freshness, quality, source, and snapshot identity metadata.
- Keep snapshots deterministic and safe to copy for alpha dry-runs.
- Make stale or missing data explicit instead of silently defaulting to fresh values.

## Do Not

- Do not mutate live indicator objects from snapshot code.
- Do not call providers or brokers from snapshot code.
- Do not place strategy decisions in snapshot freshness policy.

## Tests

Tests should cover freshness classification, stale data behavior, deterministic serialization, and missing-symbol handling.

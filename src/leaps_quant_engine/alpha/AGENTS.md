# AGENTS.md

## Scope

This folder owns alpha domain objects, alpha model loading, runtime staging, active insight management, and optional insight persistence.

## Interface Contract

Alpha models consume immutable snapshot context and emit `Insight` records.

```text
SnapshotContext -> AlphaModel.generate(context) -> InsightBatch
```

Every insight should carry sleeve identity, symbol, direction, confidence, alpha id/version, source snapshot id, expiry or horizon, and a debug-friendly reason.

## Rules

- Alpha models do not create portfolio targets, quantities, orders, tickets, or fills.
- Alpha models do not read mutable `IndicatorEngine` state directly.
- Alpha models do not call KIS, broker-engine, market-data-engine, or broker adapters.
- Runtime swaps happen through staging and activation at snapshot or cycle boundaries, never mid-cycle.
- Flat or exit-like alpha is allowed as a prediction/control signal, but order intent still belongs downstream.

## Tests

Tests should cover insight validation, expiry, active/inactive management, loader errors, dry-run staging, and deterministic behavior for repeated contexts.

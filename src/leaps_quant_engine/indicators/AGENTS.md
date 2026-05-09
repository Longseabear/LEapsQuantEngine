# AGENTS.md

## Scope

Indicators are shared incremental computation primitives for universe, alpha, and risk.

## Interface Contract

Indicators should follow a LEAN-like surface:

```text
indicator.update(bar)
indicator.is_ready
indicator.current
indicator.warmup_period
```

## Rules

- Consume normalized `Bar`, `DataSlice`, or provider-neutral snapshot values only.
- Keep runtime state sleeve-namespaced: sleeve -> symbol -> indicator name -> indicator.
- Do not fetch raw external data from inside an indicator.
- Do not persist rolling indicator state on every update.
- Keep confirmed daily indicators separate from live/provisional quote indicators by name and update path.
- Never let a live quote accidentally advance a confirmed daily SMA, EMA, momentum, ATR, volatility, or rolling-volume window.

## Tests

Tests should cover warmup, readiness, reset behavior, update ordering, sleeve isolation, and daily-vs-live resolution boundaries.

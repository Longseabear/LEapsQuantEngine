# Indicator Engine

Indicators are shared computation primitives used by universe, alpha, and risk.

The intended surface is LEAN-like:

```text
indicator.update(bar)
indicator.is_ready
indicator.current
indicator.warmup_period
```

## Main Files

- `core.py`: base indicator concepts and rolling window.
- `price.py`: price indicators such as SMA and momentum.
- `volume.py`: volume/liquidity indicators.
- `factory.py`: indicator definition to object construction.
- `registry.py`: per-sleeve symbol/indicator registry.
- `engine.py`: sleeve-namespaced indicator update and snapshot creation.

## State Model

Indicator infrastructure can be shared, but indicator state is sleeve-namespaced:

```text
sleeve_id -> symbol_key -> indicator_name -> indicator
```

The same symbol may have different periods, readiness, and current values in different sleeves.

## Data Rules

Indicators consume normalized `Bar` / `DataSlice` inputs only.

Do not let live quote snapshots accidentally advance confirmed daily indicators.
Daily/history indicators and live quote indicators must be explicitly separated
by name/config.

`IndicatorRegistry` enforces the configured indicator resolution:

- confirmed daily indicators update only from `daily` or `daily_confirmed` bars
- live/quote indicators update only from live/intraday bars
- `any` is only for intentionally generic indicators

Unknown bar resolutions are not accepted for confirmed daily indicators. Provider
adapters should stamp historical rows as `daily`, minute replay rows as
`minute`, and live/latest quote rows as `live`.

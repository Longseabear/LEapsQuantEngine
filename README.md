# LEapsQuantEngine

LEAN-style dynamic quant engine v0.

The new engine is being built from a clean root. The previous StockProgram stack is preserved under `reference/stockprogram_legacy` and should be treated as reference material only.

See [docs/agent-artifact-runtime.md](C:/Users/leap1/Documents/LEapsQuantEngine/docs/agent-artifact-runtime.md) for the long-running engine, external agent artifacts, validation, safe reload, and isolated backtesting architecture.

## Shape

- `Algorithm`: user strategy logic, similar to LEAN's algorithm surface.
- `Engine`: event loop that feeds data slices into algorithms.
- `Sleeve`: budgeted strategy compartment with its own policy and risk boundary.
- `Portfolio`: shared state for cash, holdings, and sleeve allocations.
- `Execution`: converts sleeve-approved targets into order intents.
- `Runtime`: wires config, algorithms, data, and execution together.

## Smoke Test

```powershell
py -3 -m pytest -q
```

## Run Sample

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli run-once sample_swing_kor_pipeline.json
```

Or install the package in editable mode first:

```powershell
py -3 -m pip install -e .
leapsq run-once sample_swing_kor_pipeline.json
```

## KIS Adapter

KIS is treated as an external market data provider, not as part of the deterministic engine core.

```powershell
Copy-Item .env.example .env
# fill KIS_APP_KEY and KIS_APP_SECRET
$env:PYTHONPATH='src'
py -3 -c "from leaps_quant_engine.adapters.kis import KISBrokerEngineMarketDataProvider; p=KISBrokerEngineMarketDataProvider.from_env(); print(p.health_check())"
```

CLI shortcuts:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli kis-health
py -3 -m leaps_quant_engine.cli kis-quote 005930 --market KRX
```

## Backtesting

Backtests use the same engine surface with a virtual market data provider:

```python
from datetime import datetime
from leaps_quant_engine import Symbol, Bar, VirtualMarketDataProvider

symbol = Symbol("005930", "KRX")
provider = VirtualMarketDataProvider.from_bars([
    Bar(symbol, datetime(2026, 5, 4), 100, 100, 100, 100),
    Bar(symbol, datetime(2026, 5, 7), 110, 110, 110, 110),
])
```

## Indicators

Indicators follow a LEAN-like incremental interface:

```python
from leaps_quant_engine import SimpleMovingAverage

sma = SimpleMovingAverage(20)
point = sma.update(bar)
if sma.is_ready:
    print(sma.current.value)
```

The v0 catalog supports 30+ lightweight indicators, including SMA, EMA, momentum, ROC, rolling min/max/range, variance, standard deviation, z-score, ATR, gap percent, drawdown, VWAP, OBV, PVT, accumulation/distribution, and rolling dollar volume.

Universe files can register symbol-level indicator plans:

```python
from leaps_quant_engine import IndicatorEngine
from leaps_quant_engine.universe import load_universe_definition

universe = load_universe_definition("configs/universes/swing_kor_core.json")
engine = IndicatorEngine()
engine.register_universe("swing-kor", universe)
```

Indicator runtime checks:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli indicators-kis-once sample_swing_kor_pipeline.json --sleeve-id swing-kor
py -3 -m leaps_quant_engine.cli indicators-kis-once sample_swing_kor_pipeline.json --sleeve-id swing-kor --warmup-start 2026-05-01 --warmup-end 2026-05-07
py -3 -m leaps_quant_engine.cli indicators-backtest-once sample_swing_kor_pipeline.json --sleeve-id swing-kor
```

`indicators-kis-once` pulls latest bars through broker-engine/KIS. `indicators-backtest-once` verifies the configured sleeve universe can be loaded without touching KIS; deterministic backtest updates are covered through `VirtualMarketDataProvider`.

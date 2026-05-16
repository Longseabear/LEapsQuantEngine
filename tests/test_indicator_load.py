from datetime import datetime, timedelta

from leaps_quant_engine.indicators import IndicatorEngine, supported_indicator_types
from leaps_quant_engine.models import Bar
from leaps_quant_engine.universe.loader import parse_universe_definition


LOAD_INDICATOR_TYPES = [
    "identity",
    "sma",
    "ema",
    "momentum",
    "roc",
    "rolling_min",
    "rolling_max",
    "rolling_range",
    "variance",
    "stddev",
    "zscore",
    "typical_price",
    "median_price",
    "weighted_close",
    "true_range",
    "atr",
    "gap_percent",
    "bar_return",
    "high_low_range_percent",
    "close_location_value",
    "drawdown",
    "volume",
    "rolling_volume",
    "rolling_dollar_volume",
    "volume_momentum",
    "volume_ratio",
    "vwap",
    "obv",
    "pvt",
    "accumulation_distribution",
    "money_flow_volume",
]


def test_supported_indicator_catalog_has_more_than_30_types():
    assert len(supported_indicator_types()) >= 30


def test_indicator_engine_updates_30_plus_indicators_across_many_symbols():
    symbols = [f"{idx:06d}" for idx in range(1, 51)]
    universe = parse_universe_definition(
        {
            "id": "load-test",
            "market": "KRX",
            "symbols": symbols,
            "indicators": [
                {
                    "name": f"{indicator_type}_load",
                    "type": indicator_type,
                    "period": 5,
                    "field": "close",
                }
                for indicator_type in LOAD_INDICATOR_TYPES
            ],
        }
    )
    engine = IndicatorEngine()
    engine.register_universe("load-sleeve", universe)

    bars = []
    start = datetime(2026, 5, 1)
    for day in range(8):
        for symbol in universe.symbols:
            base = int(symbol.ticker)
            close = 100.0 + day + (base % 17)
            bars.append(
                Bar(
                    symbol=symbol,
                    time=start + timedelta(days=day),
                    open=close - 0.5,
                    high=close + 1.0,
                    low=close - 1.0,
                    close=close,
                    volume=1000 + base + day,
                    resolution="daily",
                )
            )

    engine.warm_up("load-sleeve", bars)

    values = engine.values_for("load-sleeve", universe.symbols, ready_only=False)
    assert len(values) == 50
    assert all(len(symbol_values) == len(LOAD_INDICATOR_TYPES) for symbol_values in values.values())
    ready_counts = [
        sum(value is not None for value in symbol_values.values())
        for symbol_values in values.values()
    ]
    assert min(ready_counts) >= 25

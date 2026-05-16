from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.indicators import (
    IndicatorRegistry,
    Momentum,
    RollingDollarVolume,
    RollingWindow,
    SimpleMovingAverage,
)
from leaps_quant_engine.models import Bar, Symbol


def _bar(symbol: Symbol, day: int, close: float, volume: int = 10, resolution: str = "any") -> Bar:
    time = datetime(2026, 5, 1) + timedelta(days=day)
    return Bar(symbol, time, close, close, close, close, volume, resolution=resolution)


def test_rolling_window_keeps_fixed_size_values():
    window = RollingWindow[int](3)

    for value in [1, 2, 3, 4]:
        window.add(value)

    assert window.is_ready
    assert window.values == (2, 3, 4)


def test_simple_moving_average_warms_up_like_lean_indicator():
    symbol = Symbol("005930", "KRX")
    indicator = SimpleMovingAverage(3)

    assert indicator.update(_bar(symbol, 0, 10)) is None
    assert not indicator.is_ready
    assert indicator.update(_bar(symbol, 1, 20)) is None

    point = indicator.update(_bar(symbol, 2, 30))

    assert indicator.is_ready
    assert point is not None
    assert point.value == pytest.approx(20)
    assert indicator.current == point


def test_momentum_returns_period_return_after_warmup():
    symbol = Symbol("005930", "KRX")
    indicator = Momentum(2)

    indicator.update(_bar(symbol, 0, 100))
    indicator.update(_bar(symbol, 1, 110))
    point = indicator.update(_bar(symbol, 2, 121))

    assert indicator.is_ready
    assert point is not None
    assert point.value == pytest.approx(0.21)


def test_rolling_dollar_volume_averages_price_times_volume():
    symbol = Symbol("005930", "KRX")
    indicator = RollingDollarVolume(2)

    indicator.update(_bar(symbol, 0, 100, volume=10))
    point = indicator.update(_bar(symbol, 1, 110, volume=20))

    assert point is not None
    assert point.value == pytest.approx((1000 + 2200) / 2)


def test_indicator_registry_updates_symbol_indicators_chronologically():
    symbol = Symbol("005930", "KRX")
    registry = IndicatorRegistry()
    registry.add(symbol, SimpleMovingAverage(2))
    registry.add(symbol, Momentum(1))

    registry.update_many(
        [
            _bar(symbol, 1, 20),
            _bar(symbol, 0, 10),
        ]
    )

    assert registry.ready_values(symbol) == {
        "sma_2_close": pytest.approx(15),
        "momentum_1_close": pytest.approx(1.0),
    }


def test_indicator_registry_does_not_advance_daily_indicator_from_live_bar():
    symbol = Symbol("005930", "KRX")
    registry = IndicatorRegistry()
    registry.add(symbol, SimpleMovingAverage(2), resolution="daily")

    registry.update(_bar(symbol, 0, 10, resolution="daily"))
    registry.update(_bar(symbol, 1, 20, resolution="daily"))
    ready = registry.ready_values(symbol)

    registry.update(_bar(symbol, 2, 1_000, resolution="live"))

    assert ready["sma_2_close"] == pytest.approx(15)
    assert registry.ready_values(symbol)["sma_2_close"] == pytest.approx(15)


@pytest.mark.parametrize("resolution", ["quote", "live", "minute", "any", "unknown"])
def test_indicator_registry_blocks_non_daily_resolution_for_confirmed_daily_indicator(resolution: str):
    symbol = Symbol("005930", "KRX")
    registry = IndicatorRegistry()
    registry.add(symbol, SimpleMovingAverage(2), resolution="daily")

    registry.update(_bar(symbol, 0, 10, resolution="daily"))
    registry.update(_bar(symbol, 1, 20, resolution="daily"))
    report = registry.update(_bar(symbol, 2, 1_000, resolution=resolution))

    assert report.updated_count == 0
    assert report.resolution_mismatch_count == 1
    assert registry.ready_values(symbol)["sma_2_close"] == pytest.approx(15)

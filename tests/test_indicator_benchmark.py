from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.benchmark import run_daily_indicator_benchmark
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.universe.loader import load_universe_definition, parse_universe_definition


class FakeCachedHistoryProvider:
    def __init__(self, history_by_key):
        self.history_by_key = history_by_key
        self.calls = []

    def get_cached_daily_history(self, symbol, *, start=None, end=None, refresh=False):
        self.calls.append((symbol.key, start, end, refresh))
        return list(self.history_by_key[symbol.key])


class SequenceClock:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        if not self.values:
            raise AssertionError("clock exhausted")
        return self.values.pop(0)


def _bar(symbol: Symbol, day: int, close: float | None = None) -> Bar:
    value = close if close is not None else 100.0 + day
    return Bar(
        symbol=symbol,
        time=datetime(2026, 1, 1) + timedelta(days=day),
        open=value - 1,
        high=value + 1,
        low=value - 2,
        close=value,
        volume=1000 + day,
        resolution="daily",
    )


def _benchmark_universe(symbol_count: int = 200):
    return parse_universe_definition(
        {
            "id": "benchmark-test",
            "market": "KRX",
            "symbols": [f"{index:06d}" for index in range(1, symbol_count + 1)],
            "indicators": [
                {"name": "identity_close", "type": "identity", "period": 1, "field": "close"},
                {"name": "sma_2_close", "type": "sma", "period": 2, "field": "close"},
            ],
        }
    )


def test_benchmark_fixture_has_200_symbols_and_30_plus_indicators():
    universe = load_universe_definition("configs/universes/benchmark_kor_200.json")

    assert len(universe.symbols) == 200
    assert len(universe.indicators) >= 30


def test_indicator_engine_registers_200_symbol_universe():
    universe = _benchmark_universe()
    engine = IndicatorEngine()

    engine.register_universe("benchmark-kor", universe)

    assert len(engine.active_symbols()) == 200
    assert len(engine.symbols_for_sleeve("benchmark-kor")) == 200


def test_indicator_engine_updates_200_symbols_from_one_daily_slice():
    universe = _benchmark_universe()
    engine = IndicatorEngine()
    engine.register_universe("benchmark-kor", universe)
    bars = {
        symbol.key: _bar(symbol, 0, close=float(index))
        for index, symbol in enumerate(universe.symbols, start=1)
    }

    engine.on_data(DataSlice(time=datetime(2026, 1, 1), bars=bars))

    ready_symbols = [
        symbol
        for symbol in universe.symbols
        if engine.ready_values("benchmark-kor", symbol).get("identity_close") is not None
    ]
    assert len(ready_symbols) == 200


def test_daily_indicator_benchmark_measures_history_replay_and_update_separately():
    universe = _benchmark_universe()
    history_by_key = {}
    for index, symbol in enumerate(universe.symbols):
        history_by_key[symbol.key] = [
            _bar(symbol, 0, close=100.0 + index),
            _bar(symbol, 1, close=101.0 + index),
            _bar(symbol, 2, close=102.0 + index),
        ]
    provider = FakeCachedHistoryProvider(history_by_key)
    start = datetime(2026, 1, 1)
    end = datetime(2026, 1, 3)
    clock = SequenceClock([
        0.000,
        0.010,
        0.010,
        0.015,
        0.015,
        0.016,
        0.016,
        0.018,
        0.018,
        0.021,
    ])

    report = run_daily_indicator_benchmark(
        universe,
        provider,
        sleeve_id="benchmark-kor",
        start=start,
        end=end,
        refresh_history=True,
        include_daily=True,
        clock=clock,
    )

    assert report["universe_size"] == 200
    assert report["updated_symbol_count"] == 200
    assert report["indicator_count_per_symbol"] == 2
    assert report["sessions"] == 3
    assert report["bars_seen"] == 600
    assert report["indicator_updates_estimated"] == 1200
    assert report["history_load_ms"] == pytest.approx(10.0)
    assert report["replay_build_ms"] == pytest.approx(5.0)
    assert report["total_update_ms"] == pytest.approx(6.0)
    assert report["avg_ms"] == pytest.approx(2.0)
    assert report["p50_ms"] == pytest.approx(2.0)
    assert report["p95_ms"] == pytest.approx(3.0)
    assert report["max_ms"] == pytest.approx(3.0)
    assert report["ready_symbol_count"] == 200
    assert [item["bar_count"] for item in report["daily"]] == [200, 200, 200]
    assert [item["ready_symbol_count"] for item in report["daily"]] == [200, 200, 200]
    assert len(provider.calls) == 200
    assert all(call[3] is True for call in provider.calls)

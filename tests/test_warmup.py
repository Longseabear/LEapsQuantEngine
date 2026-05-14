from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.warmup import WarmupPolicy, run_daily_indicator_warmup


class FakeCachedHistoryProvider:
    def __init__(self, history_by_key, failures=None):
        self.history_by_key = history_by_key
        self.failures = failures or {}
        self.calls = []

    def get_cached_daily_history(self, symbol, *, start=None, end=None, refresh=False):
        self.calls.append((symbol.key, start, end, refresh))
        if symbol.key in self.failures:
            raise RuntimeError(self.failures[symbol.key])
        return list(self.history_by_key.get(symbol.key, []))


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
    )


def _warmup_universe():
    return parse_universe_definition(
        {
            "id": "warmup-test",
            "market": "KRX",
            "symbols": ["005930", "000660"],
            "indicators": [
                {"name": "close", "type": "close", "period": 1},
                {"name": "sma_3_close", "type": "sma", "period": 3, "field": "close"},
                {"name": "momentum_2_close", "type": "momentum", "period": 2, "field": "close"},
            ],
        }
    )


def test_warmup_policy_uses_indicator_warmup_periods():
    universe = _warmup_universe()

    assert WarmupPolicy().required_bars(universe) == 3
    assert WarmupPolicy(extra_bars=2).required_bars(universe) == 5


def test_warmup_policy_excludes_optional_indicator_warmup_periods():
    universe = parse_universe_definition(
        {
            "id": "warmup-test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_3_close", "type": "sma", "period": 3, "field": "close"},
                {
                    "name": "roc_60_close",
                    "type": "roc",
                    "period": 60,
                    "field": "close",
                    "readiness": "optional",
                },
            ],
        }
    )

    assert WarmupPolicy().required_bars(universe) == 3


def test_daily_indicator_warmup_loads_history_and_marks_symbols_ready():
    universe = _warmup_universe()
    history_by_key = {
        symbol.key: [_bar(symbol, 0), _bar(symbol, 1), _bar(symbol, 2)]
        for symbol in universe.symbols
    }
    provider = FakeCachedHistoryProvider(history_by_key)
    clock = SequenceClock([0.000, 0.000, 0.015, 0.015, 0.018, 0.020])

    result = run_daily_indicator_warmup(
        universe,
        provider,
        sleeve_id="warmup",
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 3),
        refresh_history=True,
        clock=clock,
    )

    report = result.report
    assert report.requested_symbol_count == 2
    assert report.loaded_symbol_count == 2
    assert report.failed_symbol_count == 0
    assert report.indicator_count_per_symbol == 3
    assert report.required_indicator_count_per_symbol == 3
    assert report.optional_indicator_count_per_symbol == 0
    assert report.required_warmup_bars == 3
    assert report.ready_symbol_count == 2
    assert report.ready_ratio == 1.0
    assert report.is_ready is True
    assert report.history_load_ms == pytest.approx(15.0)
    assert report.warmup_update_ms == pytest.approx(3.0)
    assert report.total_elapsed_ms == pytest.approx(20.0)
    assert all(symbol.is_ready for symbol in report.symbols)
    assert all(call[3] is True for call in provider.calls)
    assert result.indicator_engine.value("warmup", universe.symbols[0], "sma_3_close") == pytest.approx(101.0)


def test_daily_indicator_warmup_optional_indicator_gap_does_not_block_readiness():
    universe = parse_universe_definition(
        {
            "id": "warmup-test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_3_close", "type": "sma", "period": 3, "field": "close"},
                {
                    "name": "roc_60_close",
                    "type": "roc",
                    "period": 60,
                    "field": "close",
                    "readiness": "optional",
                },
            ],
        }
    )
    symbol = universe.symbols[0]
    provider = FakeCachedHistoryProvider({symbol.key: [_bar(symbol, 0), _bar(symbol, 1), _bar(symbol, 2)]})
    clock = SequenceClock([0.000, 0.000, 0.001, 0.001, 0.002, 0.003])

    result = run_daily_indicator_warmup(universe, provider, sleeve_id="warmup", clock=clock)

    report = result.report
    assert report.required_warmup_bars == 3
    assert report.required_indicator_count_per_symbol == 1
    assert report.optional_indicator_count_per_symbol == 1
    assert report.is_ready is True
    assert report.symbols[0].is_ready is True
    assert report.symbols[0].required_ready_indicator_count == 1
    assert report.symbols[0].optional_ready_indicator_count == 0
    assert report.symbols[0].missing_required_indicators == ()
    assert report.symbols[0].missing_optional_indicators == ("roc_60_close",)


def test_daily_indicator_warmup_optional_indicator_updates_when_history_is_available():
    universe = parse_universe_definition(
        {
            "id": "warmup-test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_3_close", "type": "sma", "period": 3, "field": "close"},
                {
                    "name": "roc_60_close",
                    "type": "roc",
                    "period": 60,
                    "field": "close",
                    "readiness": "optional",
                },
            ],
        }
    )
    symbol = universe.symbols[0]
    provider = FakeCachedHistoryProvider({symbol.key: [_bar(symbol, day) for day in range(61)]})
    clock = SequenceClock([0.000, 0.000, 0.001, 0.001, 0.002, 0.003])

    result = run_daily_indicator_warmup(
        universe,
        provider,
        sleeve_id="warmup",
        start=datetime(2026, 1, 1),
        end=datetime(2026, 3, 2),
        clock=clock,
    )

    assert result.report.is_ready is True
    assert result.report.symbols[0].optional_ready_indicator_count == 1
    assert result.report.symbols[0].missing_optional_indicators == ()
    assert result.indicator_engine.value("warmup", symbol, "roc_60_close") is not None


def test_daily_indicator_warmup_required_long_indicator_still_blocks_readiness():
    universe = parse_universe_definition(
        {
            "id": "warmup-test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_3_close", "type": "sma", "period": 3, "field": "close"},
                {"name": "roc_60_close", "type": "roc", "period": 60, "field": "close"},
            ],
        }
    )
    symbol = universe.symbols[0]
    provider = FakeCachedHistoryProvider({symbol.key: [_bar(symbol, 0), _bar(symbol, 1), _bar(symbol, 2)]})
    clock = SequenceClock([0.000, 0.000, 0.001, 0.001, 0.002, 0.003])

    result = run_daily_indicator_warmup(universe, provider, sleeve_id="warmup", clock=clock)

    assert result.report.required_warmup_bars == 61
    assert result.report.is_ready is False
    assert result.report.symbols[0].is_ready is False
    assert result.report.symbols[0].missing_required_indicators == ("roc_60_close",)


def test_daily_indicator_warmup_reports_insufficient_history():
    universe = _warmup_universe()
    history_by_key = {
        universe.symbols[0].key: [_bar(universe.symbols[0], 0)],
        universe.symbols[1].key: [_bar(universe.symbols[1], 0), _bar(universe.symbols[1], 1), _bar(universe.symbols[1], 2)],
    }
    provider = FakeCachedHistoryProvider(history_by_key)
    clock = SequenceClock([0.000, 0.000, 0.001, 0.001, 0.002, 0.003])

    result = run_daily_indicator_warmup(universe, provider, sleeve_id="warmup", clock=clock)

    assert result.report.ready_symbol_count == 1
    assert result.report.ready_ratio == 0.5
    assert result.report.is_ready is False
    assert result.report.symbols[0].loaded_bar_count == 1
    assert result.report.symbols[0].ready_indicator_count == 1
    assert result.report.symbols[0].is_ready is False


def test_daily_indicator_warmup_reports_provider_failures_without_aborting():
    universe = _warmup_universe()
    history_by_key = {
        universe.symbols[0].key: [_bar(universe.symbols[0], 0), _bar(universe.symbols[0], 1), _bar(universe.symbols[0], 2)]
    }
    provider = FakeCachedHistoryProvider(history_by_key, failures={universe.symbols[1].key: "history unavailable"})
    clock = SequenceClock([0.000, 0.000, 0.001, 0.001, 0.002, 0.003])

    result = run_daily_indicator_warmup(universe, provider, sleeve_id="warmup", clock=clock)

    assert result.report.loaded_symbol_count == 1
    assert result.report.failed_symbol_count == 1
    assert result.report.ready_symbol_count == 1
    assert result.report.symbols[1].failed is True
    assert result.report.symbols[1].message == "history unavailable"

from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.fine import FineUniverseCache, FineUniverseRuntime
from leaps_quant_engine.universe.loader import parse_universe_definition


class FakeProvider:
    def __init__(self, bars_by_key, failures=None):
        self.bars_by_key = bars_by_key
        self.failures = failures or {}
        self.calls = []

    def get_latest_bar(self, symbol):
        self.calls.append(symbol.key)
        if symbol.key in self.failures:
            raise RuntimeError(self.failures[symbol.key])
        return self.bars_by_key[symbol.key]


def _bar(symbol: Symbol, close: float, minute: int = 0) -> Bar:
    return Bar(
        symbol=symbol,
        time=datetime(2026, 5, 9, 0, 0) + timedelta(minutes=minute),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
    )


def _universe():
    return parse_universe_definition(
        {
            "id": "fine-test",
            "market": "US",
            "symbols": [
                {"ticker": "NVDA", "exchange": "NAS"},
                {"ticker": "MSFT", "exchange": "NAS"},
                {"ticker": "IBM", "exchange": "NYS"},
            ],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )


def test_fine_universe_runtime_refreshes_cache_and_builds_fresh_universe():
    universe = _universe()
    bars = {symbol.key: _bar(symbol, index) for index, symbol in enumerate(universe.symbols, start=100)}
    runtime = FineUniverseRuntime(
        universe=universe,
        provider=FakeProvider(bars),
        max_age_seconds=300,
    )

    report = runtime.refresh_once(max_symbols=2)
    fine_universe = runtime.fine_universe_definition()

    assert report.requested_symbol_count == 2
    assert report.updated_symbol_count == 2
    assert report.failed_symbol_count == 0
    assert report.cached_symbol_count == 2
    assert report.fresh_symbol_count == 2
    assert fine_universe.id == "fine-test-fine"
    assert fine_universe.symbol_keys == ("US:NVDA", "US:MSFT")
    assert fine_universe.properties_for("US:NVDA")["exchange"] == "NAS"


def test_fine_universe_cache_excludes_stale_entries_from_fine_universe():
    universe = _universe()
    cache = FineUniverseCache()
    now = datetime(2026, 5, 9, 1, 0)
    cache.update_bar(_bar(universe.symbols[0], 100), updated_at=now - timedelta(seconds=10))
    cache.update_bar(_bar(universe.symbols[1], 101), updated_at=now - timedelta(seconds=400))

    fine_universe = cache.to_universe_definition(universe, max_age_seconds=300, now=now)

    assert fine_universe.symbol_keys == ("US:NVDA",)
    assert cache.entry("US:MSFT").is_fresh(max_age_seconds=300, now=now) is False


def test_fine_universe_runtime_keeps_stale_cache_entry_when_refresh_fails():
    universe = _universe()
    provider = FakeProvider(
        {universe.symbols[0].key: _bar(universe.symbols[0], 100)},
        failures={universe.symbols[1].key: "quote unavailable"},
    )
    runtime = FineUniverseRuntime(universe=universe, provider=provider)

    report = runtime.refresh_once(symbols=list(universe.symbols[:2]))

    assert report.updated_symbol_count == 1
    assert report.failed_symbol_count == 1
    assert report.failures[0].symbol_key == "US:MSFT"
    assert runtime.cache.entry("US:MSFT").failure_message == "quote unavailable"
    with pytest.raises(RuntimeError):
        runtime.refresh_once(symbols=list(universe.symbols[:2]), min_success=2)

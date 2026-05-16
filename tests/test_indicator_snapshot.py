from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.indicators import IndicatorEngine, IndicatorSnapshotStore
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.universe.loader import parse_universe_definition


def _bar(symbol: Symbol, minute: int, close: float) -> Bar:
    time = datetime(2026, 5, 7, 9, 0) + timedelta(minutes=minute)
    return Bar(symbol, time, close, close, close, close, 100, resolution="daily")


def _engine_with_sma() -> tuple[IndicatorEngine, Symbol]:
    universe = parse_universe_definition(
        {
            "id": "minute-smoke",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_2_close", "type": "sma", "period": 2}],
        }
    )
    symbol = Symbol("005930", "KRX")
    engine = IndicatorEngine()
    engine.register_universe("swing-kor", universe)
    return engine, symbol


def test_indicator_snapshot_freezes_current_values_after_live_engine_moves_on():
    engine, symbol = _engine_with_sma()
    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 0), bars={symbol.key: _bar(symbol, 0, 10)}))
    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 1), bars={symbol.key: _bar(symbol, 1, 20)}))

    snapshot = engine.snapshot("swing-kor", universe_id="minute-smoke")

    assert snapshot.value(symbol.key, "sma_2_close") == pytest.approx(15)
    assert snapshot.ready_values(symbol.key) == {"sma_2_close": pytest.approx(15)}
    assert snapshot.as_of == datetime(2026, 5, 7, 9, 1)

    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 2), bars={symbol.key: _bar(symbol, 2, 30)}))

    assert engine.value("swing-kor", symbol, "sma_2_close") == pytest.approx(25)
    assert snapshot.value(symbol.key, "sma_2_close") == pytest.approx(15)


def test_indicator_snapshot_values_are_read_only():
    engine, symbol = _engine_with_sma()
    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 0), bars={symbol.key: _bar(symbol, 0, 10)}))
    snapshot = engine.snapshot("swing-kor")

    with pytest.raises(TypeError):
        snapshot.values[symbol.key]["sma_2_close"] = snapshot.values[symbol.key]["sma_2_close"]


def test_indicator_snapshot_exposes_latest_bar_metadata_to_alpha_context():
    from leaps_quant_engine.alpha import SnapshotContext

    engine, symbol = _engine_with_sma()
    engine.on_data(
        DataSlice(
            time=datetime(2026, 5, 7, 9, 0),
            bars={
                symbol.key: Bar(
                    symbol,
                    datetime(2026, 5, 7, 9, 0),
                    110,
                    120,
                    98,
                    115,
                    100,
                    resolution="daily",
                    metadata={
                        "opening_context_source": "daily_ohlc_proxy",
                        "opening_gap_pct": 0.1,
                    },
                )
            },
        )
    )

    context = SnapshotContext.from_indicator_snapshot(engine.snapshot("swing-kor"))

    assert context.metadata(symbol)["opening_context_source"] == "daily_ohlc_proxy"
    assert context.metadata_value(symbol.key, "opening_gap_pct") == pytest.approx(0.1)


def test_indicator_snapshot_store_keeps_pending_until_swap():
    engine, symbol = _engine_with_sma()
    store = IndicatorSnapshotStore()

    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 0), bars={symbol.key: _bar(symbol, 0, 10)}))
    first = engine.snapshot("swing-kor")
    store.publish_active(first)

    engine.on_data(DataSlice(time=datetime(2026, 5, 7, 9, 1), bars={symbol.key: _bar(symbol, 1, 20)}))
    second = engine.snapshot("swing-kor")
    store.publish_pending(second)

    assert store.active() is first
    assert store.pending() is second

    swapped = store.swap()

    assert swapped is second
    assert store.active() is second
    assert store.pending() is None

from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.universe.loader import load_universe_definition, parse_universe_definition


def _bar(symbol: Symbol, day: int, close: float, volume: int = 10, resolution: str = "daily") -> Bar:
    time = datetime(2026, 5, 1) + timedelta(days=day)
    return Bar(symbol, time, close, close, close, close, volume, resolution=resolution)


def test_load_universe_definition_from_file():
    universe = load_universe_definition("configs/universes/swing_kor_core.json")

    assert universe.id == "swing-kor-core"
    assert universe.symbol_keys[:2] == ("KRX:005930", "KRX:000660")
    assert [indicator.name for indicator in universe.indicators] == [
        "close",
        "sma_3_close",
        "momentum_2_close",
        "dollar_volume_2",
    ]


def test_us_live_smoke_universe_uses_live_indicator_resolution():
    universe = load_universe_definition("configs/universes/us_live_smoke.json")

    assert {indicator.resolution for indicator in universe.indicators} == {"live"}


def test_parse_universe_definition_accepts_symbol_metadata_for_mixed_exchanges():
    universe = parse_universe_definition(
        {
            "id": "us-live",
            "market": "US",
            "symbols": [
                {"ticker": "NVDA", "exchange": "NAS"},
                {"symbol": "IBM", "exchange": "NYS"},
            ],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )

    assert universe.symbol_keys == ("US:NVDA", "US:IBM")
    assert universe.properties_for("US:NVDA")["exchange"] == "NAS"
    assert universe.properties_for(universe.symbols[1])["exchange"] == "NYS"


def test_parse_universe_definition_accepts_optional_indicator_readiness():
    universe = parse_universe_definition(
        {
            "id": "test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_3_close", "type": "sma", "period": 3},
                {"name": "roc_60_close", "type": "roc", "period": 60, "readiness": "optional"},
                {"name": "roc_120_close", "type": "roc", "period": 120, "required_for_warmup": False},
            ],
        }
    )

    assert universe.indicators[0].readiness == "required"
    assert universe.indicators[0].required_for_warmup is True
    assert universe.indicators[1].readiness == "optional"
    assert universe.indicators[1].required_for_warmup is False
    assert universe.indicators[2].readiness == "optional"
    assert universe.indicators[2].required_for_warmup is False


def test_parse_universe_definition_defaults_indicators_to_confirmed_daily_resolution():
    universe = parse_universe_definition(
        {
            "id": "test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_3_close", "type": "sma", "period": 3}],
        }
    )

    assert universe.indicators[0].resolution == "daily"


def test_indicator_engine_registers_universe_and_updates_only_active_symbols():
    universe = parse_universe_definition(
        {
            "id": "test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [
                {"name": "sma_2_close", "type": "sma", "period": 2},
                {"name": "momentum_1_close", "type": "momentum", "period": 1},
            ],
        }
    )
    symbol = Symbol("005930", "KRX")
    ignored = Symbol("000660", "KRX")
    engine = IndicatorEngine()
    engine.register_universe("swing-kor", universe)

    engine.warm_up("swing-kor", [_bar(symbol, 0, 10), _bar(ignored, 0, 100)])
    assert engine.value("swing-kor", symbol, "sma_2_close") is None

    engine.on_data(
        DataSlice(
            time=datetime(2026, 5, 2),
            bars={
                symbol.key: _bar(symbol, 1, 20),
                ignored.key: _bar(ignored, 1, 200),
            },
        )
    )

    assert engine.value("swing-kor", symbol, "sma_2_close") == pytest.approx(15)
    assert engine.value("swing-kor", symbol, "momentum_1_close") == pytest.approx(1.0)
    assert engine.ready_values("swing-kor", symbol) == {
        "sma_2_close": pytest.approx(15),
        "momentum_1_close": pytest.approx(1.0),
    }
    assert engine.values_for("swing-kor", [symbol], ["sma_2_close"]) == {
        "KRX:005930": {"sma_2_close": pytest.approx(15)}
    }


def test_indicator_engine_keeps_same_symbol_isolated_by_sleeve():
    swing_universe = parse_universe_definition(
        {
            "id": "swing",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_fast", "type": "sma", "period": 2}],
        }
    )
    micro_universe = parse_universe_definition(
        {
            "id": "micro",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_fast", "type": "sma", "period": 3}],
        }
    )
    symbol = Symbol("005930", "KRX")
    engine = IndicatorEngine()
    engine.register_universe("swing-kor", swing_universe)
    engine.register_universe("micro-kor", micro_universe)

    for day, close in enumerate([10, 20, 30]):
        engine.on_data(
            DataSlice(
                time=datetime(2026, 5, 1) + timedelta(days=day),
                bars={symbol.key: _bar(symbol, day, close)},
            )
        )

    assert engine.value("swing-kor", symbol, "sma_fast") == pytest.approx(25)
    assert engine.value("micro-kor", symbol, "sma_fast") == pytest.approx(20)

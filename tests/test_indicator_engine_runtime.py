from datetime import datetime, timedelta

import pytest

from leaps_quant_engine import build_indicator_engine_from_config
from leaps_quant_engine.backtesting import VirtualMarketDataProvider
from leaps_quant_engine.config import parse_pipeline_config
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.loader import parse_universe_definition


class FakeMarketDataProvider:
    def __init__(self, history_by_key, latest_by_key=None):
        self.history_by_key = history_by_key
        self.latest_by_key = latest_by_key or {}

    def get_history(self, symbol, *, start=None, end=None):
        return list(self.history_by_key.get(symbol.key, []))

    def get_latest_bar(self, symbol):
        return self.latest_by_key[symbol.key]


def _bar(symbol: Symbol, day: int, close: float, volume: int = 10) -> Bar:
    time = datetime(2026, 5, 1) + timedelta(days=day)
    return Bar(symbol, time, close, close, close, close, volume)


def test_runtime_builds_indicator_engine_from_sleeve_universe_config():
    config = parse_pipeline_config(
        {
            "engine": {"mode": "backtest", "timezone": "Asia/Seoul"},
            "sleeves": [
                {
                    "id": "swing-kor",
                    "cash": 1_000_000,
                    "algorithm": "examples.buy_and_hold:BuyAndHoldAlgorithm",
                    "universe": "configs/universes/swing_kor_core.json",
                }
            ],
        }
    )

    engine = build_indicator_engine_from_config(config)

    assert [symbol.key for symbol in engine.symbols_for_sleeve("swing-kor")] == [
        "KRX:000660",
        "KRX:005930",
        "KRX:035420",
    ]


def test_indicator_engine_warms_up_from_market_data_provider_and_updates_latest_slice():
    universe = parse_universe_definition(
        {
            "id": "test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_3_close", "type": "sma", "period": 3}],
        }
    )
    symbol = Symbol("005930", "KRX")
    provider = FakeMarketDataProvider(
        history_by_key={symbol.key: [_bar(symbol, 0, 10), _bar(symbol, 1, 20)]},
        latest_by_key={symbol.key: _bar(symbol, 2, 30)},
    )
    engine = IndicatorEngine()
    engine.register_universe("swing-kor", universe)

    engine.warm_up_from_provider("swing-kor", provider)
    assert engine.value("swing-kor", symbol, "sma_3_close") is None

    data = engine.update_from_provider(provider)

    assert list(data.bars) == ["KRX:005930"]
    assert engine.value("swing-kor", symbol, "sma_3_close") == pytest.approx(20)


def test_indicator_engine_updates_from_virtual_market_data_provider_for_backtests():
    universe = parse_universe_definition(
        {
            "id": "test",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "momentum_2_close", "type": "momentum", "period": 2}],
        }
    )
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [_bar(symbol, 0, 100), _bar(symbol, 1, 110), _bar(symbol, 2, 121)]
    )
    engine = IndicatorEngine()
    engine.register_universe("swing-kor", universe)

    engine.warm_up_from_provider("swing-kor", provider, end=datetime(2026, 5, 3))

    assert engine.value("swing-kor", symbol, "momentum_2_close") == pytest.approx(0.21)

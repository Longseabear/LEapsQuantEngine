from datetime import datetime

import pytest

from leaps_quant_engine.adapters.kis import (
    KISBrokerEngineMarketDataProvider,
    KISCachedMarketDataProvider,
    MarketDataEngineLiveQuoteProvider,
)
from leaps_quant_engine.market_data import MarketDataError
from leaps_quant_engine.models import Symbol


class FakeBrokerClient:
    def __init__(self) -> None:
        self.calls = []

    def health_check(self):
        return {"status": "ok"}

    def call_operation(self, operation, arguments=None):
        self.calls.append((operation, arguments))
        if operation == "get_stock_price":
            return {"stck_prpr": "70000", "acml_vol": "123"}
        if operation == "get_daily_ohlcv":
            return {
                "output2": [
                    {
                        "stck_bsop_date": "20260507",
                        "stck_oprc": "69000",
                        "stck_hgpr": "71000",
                        "stck_lwpr": "68000",
                        "stck_clpr": "70000",
                        "acml_vol": "1000",
                    }
                ]
            }
        if operation == "get_or_cache_daily_ohlcv":
            return {
                "bars": [
                    {
                        "stck_bsop_date": "20260507",
                        "stck_oprc": "69000",
                        "stck_hgpr": "71000",
                        "stck_lwpr": "68000",
                        "stck_clpr": "70000",
                        "acml_vol": "1000",
                    }
                ]
            }
        raise AssertionError(operation)


class FakeMarketDataEngineClient:
    def __init__(self) -> None:
        self.calls = []

    def health_check(self):
        return {"status": "ok"}

    def call_tool(self, tool, arguments=None):
        self.calls.append((tool, arguments))
        if tool == "get_stock_price":
            return {
                "last_price": "211.44",
                "open_price": "210.00",
                "high_price": "212.00",
                "low_price": "209.50",
                "volume": "103089440",
            }
        if tool == "get_or_cache_daily_ohlcv":
            return {
                "rows": [
                    {
                        "stck_bsop_date": "20260507",
                        "stck_oprc": "69000",
                        "stck_hgpr": "71000",
                        "stck_lwpr": "68000",
                        "stck_clpr": "70000",
                        "acml_vol": "1000",
                    }
                ]
            }
        if tool == "get_or_cache_domestic_minute_bars":
            return {
                "bars": [
                    {
                        "time": "090100",
                        "open": "70000",
                        "high": "70100",
                        "low": "69900",
                        "close": "70050",
                        "volume": "100",
                    },
                    {
                        "date": "20260508",
                        "time": "09:00:00",
                        "open": "69900",
                        "high": "70000",
                        "low": "69800",
                        "close": "69950",
                        "volume": "90",
                    },
                ]
            }
        raise AssertionError(tool)


def test_kis_provider_normalizes_latest_domestic_quote_to_bar():
    provider = KISBrokerEngineMarketDataProvider(client=FakeBrokerClient())

    bar = provider.get_latest_bar(Symbol("005930", "KRX"))

    assert bar.close == 70000.0
    assert bar.volume == 123
    assert provider.client.calls[0] == (
        "get_stock_price",
        {"market": "domestic", "symbol": "005930", "exchange": None},
    )


def test_kis_provider_normalizes_daily_history_rows():
    provider = KISBrokerEngineMarketDataProvider(client=FakeBrokerClient())

    bars = provider.get_history(Symbol("005930", "KRX"), start=datetime(2026, 5, 1), end=datetime(2026, 5, 7))

    assert len(bars) == 1
    assert bars[0].time == datetime(2026, 5, 7)
    assert bars[0].open == 69000.0
    assert bars[0].close == 70000.0
    assert bars[0].resolution == "daily"
    assert provider.client.calls[0][1]["start_date"] == "20260501"
    assert provider.client.calls[0][1]["end_date"] == "20260507"


def test_kis_provider_sorts_daily_history_chronologically():
    class ReverseHistoryClient(FakeBrokerClient):
        def call_operation(self, operation, arguments=None):
            return {
                "output2": [
                    {
                        "stck_bsop_date": "20260507",
                        "stck_oprc": "1",
                        "stck_hgpr": "1",
                        "stck_lwpr": "1",
                        "stck_clpr": "1",
                    },
                    {
                        "stck_bsop_date": "20260504",
                        "stck_oprc": "1",
                        "stck_hgpr": "1",
                        "stck_lwpr": "1",
                        "stck_clpr": "1",
                    },
                ]
            }

    provider = KISBrokerEngineMarketDataProvider(client=ReverseHistoryClient())

    bars = provider.get_history(Symbol("005930", "KRX"))

    assert [bar.time for bar in bars] == [datetime(2026, 5, 4), datetime(2026, 5, 7)]


def test_kis_provider_uses_cache_first_daily_history_operation():
    provider = KISBrokerEngineMarketDataProvider(client=FakeBrokerClient())

    bars = provider.get_cached_daily_history(
        Symbol("005930", "KRX"),
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 7),
        refresh=True,
    )

    assert len(bars) == 1
    assert bars[0].close == 70000.0
    assert provider.client.calls[0] == (
        "get_or_cache_daily_ohlcv",
        {
            "market": "domestic",
            "symbol": "005930",
            "period_code": "D",
            "adjusted_price": True,
            "start_date": "20260501",
            "end_date": "20260507",
            "refresh": True,
        },
    )


def test_market_data_engine_live_quote_provider_uses_universe_exchange_map():
    provider = MarketDataEngineLiveQuoteProvider(
        client=FakeMarketDataEngineClient(),
        exchange_by_symbol={"US:NVDA": "NAS"},
    )

    bar = provider.get_latest_bar(Symbol("NVDA", "US"))

    assert bar.close == 211.44
    assert bar.volume == 103089440
    assert provider.client.calls[0] == (
        "get_stock_price",
        {"market": "overseas", "symbol": "NVDA", "exchange": "NAS"},
    )


def test_market_data_engine_live_quote_provider_requires_overseas_exchange():
    provider = MarketDataEngineLiveQuoteProvider(client=FakeMarketDataEngineClient())

    with pytest.raises(MarketDataError, match="Exchange is required"):
        provider.get_latest_bar(Symbol("NVDA", "US"))


def test_market_data_engine_live_quote_provider_marks_unusable_domestic_live_quote():
    class StaticReferenceClient(FakeMarketDataEngineClient):
        def call_tool(self, tool, arguments=None):
            self.calls.append((tool, arguments))
            return {
                "last_price": 268500,
                "open_price": 0,
                "high_price": 0,
                "low_price": 0,
                "volume": 157,
                "live_price_usable": False,
                "price_quality_reason": "reference_price_without_distinct_orderbook_price",
                "raw_output": {
                    "stck_sdpr": "268500",
                    "prdy_vrss": "0",
                    "prdy_ctrt": "0.00",
                },
            }

    provider = MarketDataEngineLiveQuoteProvider(client=StaticReferenceClient())

    bar = provider.get_latest_bar(Symbol("005930", "KRX"))

    assert bar.close == 268500
    assert bar.open == 268500
    assert bar.high == 268500
    assert bar.low == 268500
    assert bar.metadata["live_price_usable"] is False
    assert bar.metadata["price_quality_reason"] == "reference_price_without_distinct_orderbook_price"


def test_cached_kis_provider_uses_market_data_engine_cache_tool():
    provider = KISCachedMarketDataProvider(client=FakeMarketDataEngineClient())

    bars = provider.get_cached_daily_history(
        Symbol("005930", "KRX"),
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 7),
        refresh=False,
    )

    assert len(bars) == 1
    assert bars[0].close == 70000.0
    assert provider.client.calls[0] == (
        "get_or_cache_daily_ohlcv",
        {
            "market": "domestic",
            "symbol": "005930",
            "period_code": "D",
            "adjusted_price": True,
            "start_date": "20260501",
            "end_date": "20260507",
            "refresh": False,
        },
    )


def test_cached_kis_provider_loads_domestic_minute_history_from_cache_tool():
    provider = KISCachedMarketDataProvider(client=FakeMarketDataEngineClient())

    bars = provider.get_cached_minute_history(
        Symbol("005930", "KRX"),
        trade_date=datetime(2026, 5, 8),
        start_time="09:00:00",
        end_time="09:01:00",
        interval_minutes=1,
        refresh=True,
    )

    assert [bar.time for bar in bars] == [
        datetime(2026, 5, 8, 9, 0),
        datetime(2026, 5, 8, 9, 1),
    ]
    assert bars[0].close == 69950.0
    assert bars[1].volume == 100
    assert {bar.resolution for bar in bars} == {"minute"}
    assert provider.client.calls[0] == (
        "get_or_cache_domestic_minute_bars",
        {
            "symbol": "005930",
            "trade_date": "2026-05-08",
            "start_time": "09:00:00",
            "end_time": "09:01:00",
            "interval_minutes": 1,
            "refresh": True,
        },
    )


def test_cached_kis_provider_quarantines_zero_volume_adjusted_price_discontinuity():
    class DiscontinuousHistoryClient(FakeMarketDataEngineClient):
        def call_tool(self, tool, arguments=None):
            self.calls.append((tool, arguments))
            return {
                "rows": [
                    {
                        "stck_bsop_date": "20260514",
                        "stck_oprc": "3995",
                        "stck_hgpr": "3995",
                        "stck_lwpr": "3995",
                        "stck_clpr": "3995",
                        "acml_vol": "1000",
                    },
                    {
                        "stck_bsop_date": "20260515",
                        "stck_oprc": "39950",
                        "stck_hgpr": "39950",
                        "stck_lwpr": "39950",
                        "stck_clpr": "39950",
                        "acml_vol": "0",
                    },
                ]
            }

    provider = KISCachedMarketDataProvider(client=DiscontinuousHistoryClient())

    bars = provider.get_cached_daily_history(Symbol("005930", "KRX"))

    assert [bar.time for bar in bars] == [datetime(2026, 5, 14)]
    assert bars[0].close == 3995

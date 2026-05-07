from datetime import datetime

from leaps_quant_engine.adapters.kis import KISBrokerEngineMarketDataProvider
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
        raise AssertionError(operation)


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

from datetime import datetime

import pandas as pd

from leaps_quant_engine.adapters import finance_datareader
from leaps_quant_engine.adapters.finance_datareader import FinanceDataReaderFundamentalProvider
from leaps_quant_engine.models import Symbol


class FakeFinanceDataReader:
    def __init__(self, frame):
        self.frame = frame
        self.requested_markets = []

    def StockListing(self, market):
        self.requested_markets.append(market)
        return self.frame


def test_finance_datareader_fundamentals_load_listing_and_valuation_into_pit_store(monkeypatch):
    fdr = FakeFinanceDataReader(
        pd.DataFrame(
            [
                {
                    "Code": "005930",
                    "Name": "Samsung Electronics",
                    "Market": "KOSPI",
                    "Close": 72000,
                    "Volume": 1000,
                    "Amount": 72_000_000,
                    "Marcap": 430_000_000_000_000,
                    "Stocks": 5_969_782_550,
                },
                {
                    "Code": "000660",
                    "Name": "SK Hynix",
                    "Market": "KOSPI",
                    "Close": 180000,
                    "Volume": 500,
                    "Amount": 90_000_000,
                    "Marcap": 131_000_000_000_000,
                    "Stocks": 728_002_365,
                },
            ]
        )
    )
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: fdr)
    valuation_payload = {
        "005930": {
            "per": 9.5,
            "pbr": 1.2,
            "eps": 7600,
            "bps": 60000,
            "roe": 11.0,
            "dividend_yield": 2.1,
        }
    }
    as_of = datetime(2026, 5, 8)

    store = FinanceDataReaderFundamentalProvider(
        market="KRX",
        include_naver_valuation=True,
        valuation_loader=lambda: valuation_payload,
    ).load_to_store(
        symbols=(Symbol("005930", "KRX"),),
        as_of=as_of,
    )

    assert fdr.requested_markets == ["KRX"]
    assert store.latest(Symbol("005930", "KRX"), "market_cap", as_of=as_of).value == 430_000_000_000_000
    assert store.latest(Symbol("005930", "KRX"), "listed_shares", as_of=as_of).value == 5_969_782_550
    assert store.latest(Symbol("005930", "KRX"), "turnover_krw", as_of=as_of).value == 72_000_000
    assert store.latest(Symbol("005930", "KRX"), "per", as_of=as_of).value == 9.5
    assert store.latest(Symbol("005930", "KRX"), "roe", as_of=as_of).value == 11.0
    assert store.latest(Symbol("000660", "KRX"), "market_cap", as_of=as_of) is None
    assert store.latest(Symbol("005930", "KRX"), "per", as_of=datetime(2026, 5, 7)) is None


def test_finance_datareader_fundamentals_can_filter_names(monkeypatch):
    fdr = FakeFinanceDataReader(
        pd.DataFrame(
            [
                {
                    "Code": "005930",
                    "PER": 10.5,
                    "PBR": 1.3,
                    "Marcap": 430_000_000_000_000,
                }
            ]
        )
    )
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: fdr)
    as_of = datetime(2026, 5, 8)

    store = FinanceDataReaderFundamentalProvider(market="KRX").load_to_store(
        symbols=("005930",),
        as_of=as_of,
        names=("per", "market_cap"),
    )

    assert store.latest("KRX:005930", "per", as_of=as_of).value == 10.5
    assert store.latest("KRX:005930", "market_cap", as_of=as_of).value == 430_000_000_000_000
    assert store.latest("KRX:005930", "pbr", as_of=as_of) is None


def test_finance_datareader_fundamentals_builds_snapshot(monkeypatch):
    fdr = FakeFinanceDataReader(pd.DataFrame([{"Code": "005930", "PER": 10.5}]))
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: fdr)
    symbol = Symbol("005930", "KRX")
    as_of = datetime(2026, 5, 8)

    snapshot = FinanceDataReaderFundamentalProvider(market="KRX").snapshot(
        sleeve_id="LEaps",
        universe_id="value-universe",
        symbols=(symbol,),
        as_of=as_of,
        names=("per",),
    )

    assert snapshot.sleeve_id == "LEaps"
    assert snapshot.value(symbol, "per") == 10.5
    assert snapshot.source_snapshot_id == "FinanceDataReader:StockListing(KRX)"

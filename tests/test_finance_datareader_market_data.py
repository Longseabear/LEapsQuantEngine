from datetime import datetime

from leaps_quant_engine.adapters import finance_datareader
from leaps_quant_engine.adapters.finance_datareader import FinanceDataReaderMarketDataProvider
from leaps_quant_engine.models import Symbol


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(self.rows)


class _FakeFDR:
    def __init__(self):
        self.calls = []

    def DataReader(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        return _Frame(
            [
                (
                    datetime(2026, 1, 2),
                    {"Open": 100, "High": 105, "Low": 99, "Close": 103, "Volume": 1000},
                )
            ]
        )


def test_finance_datareader_daily_history_reuses_disk_cache(monkeypatch, tmp_path):
    fdr = _FakeFDR()
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: fdr)
    provider = FinanceDataReaderMarketDataProvider(cache_root=tmp_path / "fdr-cache")
    symbol = Symbol("005930", "KRX")

    first = provider.get_cached_daily_history(
        symbol,
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 31),
    )
    second = provider.get_cached_daily_history(
        symbol,
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 31),
    )

    assert fdr.calls == [("005930", "2026-01-01", "2026-01-31")]
    assert [bar.close for bar in first] == [103.0]
    assert [bar.close for bar in second] == [103.0]
    assert list((tmp_path / "fdr-cache" / "KRX" / "005930").glob("*.json"))


def test_finance_datareader_daily_history_refresh_rewrites_cache(monkeypatch, tmp_path):
    fdr = _FakeFDR()
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: fdr)
    provider = FinanceDataReaderMarketDataProvider(cache_root=tmp_path / "fdr-cache")
    symbol = Symbol("005930", "KRX")

    provider.get_cached_daily_history(symbol, start=datetime(2026, 1, 1), end=datetime(2026, 1, 31))
    provider.get_cached_daily_history(
        symbol,
        start=datetime(2026, 1, 1),
        end=datetime(2026, 1, 31),
        refresh=True,
    )

    assert fdr.calls == [
        ("005930", "2026-01-01", "2026-01-31"),
        ("005930", "2026-01-01", "2026-01-31"),
    ]

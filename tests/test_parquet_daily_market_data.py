from __future__ import annotations

from datetime import datetime

import pytest

from leaps_quant_engine.adapters import parquet_daily
from leaps_quant_engine.adapters.parquet_daily import ParquetDailyBarProvider
from leaps_quant_engine.market_data import MarketDataError
from leaps_quant_engine.models import Symbol


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.rows)


class _FakePandas:
    def __init__(self, rows_by_name):
        self.rows_by_name = rows_by_name
        self.calls = []

    def read_parquet(self, path):
        self.calls.append(path.name)
        return _Frame(self.rows_by_name[path.name])


def test_parquet_daily_provider_loads_monthly_bars(monkeypatch, tmp_path):
    rows = {
        "krx_2026_04.parquet": [
            {
                "market": "KRX",
                "symbol": "KRX:005930",
                "date": "2026-04-30",
                "open": 100,
                "high": 110,
                "low": 95,
                "close": 105,
                "volume": 1000,
                "adjusted": True,
                "source": "kis-cache",
            }
        ],
        "krx_2026_05.parquet": [
            {
                "market": "KRX",
                "symbol": "KRX:005930",
                "date": "2026-05-02",
                "open": 105,
                "high": 115,
                "low": 101,
                "close": 112,
                "volume": 1200,
                "metadata_json": '{"quality":"confirmed"}',
            },
            {
                "market": "KRX",
                "symbol": "KRX:000660",
                "date": "2026-05-02",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            },
        ],
    }
    fake_pandas = _FakePandas(rows)
    monkeypatch.setattr(parquet_daily, "_load_pandas", lambda: fake_pandas)
    for name in rows:
        (tmp_path / name).write_text("", encoding="utf-8")

    provider = ParquetDailyBarProvider(root=tmp_path)
    bars = provider.get_history(
        Symbol("005930", "KRX"),
        start=datetime(2026, 4, 1),
        end=datetime(2026, 5, 31),
    )

    assert fake_pandas.calls == ["krx_2026_04.parquet", "krx_2026_05.parquet"]
    assert [bar.time for bar in bars] == [datetime(2026, 4, 30), datetime(2026, 5, 2)]
    assert [bar.close for bar in bars] == [105.0, 112.0]
    assert bars[0].metadata["source"] == "kis-cache"
    assert bars[0].metadata["adjusted"] is True
    assert bars[1].metadata["quality"] == "confirmed"
    assert bars[1].resolution == "daily"


def test_parquet_daily_provider_latest_bar_uses_sorted_history(monkeypatch, tmp_path):
    rows = {
        "krx_2026_05.parquet": [
            {
                "market": "KRX",
                "symbol": "005930",
                "date": "2026-05-03",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 103,
                "volume": 1,
            },
            {
                "market": "KRX",
                "symbol": "005930",
                "date": "2026-05-01",
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 101,
                "volume": 1,
            },
        ],
    }
    monkeypatch.setattr(parquet_daily, "_load_pandas", lambda: _FakePandas(rows))
    (tmp_path / "krx_2026_05.parquet").write_text("", encoding="utf-8")

    latest = ParquetDailyBarProvider(root=tmp_path).get_latest_bar(Symbol("005930", "KRX"))

    assert latest.time == datetime(2026, 5, 3)
    assert latest.close == 103.0


def test_parquet_daily_provider_reports_missing_files(tmp_path):
    provider = ParquetDailyBarProvider(root=tmp_path)

    with pytest.raises(MarketDataError, match="No Parquet daily files"):
        provider.get_history(Symbol("005930", "KRX"), start=datetime(2026, 5, 1), end=datetime(2026, 5, 31))

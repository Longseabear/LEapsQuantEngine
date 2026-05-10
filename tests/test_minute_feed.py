from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from leaps_quant_engine.minute_feed import StaticMinuteBarProvider, YFinanceMinuteBarProvider, download_us_minute_feed
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


def test_yfinance_minute_provider_chunks_long_requests(monkeypatch):
    pd = pytest.importorskip("pandas")
    calls: list[tuple[str, str]] = []

    def fake_download(ticker, *, start, end, interval, auto_adjust, prepost, progress, threads):
        calls.append((start, end))
        return pd.DataFrame(
            {
                "Open": [100.0],
                "High": [101.0],
                "Low": [99.0],
                "Close": [100.5],
                "Volume": [1000],
            },
            index=pd.DatetimeIndex([pd.Timestamp(f"{start} 09:30:00", tz="America/New_York")]),
        )

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    provider = YFinanceMinuteBarProvider(max_request_days=6)
    bars = provider.download(
        Symbol("SPY", "US"),
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 10, 23, 59, 59),
        interval="1m",
    )

    assert calls == [("2026-05-01", "2026-05-07"), ("2026-05-07", "2026-05-11")]
    assert [bar.time for bar in bars] == [datetime(2026, 5, 1, 9, 30), datetime(2026, 5, 7, 9, 30)]
    assert {bar.symbol.key for bar in bars} == {"US:SPY"}


def test_download_us_minute_feed_marks_partial_when_symbol_is_empty(tmp_path):
    universe = UniverseDefinition(
        id="us-test",
        market="US",
        symbols=(Symbol("SPY", "US"), Symbol("QQQ", "US")),
        indicators=(),
    )
    provider = StaticMinuteBarProvider(
        bars_by_symbol={
            "US:SPY": [
                Bar(
                    Symbol("SPY", "US"),
                    datetime(2026, 5, 1, 9, 30),
                    100,
                    101,
                    99,
                    100.5,
                    1000,
                    resolution="minute",
                )
            ]
        }
    )

    report = download_us_minute_feed(
        universe,
        provider=provider,
        output_path=tmp_path / "feed.csv",
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 1, 16),
    )

    assert report.status == "partial"
    assert report.downloaded_symbol_count == 1
    assert report.empty_symbols == ("US:QQQ",)

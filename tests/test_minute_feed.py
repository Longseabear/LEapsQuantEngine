from __future__ import annotations

import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from leaps_quant_engine.minute_feed import (
    KISCachedMinuteBarProvider,
    StaticMinuteBarProvider,
    YFinanceMinuteBarProvider,
    write_minute_feed_csv,
    build_minute_feed_cache,
    download_us_minute_feed,
    export_minute_feed_cache,
    load_minute_feed_cache_bars,
    yfinance_symbol_map_for_universe,
)
from leaps_quant_engine.backtesting import load_minute_replay_feed
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


def test_yfinance_minute_provider_retries_empty_chunks_by_day(monkeypatch):
    pd = pytest.importorskip("pandas")
    calls: list[tuple[str, str]] = []

    def fake_download(ticker, *, start, end, interval, auto_adjust, prepost, progress, threads):
        calls.append((start, end))
        if start == "2026-05-01" and end == "2026-05-04":
            return pd.DataFrame()
        if start == "2026-05-02":
            return pd.DataFrame(
                {
                    "Open": [100.0],
                    "High": [101.0],
                    "Low": [99.0],
                    "Close": [100.5],
                    "Volume": [1000],
                },
                index=pd.DatetimeIndex([pd.Timestamp("2026-05-02 09:30:00", tz="America/New_York")]),
            )
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    provider = YFinanceMinuteBarProvider(max_request_days=3)
    bars = provider.download(
        Symbol("SPY", "US"),
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 3, 23, 59, 59),
        interval="1m",
    )

    assert ("2026-05-01", "2026-05-04") in calls
    assert ("2026-05-02", "2026-05-03") in calls
    assert [bar.time for bar in bars] == [datetime(2026, 5, 2, 9, 30)]


def test_yfinance_hourly_provider_skips_daily_retry_for_empty_chunks(monkeypatch):
    pd = pytest.importorskip("pandas")
    calls: list[tuple[str, str]] = []

    def fake_download(ticker, *, start, end, interval, auto_adjust, prepost, progress, threads):
        calls.append((start, end))
        return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=fake_download))

    provider = YFinanceMinuteBarProvider(max_request_days=3)
    bars = provider.download(
        Symbol("SPY", "US"),
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 3, 23, 59, 59),
        interval="60m",
    )

    assert calls == [("2026-05-01", "2026-05-04")]
    assert bars == []


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


def test_minute_feed_cache_builds_compressed_days_and_exports_backtest_feed(tmp_path):
    universe = UniverseDefinition(
        id="krx-top",
        market="KRX",
        symbols=(Symbol("005930", "KRX"), Symbol("000660", "KRX")),
        indicators=(),
    )
    provider = StaticMinuteBarProvider(
        bars_by_symbol={
            "KRX:005930": [
                Bar(Symbol("005930", "KRX"), datetime(2026, 5, 14, 9, 0), 100, 101, 99, 100, 1000, resolution="minute"),
                Bar(Symbol("005930", "KRX"), datetime(2026, 5, 15, 9, 0), 102, 103, 101, 102, 1200, resolution="minute"),
            ],
            "KRX:000660": [
                Bar(Symbol("000660", "KRX"), datetime(2026, 5, 15, 9, 0), 200, 201, 199, 200, 2000, resolution="minute"),
            ],
        }
    )

    build_report = build_minute_feed_cache(
        universe,
        provider=provider,
        cache_root=tmp_path / "cache",
        start=datetime(2026, 5, 14),
        end=datetime(2026, 5, 15, 23, 59, 59),
        timezone="Asia/Seoul",
    )

    assert build_report.status == "ok"
    assert build_report.row_count == 3
    assert all(path.endswith(".csv.gz") for path in build_report.day_files)

    output = tmp_path / "feed.csv"
    export_report = export_minute_feed_cache(
        universe,
        cache_root=tmp_path / "cache",
        output_path=output,
        start=datetime(2026, 5, 15, 9, 0),
        end=datetime(2026, 5, 15, 9, 0),
        symbols=("KRX:005930",),
    )

    assert export_report.status == "ok"
    assert export_report.row_count == 1
    assert export_report.symbols == ("KRX:005930",)
    rows = output.read_text(encoding="utf-8").splitlines()
    assert rows == [
        "symbol,time,open,high,low,close,volume",
        "KRX:005930,2026-05-15T09:00:00,102,103,101,102,1200",
    ]

    loaded_bars, load_report = load_minute_feed_cache_bars(
        universe,
        cache_root=tmp_path / "cache",
        start=datetime(2026, 5, 15, 9, 0),
        end=datetime(2026, 5, 15, 9, 0),
        symbols=("KRX:005930",),
    )

    assert load_report.status == "ok"
    assert load_report.row_count == 1
    assert load_report.loaded_symbol_count == 1
    assert [bar.symbol.key for bar in loaded_bars] == ["KRX:005930"]
    assert [bar.time for bar in loaded_bars] == [datetime(2026, 5, 15, 9, 0)]


def test_minute_feed_preserves_session_metadata_for_opening_context(tmp_path):
    path = tmp_path / "opening.csv"
    bar = Bar(
        Symbol("005930", "KRX"),
        datetime(2026, 5, 15, 8, 50),
        100,
        101,
        99,
        100.5,
        1000,
        resolution="minute",
    )

    write_minute_feed_csv(path, [bar], include_session_metadata=True)

    rows = path.read_text(encoding="utf-8").splitlines()
    assert rows[0] == (
        "symbol,time,open,high,low,close,volume,market_session_scope,"
        "market_session_phase,is_regular_market_open,is_orderable_session,"
        "is_extended_market_hours,session_source"
    )
    assert "regular_open_auction" in rows[1]
    feed = load_minute_replay_feed(path, default_market="KRX")
    loaded = feed[0].bars["KRX:005930"]
    assert loaded.metadata["market_session_phase"] == "regular_open_auction"
    assert loaded.metadata["is_orderable_session"] is True
    assert loaded.metadata["is_regular_market_open"] is True


def test_kis_cached_minute_provider_requests_each_trade_day_with_time_bounds():
    class FakeKISProvider:
        def __init__(self):
            self.calls = []

        def get_cached_minute_history(
            self,
            symbol,
            *,
            trade_date,
            start_time,
            end_time,
            interval_minutes,
            refresh,
        ):
            self.calls.append((symbol.key, trade_date, start_time, end_time, interval_minutes, refresh))
            return [
                Bar(
                    symbol,
                    datetime(trade_date.year, trade_date.month, trade_date.day, 8, 50),
                    100,
                    101,
                    99,
                    100,
                    1000,
                    resolution="minute",
                )
            ]

    fake = FakeKISProvider()
    provider = KISCachedMinuteBarProvider(
        provider=fake,
        refresh=True,
        daily_start_time="08:30:00",
        daily_end_time="18:00:00",
    )

    bars = provider.download(
        Symbol("005930", "KRX"),
        start=datetime(2026, 5, 14, 8, 30),
        end=datetime(2026, 5, 15, 18, 0),
        interval="1m",
    )

    assert [call[2:6] for call in fake.calls] == [
        ("08:30:00", "18:00:00", 1, True),
        ("08:30:00", "18:00:00", 1, True),
    ]
    assert len(bars) == 2


def test_yfinance_symbol_map_for_krx_uses_market_segment_suffixes():
    universe = UniverseDefinition(
        id="krx",
        market="KRX",
        symbols=(Symbol("005930", "KRX"), Symbol("050890", "KRX")),
        indicators=(),
        symbol_properties={
            "KRX:005930": {"market_segment": "KOSPI", "market_id": "STK"},
            "KRX:050890": {"market_segment": "KOSDAQ", "market_id": "KSQ"},
        },
    )

    assert yfinance_symbol_map_for_universe(universe) == {
        "KRX:005930": "005930.KS",
        "KRX:050890": "050890.KQ",
    }

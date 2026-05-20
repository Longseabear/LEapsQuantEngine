from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.live_snapshot import run_live_indicator_snapshot
from leaps_quant_engine.market_data_snapshot import FileMarketDataSnapshotStore, MarketDataSnapshot, MarketDataSnapshotEngine
from leaps_quant_engine.models import Bar, DataResolution, Symbol
from leaps_quant_engine.snapshots import SnapshotFreshnessPolicy, SnapshotQualityStatus
from leaps_quant_engine.universe.loader import parse_universe_definition


class FakeProvider:
    def __init__(self, bars):
        self.bars = {bar.symbol.key: bar for bar in bars}

    def get_latest_bar(self, symbol):
        return self.bars[symbol.key]

    def get_history(self, symbol, *, start=None, end=None):
        return []


class BestEffortProvider:
    class Client:
        rate_limit_per_second = 17

    client = Client()

    def get_latest_bar(self, symbol):
        if symbol.ticker == "FAIL":
            raise RuntimeError("quote unavailable")
        return _bar(symbol, 0, 10)

    def get_history(self, symbol, *, start=None, end=None):
        return []


def _bar(symbol: Symbol, minute: int, close: float, *, resolution: str = DataResolution.LIVE.value) -> Bar:
    time = datetime(2026, 5, 7, 9, 0) + timedelta(minutes=minute)
    return Bar(symbol, time, close, close, close, close, 100, resolution=resolution)


def test_market_data_snapshot_engine_collects_updates_and_publishes_indicator_snapshot():
    universe = parse_universe_definition(
        {
            "id": "minute-smoke",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "sma_2_close", "type": "sma", "period": 2, "resolution": "live"}],
        }
    )
    symbol = Symbol("005930", "KRX")
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe("swing-kor", universe)

    provider = FakeProvider([_bar(symbol, 0, 10)])
    snapshot_engine = MarketDataSnapshotEngine(provider, indicator_engine, source="fake-live")
    market_snapshot, indicator_snapshots = snapshot_engine.run_once(
        universe_id_by_sleeve={"swing-kor": universe.id}
    )

    assert market_snapshot.source == "fake-live"
    assert list(market_snapshot.bars) == [symbol.key]
    assert indicator_snapshots["swing-kor"].source_snapshot_id == market_snapshot.snapshot_id
    assert snapshot_engine.stores_by_sleeve["swing-kor"].active() is indicator_snapshots["swing-kor"]

    provider.bars[symbol.key] = _bar(symbol, 1, 20)
    _, indicator_snapshots = snapshot_engine.run_once(universe_id_by_sleeve={"swing-kor": universe.id})

    assert indicator_snapshots["swing-kor"].value(symbol.key, "sma_2_close") == pytest.approx(15)


def test_market_data_snapshot_can_feed_indicator_engine_without_provider():
    symbol = Symbol("005930", "KRX")
    snapshot = MarketDataSnapshot.from_bars({symbol.key: _bar(symbol, 0, 10, resolution=DataResolution.MINUTE.value)}, source="minute-replay")

    data_slice = snapshot.as_data_slice()

    assert data_slice.time == datetime(2026, 5, 7, 9, 0)
    assert data_slice.get(symbol).close == 10
    with pytest.raises(TypeError):
        snapshot.bars[symbol.key] = _bar(symbol, 1, 20)


def test_market_data_snapshot_rejects_mixed_resolution_lanes():
    symbol = Symbol("005930", "KRX")
    other = Symbol("000660", "KRX")

    with pytest.raises(ValueError, match="cannot mix resolution lanes"):
        MarketDataSnapshot.from_bars(
            {
                symbol.key: Bar(symbol, datetime(2026, 5, 7), 10, 10, 10, 10, resolution=DataResolution.DAILY.value),
                other.key: Bar(other, datetime(2026, 5, 7, 9, 0), 20, 20, 20, 20, resolution=DataResolution.MINUTE.value),
            },
            source="mixed",
        )


def test_file_market_data_snapshot_store_round_trips_latest_record(tmp_path):
    symbol = Symbol("005930", "KRX")
    store = FileMarketDataSnapshotStore(tmp_path / "snapshots.jsonl")
    snapshot = MarketDataSnapshot.from_bars({symbol.key: _bar(symbol, 0, 10, resolution=DataResolution.DAILY.value)}, source="kis-gateway")

    store.append(snapshot, metadata={"runtime_id": "live"})
    latest = store.latest()

    assert latest is not None
    assert latest.snapshot.lane == "daily_confirmed"
    assert latest.snapshot.snapshot_id == snapshot.snapshot_id
    assert latest.snapshot.bars[symbol.key].close == 10
    assert latest.metadata == {"runtime_id": "live"}


def test_file_market_data_snapshot_store_filters_latest_by_lane(tmp_path):
    symbol = Symbol("005930", "KRX")
    store = FileMarketDataSnapshotStore(tmp_path / "snapshots.jsonl")
    daily = MarketDataSnapshot.from_bars({symbol.key: _bar(symbol, 0, 10, resolution=DataResolution.DAILY.value)}, source="daily")
    quote = MarketDataSnapshot.from_bars(
        {
            symbol.key: Bar(
                symbol,
                datetime(2026, 5, 7, 9, 1),
                11,
                11,
                11,
                11,
                resolution=DataResolution.LIVE.value,
            )
        },
        source="quote",
    )

    store.append(daily)
    store.append(quote)

    assert store.latest().snapshot.lane == "quote"
    assert store.latest(lane="daily").snapshot.snapshot_id == daily.snapshot_id
    assert store.latest(lane="quote").snapshot.snapshot_id == quote.snapshot_id


def test_market_data_snapshot_engine_best_effort_reports_symbol_failures():
    universe = parse_universe_definition(
        {
            "id": "best-effort",
            "market": "KRX",
            "symbols": ["005930", "FAIL"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe("swing-kor", universe)
    snapshot_engine = MarketDataSnapshotEngine(BestEffortProvider(), indicator_engine)

    result = snapshot_engine.collect_once_best_effort(list(universe.symbols), min_success=1)

    assert result.report.requested_symbol_count == 2
    assert result.report.collected_symbol_count == 1
    assert result.report.failed_symbol_count == 1
    assert result.report.failures[0].symbol_key == "KRX:FAIL"
    assert list(result.snapshot.bars) == ["KRX:005930"]


def test_market_data_snapshot_engine_logs_collection_summary(caplog):
    universe = parse_universe_definition(
        {
            "id": "best-effort",
            "market": "KRX",
            "symbols": ["005930", "FAIL"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe("swing-kor", universe)
    snapshot_engine = MarketDataSnapshotEngine(BestEffortProvider(), indicator_engine, source="fake-live")

    with caplog.at_level("INFO", logger="leaps_quant_engine.market_data_snapshot"):
        snapshot_engine.collect_once_best_effort(list(universe.symbols), min_success=1)

    messages = [record.message for record in caplog.records]
    assert "market_data_snapshot.collect.start" in messages
    assert "market_data_snapshot.collect.symbol_failed" in messages
    assert "market_data_snapshot.collect.complete" in messages
    complete = next(record for record in caplog.records if record.message == "market_data_snapshot.collect.complete")
    assert complete.source == "fake-live"
    assert complete.collected_symbol_count == 1
    assert complete.failed_symbol_count == 1


def test_live_indicator_snapshot_runner_splits_collection_and_indicator_timing():
    universe = parse_universe_definition(
        {
            "id": "live",
            "market": "KRX",
            "symbols": ["005930", "FAIL"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )

    report = run_live_indicator_snapshot(
        universe,
        BestEffortProvider(),
        sleeve_id="live-sleeve",
        source="fake-live",
        min_success=1,
        include_failures=True,
        freshness_policy=SnapshotFreshnessPolicy(degraded_complete_ratio=0.5),
    )

    assert report["source"] == "fake-live"
    assert report["rate_limit_per_second"] == 17
    assert report["requested_symbol_count"] == 2
    assert report["updated_symbol_count"] == 1
    assert report["failed_symbol_count"] == 1
    assert report["indicator_count_per_symbol"] == 1
    assert report["indicator_updates_estimated"] == 1
    assert report["snapshot_quality"]["status"] == "degraded"
    assert report["failures"][0]["symbol"] == "KRX:FAIL"


def test_indicator_snapshot_receives_quality_report_from_live_runner():
    universe = parse_universe_definition(
        {
            "id": "live",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe("live-sleeve", universe)
    snapshot_engine = MarketDataSnapshotEngine(BestEffortProvider(), indicator_engine)
    collection = snapshot_engine.collect_once_best_effort(list(universe.symbols))
    quality = SnapshotFreshnessPolicy().evaluate(
        requested_symbol_count=collection.report.requested_symbol_count,
        collected_symbol_count=collection.report.collected_symbol_count,
        failed_symbol_count=collection.report.failed_symbol_count,
        completed_at=collection.report.completed_at,
        elapsed_ms=collection.report.elapsed_ms,
    )

    snapshots = snapshot_engine.update_indicators(
        collection.snapshot,
        sleeve_ids=["live-sleeve"],
        quality_report_by_sleeve={"live-sleeve": quality},
    )

    assert snapshots["live-sleeve"].quality_report is quality
    assert snapshots["live-sleeve"].quality_report.status == SnapshotQualityStatus.FRESH

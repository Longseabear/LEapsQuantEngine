from datetime import datetime, timedelta

from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker
from leaps_quant_engine.snapshots import SnapshotFreshnessPolicy, SnapshotQualityStatus
from leaps_quant_engine.universe.loader import parse_universe_definition


class FakeHistoryProvider:
    def __init__(self, history_by_key):
        self.history_by_key = history_by_key
        self.calls = []

    def get_cached_daily_history(self, symbol, *, start=None, end=None, refresh=False):
        self.calls.append((symbol.key, start, end, refresh))
        return list(self.history_by_key[symbol.key])


class SequentialLiveProvider:
    class Client:
        rate_limit_per_second = 20

    client = Client()

    def __init__(self, fail_tickers=None):
        self.fail_tickers = set(fail_tickers or [])
        self.calls = []

    def get_latest_bar(self, symbol):
        self.calls.append(symbol.key)
        if symbol.ticker in self.fail_tickers:
            raise RuntimeError("quote unavailable")
        value = 100.0 + len(self.calls)
        return Bar(
            symbol=symbol,
            time=datetime(2026, 5, 8, 9, 0) + timedelta(minutes=len(self.calls)),
            open=value,
            high=value,
            low=value,
            close=value,
            volume=1000 + len(self.calls),
        )

    def get_history(self, symbol, *, start=None, end=None):
        return []


class ReferencePriceProvider(SequentialLiveProvider):
    def get_latest_bar(self, symbol):
        self.calls.append(symbol.key)
        return Bar(
            symbol=symbol,
            time=datetime(2026, 5, 8, 8, 40),
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=100,
            metadata={
                "live_price_usable": False,
                "price_quality_reason": "reference_price_without_distinct_orderbook_price",
            },
        )


class OneInsightAlpha:
    alpha_id = "one-insight"
    version = "1.0"

    def generate(self, context):
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(context.symbol_keys[0]),
                direction=InsightDirection.UP,
                generated_at=datetime(2026, 5, 8, 9, 0),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason="worker_alpha",
            )
        ]


def _bar(symbol: Symbol, day: int, close: float | None = None) -> Bar:
    value = close if close is not None else 100.0 + day
    return Bar(
        symbol=symbol,
        time=datetime(2026, 5, 1) + timedelta(days=day),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1000 + day,
    )


def _worker_universe():
    return parse_universe_definition(
        {
            "id": "worker-test",
            "market": "KRX",
            "symbols": ["005930", "000660"],
            "indicators": [
                {"name": "close", "type": "close", "period": 1},
                {"name": "sma_2_close", "type": "sma", "period": 2, "field": "close"},
            ],
        }
    )


def test_background_snapshot_worker_warms_up_then_publishes_cycles():
    universe = _worker_universe()
    history = {
        symbol.key: [_bar(symbol, 0), _bar(symbol, 1)]
        for symbol in universe.symbols
    }
    history_provider = FakeHistoryProvider(history)
    live_provider = SequentialLiveProvider()
    worker = BackgroundSnapshotWorker(
        universe=universe,
        sleeve_id="swing-kor",
        live_provider=live_provider,
        history_provider=history_provider,
        interval_seconds=0.0,
    )

    report = worker.run(max_cycles=2, warmup=True, refresh_history=True)

    assert report.cycles_completed == 2
    assert report.warmup is not None
    assert report.warmup.ready_symbol_count == 2
    assert [cycle.cycle_index for cycle in report.cycles] == [1, 2]
    assert all(cycle.updated_symbol_count == 2 for cycle in report.cycles)
    assert all(cycle.ready_count_min == 2 for cycle in report.cycles)
    assert len(history_provider.calls) == 2
    assert len(live_provider.calls) == 4
    active = worker.stores_by_sleeve["swing-kor"].active()
    assert active is not None
    assert active.quality_report is not None
    assert active.quality_report.status == SnapshotQualityStatus.FRESH


def test_background_snapshot_worker_degrades_cycle_quality_when_warmup_is_not_ready():
    universe = _worker_universe()
    history = {
        universe.symbols[0].key: [_bar(universe.symbols[0], 0)],
        universe.symbols[1].key: [],
    }
    worker = BackgroundSnapshotWorker(
        universe=universe,
        sleeve_id="swing-kor",
        live_provider=SequentialLiveProvider(),
        history_provider=FakeHistoryProvider(history),
        interval_seconds=0.0,
    )

    report = worker.run(max_cycles=1, warmup=True)

    assert report.warmup is not None
    assert report.warmup.is_ready is False
    assert report.cycles[0].snapshot_quality.status == SnapshotQualityStatus.DEGRADED
    assert "warmup_not_ready" in report.cycles[0].snapshot_quality.reasons
    active = worker.stores_by_sleeve["swing-kor"].active()
    assert active is not None
    assert active.quality_report is not None
    assert "warmup_not_ready" in active.quality_report.reasons


def test_background_snapshot_worker_attaches_degraded_quality_to_partial_cycle():
    universe = _worker_universe()
    worker = BackgroundSnapshotWorker(
        universe=universe,
        sleeve_id="swing-kor",
        live_provider=SequentialLiveProvider(fail_tickers={"000660"}),
        min_success=1,
        freshness_policy=SnapshotFreshnessPolicy(degraded_complete_ratio=0.5),
    )

    report = worker.run_once()

    assert report.requested_symbol_count == 2
    assert report.updated_symbol_count == 1
    assert report.failed_symbol_count == 1
    assert report.snapshot_quality.status == SnapshotQualityStatus.DEGRADED
    assert report.failures == ({"symbol": "KRX:000660", "message": "quote unavailable"},)
    active = worker.stores_by_sleeve["swing-kor"].active()
    assert active is not None
    assert active.quality_report is report.snapshot_quality


def test_background_snapshot_worker_degrades_reference_price_cycle_without_failing_collection():
    universe = _worker_universe()
    worker = BackgroundSnapshotWorker(
        universe=universe,
        sleeve_id="swing-kor",
        live_provider=ReferencePriceProvider(),
        min_success=1,
    )

    report = worker.run_once()

    assert report.updated_symbol_count == 2
    assert report.failed_symbol_count == 0
    assert report.snapshot_quality.status == SnapshotQualityStatus.DEGRADED
    assert report.snapshot_quality.allows_new_entries is False
    assert "live_price_unusable" in report.snapshot_quality.reasons
    assert "price_quality:reference_price_without_distinct_orderbook_price" in report.snapshot_quality.reasons
    active = worker.stores_by_sleeve["swing-kor"].active()
    assert active is not None
    assert active.quality_report is report.snapshot_quality


def test_background_snapshot_worker_runs_alpha_runtime_after_indicator_snapshot():
    universe = _worker_universe()
    worker = BackgroundSnapshotWorker(
        universe=universe,
        sleeve_id="swing-kor",
        live_provider=SequentialLiveProvider(),
        alpha_runtime=AlphaRuntime(active_models=(OneInsightAlpha(),)),
    )

    report = worker.run_once()

    assert report.insight_count == 1
    assert report.alpha_ids == ("one-insight",)
    active_batch = worker.alpha_runtime.store.active()
    assert active_batch is not None
    assert active_batch.insights[0].reason == "worker_alpha"

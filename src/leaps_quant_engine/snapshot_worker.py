from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import statistics
from threading import Event, Thread
import time
from typing import Any

from leaps_quant_engine.alpha import AlphaRuntime, InsightBatch, SnapshotContext
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.market_data_snapshot import MarketDataSnapshotEngine
from leaps_quant_engine.snapshots import IndicatorSnapshotStore, SnapshotFreshnessPolicy, SnapshotQualityReport
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.warmup import WarmupPolicy, WarmupReport, run_daily_indicator_warmup


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SnapshotWorkerCycleReport:
    cycle_index: int
    source: str
    sleeve_id: str
    universe_id: str
    requested_symbol_count: int
    updated_symbol_count: int
    failed_symbol_count: int
    indicator_count_per_symbol: int
    indicator_updates_estimated: int
    market_snapshot_id: str
    indicator_snapshot_id: str
    snapshot_as_of: str
    snapshot_quality: SnapshotQualityReport
    collection_elapsed_ms: float
    indicator_update_snapshot_ms: float
    ready_count_min: int
    ready_count_avg: float
    ready_count_max: int
    started_at: str
    completed_at: str
    failures: tuple[dict[str, str], ...] = ()
    insight_batch_id: str | None = None
    insight_count: int = 0
    alpha_ids: tuple[str, ...] = ()

    def to_dict(self, *, include_failures: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cycle_index": self.cycle_index,
            "source": self.source,
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "requested_symbol_count": self.requested_symbol_count,
            "updated_symbol_count": self.updated_symbol_count,
            "failed_symbol_count": self.failed_symbol_count,
            "indicator_count_per_symbol": self.indicator_count_per_symbol,
            "indicator_updates_estimated": self.indicator_updates_estimated,
            "market_snapshot_id": self.market_snapshot_id,
            "indicator_snapshot_id": self.indicator_snapshot_id,
            "snapshot_as_of": self.snapshot_as_of,
            "snapshot_quality": self.snapshot_quality.to_dict(),
            "collection_elapsed_ms": self.collection_elapsed_ms,
            "indicator_update_snapshot_ms": self.indicator_update_snapshot_ms,
            "ready_count_min": self.ready_count_min,
            "ready_count_avg": self.ready_count_avg,
            "ready_count_max": self.ready_count_max,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "insight_batch_id": self.insight_batch_id,
            "insight_count": self.insight_count,
            "alpha_ids": list(self.alpha_ids),
        }
        if include_failures:
            payload["failures"] = list(self.failures)
        return payload


@dataclass(frozen=True, slots=True)
class SnapshotWorkerRunReport:
    sleeve_id: str
    universe_id: str
    source: str
    cycles_requested: int | None
    cycles_completed: int
    started_at: str
    completed_at: str
    warmup: WarmupReport | None
    cycles: tuple[SnapshotWorkerCycleReport, ...]

    def to_dict(
        self,
        *,
        include_warmup_symbols: bool = True,
        include_failures: bool = True,
    ) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "source": self.source,
            "cycles_requested": self.cycles_requested,
            "cycles_completed": self.cycles_completed,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "warmup": self.warmup.to_dict(include_symbols=include_warmup_symbols) if self.warmup else None,
            "cycles": [
                cycle.to_dict(include_failures=include_failures)
                for cycle in self.cycles
            ],
        }


@dataclass(slots=True)
class BackgroundSnapshotWorker:
    universe: UniverseDefinition
    sleeve_id: str
    live_provider: MarketDataProvider
    history_provider: MarketDataProvider | None = None
    source: str = "market-data-engine"
    history_source: str = "kis-cache"
    min_success: int | None = None
    interval_seconds: float = 60.0
    indicator_engine: IndicatorEngine = field(default_factory=IndicatorEngine)
    stores_by_sleeve: dict[str, IndicatorSnapshotStore] = field(default_factory=dict)
    alpha_runtime: AlphaRuntime | None = None
    freshness_policy: SnapshotFreshnessPolicy = field(default_factory=SnapshotFreshnessPolicy)
    warmup_policy: WarmupPolicy = field(default_factory=WarmupPolicy)
    snapshot_engine: MarketDataSnapshotEngine = field(init=False)
    _stop_event: Event = field(default_factory=Event, init=False)
    _thread: Thread | None = field(default=None, init=False)
    _cycle_index: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.interval_seconds < 0:
            raise ValueError("interval_seconds must be non-negative.")
        if self.sleeve_id not in self.indicator_engine.registries_by_sleeve:
            self.indicator_engine.register_universe(self.sleeve_id, self.universe)
        self.snapshot_engine = MarketDataSnapshotEngine(
            provider=self.live_provider,
            indicator_engine=self.indicator_engine,
            stores_by_sleeve=self.stores_by_sleeve,
            source=self.source,
        )

    def warm_up(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh_history: bool = False,
    ) -> WarmupReport:
        if self.history_provider is None:
            raise ValueError("history_provider is required for warmup.")
        logger.info(
            "background_snapshot_worker.warmup.start",
            extra={
                "source": self.history_source,
                "sleeve_id": self.sleeve_id,
                "universe_id": self.universe.id,
                "universe_size": len(self.universe.symbols),
            },
        )
        result = run_daily_indicator_warmup(
            self.universe,
            self.history_provider,
            sleeve_id=self.sleeve_id,
            start=start,
            end=end,
            refresh_history=refresh_history,
            source=self.history_source,
            policy=self.warmup_policy,
            indicator_engine=self.indicator_engine,
        )
        logger.info(
            "background_snapshot_worker.warmup.complete",
            extra={
                "source": self.history_source,
                "sleeve_id": self.sleeve_id,
                "universe_id": self.universe.id,
                "ready_symbol_count": result.report.ready_symbol_count,
                "ready_ratio": result.report.ready_ratio,
                "is_ready": result.report.is_ready,
                "history_load_ms": result.report.history_load_ms,
                "warmup_update_ms": result.report.warmup_update_ms,
            },
        )
        return result.report

    def run_once(self) -> SnapshotWorkerCycleReport:
        self._cycle_index += 1
        cycle_index = self._cycle_index
        started_at = datetime.now()
        logger.info(
            "background_snapshot_worker.cycle.start",
            extra={
                "cycle_index": cycle_index,
                "source": self.source,
                "sleeve_id": self.sleeve_id,
                "universe_id": self.universe.id,
                "universe_size": len(self.universe.symbols),
                "min_success": self.min_success,
            },
        )
        collection = self.snapshot_engine.collect_once_best_effort(
            list(self.universe.symbols),
            min_success=self.min_success,
        )
        quality_report = self.freshness_policy.evaluate(
            requested_symbol_count=collection.report.requested_symbol_count,
            collected_symbol_count=collection.report.collected_symbol_count,
            failed_symbol_count=collection.report.failed_symbol_count,
            completed_at=collection.report.completed_at,
            elapsed_ms=collection.report.elapsed_ms,
        )
        update_started = time.perf_counter()
        indicator_snapshots = self.snapshot_engine.update_indicators(
            collection.snapshot,
            sleeve_ids=[self.sleeve_id],
            universe_id_by_sleeve={self.sleeve_id: self.universe.id},
            quality_report_by_sleeve={self.sleeve_id: quality_report},
        )
        indicator_update_ms = (time.perf_counter() - update_started) * 1000
        indicator_snapshot = indicator_snapshots[self.sleeve_id]
        insight_batch = self._run_alpha(indicator_snapshot)
        ready_counts = [
            len(indicator_snapshot.ready_values(symbol_key))
            for symbol_key in indicator_snapshot.symbols
        ]
        completed_at = datetime.now()
        report = SnapshotWorkerCycleReport(
            cycle_index=cycle_index,
            source=self.source,
            sleeve_id=self.sleeve_id,
            universe_id=self.universe.id,
            requested_symbol_count=collection.report.requested_symbol_count,
            updated_symbol_count=collection.report.collected_symbol_count,
            failed_symbol_count=collection.report.failed_symbol_count,
            indicator_count_per_symbol=len(self.universe.indicators),
            indicator_updates_estimated=collection.report.collected_symbol_count * len(self.universe.indicators),
            market_snapshot_id=collection.snapshot.snapshot_id,
            indicator_snapshot_id=indicator_snapshot.snapshot_id,
            snapshot_as_of=indicator_snapshot.as_of.isoformat(),
            snapshot_quality=quality_report,
            collection_elapsed_ms=collection.report.elapsed_ms,
            indicator_update_snapshot_ms=indicator_update_ms,
            ready_count_min=min(ready_counts, default=0),
            ready_count_avg=statistics.mean(ready_counts) if ready_counts else 0.0,
            ready_count_max=max(ready_counts, default=0),
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            failures=tuple(
                {"symbol": failure.symbol_key, "message": failure.message}
                for failure in collection.report.failures
            ),
            insight_batch_id=insight_batch.batch_id if insight_batch is not None else None,
            insight_count=len(insight_batch.insights) if insight_batch is not None else 0,
            alpha_ids=insight_batch.alpha_ids if insight_batch is not None else (),
        )
        logger.info(
            "background_snapshot_worker.cycle.complete",
            extra={
                "cycle_index": cycle_index,
                "source": self.source,
                "sleeve_id": self.sleeve_id,
                "universe_id": self.universe.id,
                "updated_symbol_count": report.updated_symbol_count,
                "failed_symbol_count": report.failed_symbol_count,
                "collection_elapsed_ms": report.collection_elapsed_ms,
                "indicator_update_snapshot_ms": report.indicator_update_snapshot_ms,
                "market_snapshot_id": report.market_snapshot_id,
                "indicator_snapshot_id": report.indicator_snapshot_id,
                "quality_status": quality_report.status.value,
                "quality_complete_ratio": quality_report.complete_ratio,
                "quality_reasons": list(quality_report.reasons),
                "insight_count": report.insight_count,
                "alpha_ids": list(report.alpha_ids),
            },
        )
        return report

    def _run_alpha(self, indicator_snapshot) -> InsightBatch | None:
        if self.alpha_runtime is None:
            return None
        context = SnapshotContext.from_indicator_snapshot(indicator_snapshot)
        return self.alpha_runtime.run(context, activate_pending=True, publish_active=True)

    def run(
        self,
        *,
        max_cycles: int | None = None,
        warmup: bool = True,
        warmup_start: datetime | None = None,
        warmup_end: datetime | None = None,
        refresh_history: bool = False,
    ) -> SnapshotWorkerRunReport:
        if max_cycles is not None and max_cycles < 0:
            raise ValueError("max_cycles must be non-negative.")
        started_at = datetime.now()
        self._stop_event.clear()
        warmup_report = (
            self.warm_up(start=warmup_start, end=warmup_end, refresh_history=refresh_history)
            if warmup
            else None
        )
        cycles: list[SnapshotWorkerCycleReport] = []
        while not self._stop_event.is_set() and (max_cycles is None or len(cycles) < max_cycles):
            cycles.append(self.run_once())
            if max_cycles is not None and len(cycles) >= max_cycles:
                break
            if self.interval_seconds > 0:
                self._stop_event.wait(self.interval_seconds)
        completed_at = datetime.now()
        return SnapshotWorkerRunReport(
            sleeve_id=self.sleeve_id,
            universe_id=self.universe.id,
            source=self.source,
            cycles_requested=max_cycles,
            cycles_completed=len(cycles),
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            warmup=warmup_report,
            cycles=tuple(cycles),
        )

    def start(
        self,
        *,
        max_cycles: int | None = None,
        warmup: bool = True,
        warmup_start: datetime | None = None,
        warmup_end: datetime | None = None,
        refresh_history: bool = False,
        daemon: bool = True,
    ) -> Thread:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("BackgroundSnapshotWorker is already running.")
        self._thread = Thread(
            target=self.run,
            kwargs={
                "max_cycles": max_cycles,
                "warmup": warmup,
                "warmup_start": warmup_start,
                "warmup_end": warmup_end,
                "refresh_history": refresh_history,
            },
            daemon=daemon,
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop_event.set()

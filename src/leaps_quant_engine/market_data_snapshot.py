from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
import time
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorSnapshotStore, SnapshotQualityReport


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MarketDataCollectionFailure:
    symbol_key: str
    message: str


@dataclass(frozen=True, slots=True)
class MarketDataCollectionReport:
    requested_symbol_count: int
    collected_symbol_count: int
    failed_symbol_count: int
    started_at: datetime
    completed_at: datetime
    elapsed_ms: float
    failures: tuple[MarketDataCollectionFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketDataCollectionResult:
    snapshot: "MarketDataSnapshot"
    report: MarketDataCollectionReport


@dataclass(frozen=True, slots=True)
class MarketDataSnapshot:
    snapshot_id: str
    time: datetime
    bars: Mapping[str, Bar]
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", MappingProxyType(dict(self.bars)))

    @classmethod
    def from_bars(
        cls,
        bars: Mapping[str, Bar],
        *,
        source: str,
        snapshot_id: str | None = None,
        time: datetime | None = None,
    ) -> "MarketDataSnapshot":
        return cls(
            snapshot_id=snapshot_id or f"market-data-{uuid4()}",
            time=time or max((bar.time for bar in bars.values()), default=datetime.now()),
            bars=bars,
            source=source,
        )

    def as_data_slice(self) -> DataSlice:
        return DataSlice(time=self.time, bars=dict(self.bars))


@dataclass(slots=True)
class MarketDataSnapshotEngine:
    provider: MarketDataProvider
    indicator_engine: IndicatorEngine
    stores_by_sleeve: dict[str, IndicatorSnapshotStore] = field(default_factory=dict)
    source: str = "provider"

    def collect_once(self, symbols: list[Symbol] | None = None) -> MarketDataSnapshot:
        target_symbols = symbols or self.indicator_engine.active_symbols()
        logger.info(
            "market_data_snapshot.collect.start",
            extra={"source": self.source, "requested_symbol_count": len(target_symbols), "best_effort": False},
        )
        started = time.perf_counter()
        bars: dict[str, Bar] = {}
        for symbol in target_symbols:
            bar = self.provider.get_latest_bar(symbol)
            bars[bar.symbol.key] = bar
        snapshot = MarketDataSnapshot.from_bars(bars, source=self.source)
        logger.info(
            "market_data_snapshot.collect.complete",
            extra={
                "source": self.source,
                "snapshot_id": snapshot.snapshot_id,
                "collected_symbol_count": len(bars),
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "best_effort": False,
            },
        )
        return snapshot

    def collect_once_best_effort(
        self,
        symbols: list[Symbol] | None = None,
        *,
        min_success: int | None = None,
    ) -> MarketDataCollectionResult:
        target_symbols = symbols or self.indicator_engine.active_symbols()
        started_at = datetime.now()
        started = time.perf_counter()
        logger.info(
            "market_data_snapshot.collect.start",
            extra={
                "source": self.source,
                "requested_symbol_count": len(target_symbols),
                "min_success": min_success,
                "best_effort": True,
            },
        )
        bars: dict[str, Bar] = {}
        failures: list[MarketDataCollectionFailure] = []
        for symbol in target_symbols:
            try:
                bar = self.provider.get_latest_bar(symbol)
            except Exception as exc:  # noqa: BLE001 - collection reports provider failures explicitly.
                failures.append(MarketDataCollectionFailure(symbol_key=symbol.key, message=str(exc)))
                logger.warning(
                    "market_data_snapshot.collect.symbol_failed",
                    extra={"source": self.source, "symbol": symbol.key, "error": str(exc)},
                )
                continue
            bars[bar.symbol.key] = bar
        completed_at = datetime.now()
        report = MarketDataCollectionReport(
            requested_symbol_count=len(target_symbols),
            collected_symbol_count=len(bars),
            failed_symbol_count=len(failures),
            started_at=started_at,
            completed_at=completed_at,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failures=tuple(failures),
        )
        if min_success is not None and len(bars) < min_success:
            logger.error(
                "market_data_snapshot.collect.min_success_failed",
                extra={
                    "source": self.source,
                    "collected_symbol_count": len(bars),
                    "min_success": min_success,
                    "failed_symbol_count": len(failures),
                    "elapsed_ms": report.elapsed_ms,
                },
            )
            raise RuntimeError(f"Collected {len(bars)} bars, below min_success={min_success}.")
        snapshot = MarketDataSnapshot.from_bars(bars, source=self.source, time=completed_at)
        logger.info(
            "market_data_snapshot.collect.complete",
            extra={
                "source": self.source,
                "snapshot_id": snapshot.snapshot_id,
                "collected_symbol_count": len(bars),
                "failed_symbol_count": len(failures),
                "elapsed_ms": report.elapsed_ms,
                "best_effort": True,
            },
        )
        return MarketDataCollectionResult(
            snapshot=snapshot,
            report=report,
        )

    def update_indicators(
        self,
        snapshot: MarketDataSnapshot,
        *,
        sleeve_ids: list[str] | None = None,
        universe_id_by_sleeve: Mapping[str, str] | None = None,
        quality_report_by_sleeve: Mapping[str, SnapshotQualityReport] | None = None,
        publish_active: bool = True,
    ) -> dict[str, IndicatorSnapshot]:
        started = time.perf_counter()
        logger.info(
            "indicator_snapshot.update.start",
            extra={
                "source": snapshot.source,
                "market_snapshot_id": snapshot.snapshot_id,
                "bar_count": len(snapshot.bars),
                "publish_active": publish_active,
            },
        )
        self.indicator_engine.on_data(snapshot.as_data_slice())
        target_sleeves = sleeve_ids or sorted(self.indicator_engine.registries_by_sleeve)
        indicator_snapshots: dict[str, IndicatorSnapshot] = {}
        for sleeve_id in target_sleeves:
            indicator_snapshot = self.indicator_engine.snapshot(
                sleeve_id,
                universe_id=(universe_id_by_sleeve or {}).get(sleeve_id),
                source_snapshot_id=snapshot.snapshot_id,
                as_of=snapshot.time,
                quality_report=(quality_report_by_sleeve or {}).get(sleeve_id),
            )
            store = self.stores_by_sleeve.setdefault(sleeve_id, IndicatorSnapshotStore())
            if publish_active:
                store.publish_active(indicator_snapshot)
            else:
                store.publish_pending(indicator_snapshot)
            indicator_snapshots[sleeve_id] = indicator_snapshot
            logger.info(
                "indicator_snapshot.publish",
                extra={
                    "sleeve_id": sleeve_id,
                    "universe_id": indicator_snapshot.universe_id,
                    "market_snapshot_id": snapshot.snapshot_id,
                    "indicator_snapshot_id": indicator_snapshot.snapshot_id,
                    "symbol_count": len(indicator_snapshot.symbols),
                    "publish_active": publish_active,
                    "quality_status": (
                        indicator_snapshot.quality_report.status.value
                        if indicator_snapshot.quality_report is not None
                        else None
                    ),
                },
            )
        logger.info(
            "indicator_snapshot.update.complete",
            extra={
                "source": snapshot.source,
                "market_snapshot_id": snapshot.snapshot_id,
                "sleeve_count": len(indicator_snapshots),
                "elapsed_ms": (time.perf_counter() - started) * 1000,
            },
        )
        return indicator_snapshots

    def run_once(
        self,
        *,
        symbols: list[Symbol] | None = None,
        sleeve_ids: list[str] | None = None,
        universe_id_by_sleeve: Mapping[str, str] | None = None,
        quality_report_by_sleeve: Mapping[str, SnapshotQualityReport] | None = None,
        publish_active: bool = True,
    ) -> tuple[MarketDataSnapshot, dict[str, IndicatorSnapshot]]:
        snapshot = self.collect_once(symbols)
        indicator_snapshots = self.update_indicators(
            snapshot,
            sleeve_ids=sleeve_ids,
            universe_id_by_sleeve=universe_id_by_sleeve,
            quality_report_by_sleeve=quality_report_by_sleeve,
            publish_active=publish_active,
        )
        return snapshot, indicator_snapshots

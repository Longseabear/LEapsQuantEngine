from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import logging
from pathlib import Path
import time
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from leaps_quant_engine.indicators import IndicatorEngine, IndicatorUpdateReport
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorSnapshotStore, SnapshotQualityReport


logger = logging.getLogger(__name__)


QUOTE_SNAPSHOT_LANE = "quote"
MINUTE_SNAPSHOT_LANE = "minute"
DAILY_CONFIRMED_SNAPSHOT_LANE = "daily_confirmed"
UNKNOWN_SNAPSHOT_LANE = "unknown"


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
    lane: str = UNKNOWN_SNAPSHOT_LANE

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", MappingProxyType(dict(self.bars)))
        object.__setattr__(self, "lane", normalize_snapshot_lane(self.lane))

    @classmethod
    def from_bars(
        cls,
        bars: Mapping[str, Bar],
        *,
        source: str,
        snapshot_id: str | None = None,
        time: datetime | None = None,
        lane: str | None = None,
    ) -> "MarketDataSnapshot":
        resolved_lane = normalize_snapshot_lane(lane) if lane is not None else infer_snapshot_lane(bars.values())
        return cls(
            snapshot_id=snapshot_id or f"market-data-{uuid4()}",
            time=time or max((bar.time for bar in bars.values()), default=datetime.now()),
            bars=bars,
            source=source,
            lane=resolved_lane,
        )

    def as_data_slice(self) -> DataSlice:
        return DataSlice(time=self.time, bars=dict(self.bars), resolution=_slice_resolution(self.bars.values()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "time": self.time.isoformat(),
            "source": self.source,
            "lane": self.lane,
            "bars": [_bar_to_dict(bar) for bar in self.bars.values()],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MarketDataSnapshot":
        bars = {
            bar.symbol.key: bar
            for bar in (_bar_from_dict(item) for item in payload.get("bars", []) if isinstance(item, Mapping))
        }
        return cls(
            snapshot_id=str(payload.get("snapshot_id") or ""),
            time=_parse_datetime(payload.get("time")) or datetime.now(),
            source=str(payload.get("source") or "unknown"),
            bars=bars,
            lane=str(payload.get("lane") or infer_snapshot_lane(bars.values())),
        )


@dataclass(frozen=True, slots=True)
class MarketDataSnapshotRecord:
    snapshot: MarketDataSnapshot
    quality_by_sleeve: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot": self.snapshot.to_dict(),
            "quality_by_sleeve": {key: dict(value) for key, value in self.quality_by_sleeve.items()},
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MarketDataSnapshotRecord":
        return cls(
            snapshot=MarketDataSnapshot.from_dict(dict(payload.get("snapshot") or {})),
            quality_by_sleeve={
                str(key): dict(value)
                for key, value in dict(payload.get("quality_by_sleeve") or {}).items()
                if isinstance(value, Mapping)
            },
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class FileMarketDataSnapshotStore:
    path: Path

    def append(
        self,
        snapshot: MarketDataSnapshot,
        *,
        quality_by_sleeve: Mapping[str, SnapshotQualityReport] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> MarketDataSnapshotRecord:
        record = MarketDataSnapshotRecord(
            snapshot=snapshot,
            quality_by_sleeve={
                sleeve_id: report.to_dict()
                for sleeve_id, report in dict(quality_by_sleeve or {}).items()
            },
            metadata=dict(metadata or {}),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return record

    def latest(self, *, lane: str | None = None) -> MarketDataSnapshotRecord | None:
        target_lane = normalize_snapshot_lane(lane) if lane is not None else None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            record = MarketDataSnapshotRecord.from_dict(payload)
            if target_lane is None or record.snapshot.lane == target_lane:
                return record
        return None

    def entries(self, *, limit: int = 100, lane: str | None = None) -> tuple[MarketDataSnapshotRecord, ...]:
        target_lane = normalize_snapshot_lane(lane) if lane is not None else None
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        records: list[MarketDataSnapshotRecord] = []
        for line in lines[-max(int(limit), 0):]:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                record = MarketDataSnapshotRecord.from_dict(payload)
                if target_lane is None or record.snapshot.lane == target_lane:
                    records.append(record)
        return tuple(records)


@dataclass(slots=True)
class MarketDataSnapshotEngine:
    provider: MarketDataProvider
    indicator_engine: IndicatorEngine
    stores_by_sleeve: dict[str, IndicatorSnapshotStore] = field(default_factory=dict)
    source: str = "provider"
    snapshot_store: FileMarketDataSnapshotStore | None = None
    last_indicator_update_report: IndicatorUpdateReport = field(default_factory=IndicatorUpdateReport, init=False)
    last_indicator_update_report_by_sleeve: dict[str, IndicatorUpdateReport] = field(default_factory=dict, init=False)

    def collect_once(self, symbols: list[Symbol] | None = None) -> MarketDataSnapshot:
        target_symbols = symbols or self.indicator_engine.active_symbols()
        logger.info(
            "market_data_snapshot.collect.start",
            extra={"source": self.source, "requested_symbol_count": len(target_symbols), "best_effort": False},
        )
        started = time.perf_counter()
        bars: dict[str, Bar] = {}
        for symbol in target_symbols:
            bar = _as_live_bar(self.provider.get_latest_bar(symbol))
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
            bar = _as_live_bar(bar)
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
        self.last_indicator_update_report_by_sleeve = self.indicator_engine.on_data_by_sleeve(snapshot.as_data_slice())
        report = IndicatorUpdateReport()
        for sleeve_report in self.last_indicator_update_report_by_sleeve.values():
            report = report.combine(sleeve_report)
        self.last_indicator_update_report = report
        target_sleeves = sleeve_ids or sorted(self.indicator_engine.registries_by_sleeve)
        indicator_snapshots: dict[str, IndicatorSnapshot] = {}
        for sleeve_id in target_sleeves:
            indicator_snapshot = self.indicator_engine.snapshot(
                sleeve_id,
                universe_id=(universe_id_by_sleeve or {}).get(sleeve_id),
                source_snapshot_id=snapshot.snapshot_id,
                as_of=snapshot.time,
                quality_report=(quality_report_by_sleeve or {}).get(sleeve_id),
                lane=snapshot.lane,
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
                    "snapshot_lane": snapshot.lane,
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
        self._append_snapshot(snapshot, quality_by_sleeve=quality_report_by_sleeve)
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

    def _append_snapshot(
        self,
        snapshot: MarketDataSnapshot,
        quality_by_sleeve: Mapping[str, SnapshotQualityReport] | None = None,
    ) -> None:
        if self.snapshot_store is None:
            return
        self.snapshot_store.append(snapshot, quality_by_sleeve=quality_by_sleeve)


def _as_live_bar(bar: Bar) -> Bar:
    if bar.resolution not in {"", "any", "unknown"}:
        return bar
    return replace(bar, resolution="live")


def _slice_resolution(bars: object) -> str:
    resolutions = {getattr(bar, "resolution", "any") for bar in bars}
    if len(resolutions) == 1:
        return next(iter(resolutions), "any")
    return "mixed"


def normalize_snapshot_lane(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "any", "*"}:
        return UNKNOWN_SNAPSHOT_LANE
    if text in {"quote", "live", "tick", "second"}:
        return QUOTE_SNAPSHOT_LANE
    if text in {"minute", "intraday"}:
        return MINUTE_SNAPSHOT_LANE
    if text in {"daily", "daily_confirmed", "confirmed_daily"}:
        return DAILY_CONFIRMED_SNAPSHOT_LANE
    if text == UNKNOWN_SNAPSHOT_LANE:
        return UNKNOWN_SNAPSHOT_LANE
    return text


def infer_snapshot_lane(bars: object) -> str:
    lanes = {
        normalize_snapshot_lane(getattr(bar, "resolution", None))
        for bar in bars
    }
    lanes.discard(UNKNOWN_SNAPSHOT_LANE)
    if not lanes:
        return UNKNOWN_SNAPSHOT_LANE
    if len(lanes) > 1:
        raise ValueError(f"MarketDataSnapshot cannot mix resolution lanes: {sorted(lanes)}")
    return next(iter(lanes))


def _bar_to_dict(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": {"ticker": bar.symbol.ticker, "market": bar.symbol.market},
        "time": bar.time.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "resolution": bar.resolution,
        "metadata": dict(bar.metadata),
    }


def _bar_from_dict(payload: Mapping[str, Any]) -> Bar:
    symbol_payload = payload.get("symbol")
    if isinstance(symbol_payload, Mapping):
        symbol = Symbol(str(symbol_payload.get("ticker") or ""), str(symbol_payload.get("market") or "KR"))
    else:
        market, _, ticker = str(payload.get("symbol_key") or "").partition(":")
        symbol = Symbol(ticker or str(payload.get("ticker") or ""), market or str(payload.get("market") or "KR"))
    return Bar(
        symbol=symbol,
        time=_parse_datetime(payload.get("time")) or datetime.now(),
        open=float(payload.get("open") or 0.0),
        high=float(payload.get("high") or 0.0),
        low=float(payload.get("low") or 0.0),
        close=float(payload.get("close") or 0.0),
        volume=int(float(payload.get("volume") or 0)),
        resolution=str(payload.get("resolution") or "any"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None

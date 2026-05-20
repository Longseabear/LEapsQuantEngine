from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from types import MappingProxyType
from typing import Any, Mapping

from leaps_quant_engine.snapshots.freshness import SnapshotQualityReport


@dataclass(frozen=True, slots=True)
class IndicatorValue:
    name: str
    value: float | None
    is_ready: bool
    samples: int
    time: datetime | None = None
    resolution: str = "any"


@dataclass(frozen=True, slots=True)
class IndicatorSnapshot:
    snapshot_id: str
    sleeve_id: str
    universe_id: str | None
    as_of: datetime
    created_at: datetime
    symbols: tuple[str, ...]
    values: Mapping[str, Mapping[str, IndicatorValue]]
    source_snapshot_id: str | None = None
    quality_report: SnapshotQualityReport | None = None
    symbol_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    lane: str = "unknown"

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "values", _freeze_values(self.values))
        object.__setattr__(self, "symbol_metadata", _freeze_metadata(self.symbol_metadata))
        object.__setattr__(self, "lane", _normalize_snapshot_lane(self.lane))

    def value(self, symbol_key: str, name: str, *, ready_only: bool = True) -> float | None:
        indicator_value = self.values.get(symbol_key, {}).get(name)
        if indicator_value is None:
            return None
        if ready_only and not indicator_value.is_ready:
            return None
        return indicator_value.value

    def ready_values(self, symbol_key: str) -> dict[str, float]:
        return {
            name: indicator_value.value
            for name, indicator_value in self.values.get(symbol_key, {}).items()
            if indicator_value.is_ready and indicator_value.value is not None
        }

    def metadata(self, symbol_key: str) -> Mapping[str, Any]:
        return self.symbol_metadata.get(symbol_key, MappingProxyType({}))

    def metadata_value(self, symbol_key: str, name: str, default: Any = None) -> Any:
        return self.symbol_metadata.get(symbol_key, {}).get(name, default)


@dataclass(slots=True)
class IndicatorSnapshotStore:
    _active_by_lane: dict[str, IndicatorSnapshot] = field(default_factory=dict)
    _pending_by_lane: dict[str, IndicatorSnapshot] = field(default_factory=dict)
    _active_lane: str | None = None
    _pending_lane: str | None = None
    _lock: Lock = field(default_factory=Lock)

    def publish_active(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        with self._lock:
            lane = _normalize_snapshot_lane(snapshot.lane)
            self._active_by_lane[lane] = snapshot
            self._pending_by_lane.pop(lane, None)
            self._active_lane = lane
            if self._pending_lane == lane:
                self._pending_lane = None
            return snapshot

    def publish_pending(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        with self._lock:
            lane = _normalize_snapshot_lane(snapshot.lane)
            self._pending_by_lane[lane] = snapshot
            self._pending_lane = lane
            return snapshot

    def swap(self, lane: str | None = None) -> IndicatorSnapshot | None:
        with self._lock:
            target_lane = _normalize_snapshot_lane(lane) if lane is not None else self._pending_lane
            if target_lane is not None and target_lane in self._pending_by_lane:
                self._active_by_lane[target_lane] = self._pending_by_lane.pop(target_lane)
                self._active_lane = target_lane
                if self._pending_lane == target_lane:
                    self._pending_lane = None
            if target_lane is None:
                return None
            return self._active_by_lane.get(target_lane)

    def active(self, lane: str | None = None) -> IndicatorSnapshot | None:
        with self._lock:
            target_lane = _normalize_snapshot_lane(lane) if lane is not None else self._active_lane
            if target_lane is None:
                return None
            return self._active_by_lane.get(target_lane)

    def pending(self, lane: str | None = None) -> IndicatorSnapshot | None:
        with self._lock:
            target_lane = _normalize_snapshot_lane(lane) if lane is not None else self._pending_lane
            if target_lane is None:
                return None
            return self._pending_by_lane.get(target_lane)

    def active_lanes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._active_by_lane))


def _freeze_values(
    values: Mapping[str, Mapping[str, IndicatorValue]],
) -> Mapping[str, Mapping[str, IndicatorValue]]:
    frozen_symbols = {
        symbol_key: MappingProxyType(dict(indicator_values))
        for symbol_key, indicator_values in values.items()
    }
    return MappingProxyType(frozen_symbols)


def _freeze_metadata(
    metadata: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Mapping[str, Any]]:
    frozen_symbols = {
        symbol_key: MappingProxyType(dict(symbol_metadata))
        for symbol_key, symbol_metadata in metadata.items()
    }
    return MappingProxyType(frozen_symbols)


def _normalize_snapshot_lane(value: str | None) -> str:
    text = str(value or "").strip().lower()
    if text in {"", "any", "*"}:
        return "unknown"
    if text in {"live", "quote", "tick", "second"}:
        return "quote"
    if text in {"minute", "intraday"}:
        return "minute"
    if text in {"daily", "daily_confirmed", "confirmed_daily"}:
        return "daily_confirmed"
    return text

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

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "values", _freeze_values(self.values))
        object.__setattr__(self, "symbol_metadata", _freeze_metadata(self.symbol_metadata))

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
    _active: IndicatorSnapshot | None = None
    _pending: IndicatorSnapshot | None = None
    _lock: Lock = field(default_factory=Lock)

    def publish_active(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        with self._lock:
            self._active = snapshot
            self._pending = None
            return snapshot

    def publish_pending(self, snapshot: IndicatorSnapshot) -> IndicatorSnapshot:
        with self._lock:
            self._pending = snapshot
            return snapshot

    def swap(self) -> IndicatorSnapshot | None:
        with self._lock:
            if self._pending is not None:
                self._active = self._pending
                self._pending = None
            return self._active

    def active(self) -> IndicatorSnapshot | None:
        with self._lock:
            return self._active

    def pending(self) -> IndicatorSnapshot | None:
        with self._lock:
            return self._pending


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

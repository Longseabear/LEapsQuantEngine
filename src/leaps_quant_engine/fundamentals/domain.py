from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from leaps_quant_engine.models import Symbol


@dataclass(frozen=True, slots=True)
class FundamentalValue:
    name: str
    value: float
    as_of: datetime
    reported_at: datetime | None = None
    effective_at: datetime | None = None
    source: str = ""
    stale_after: datetime | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _normalize_name(self.name))
        object.__setattr__(self, "value", float(self.value))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def is_available(self, as_of: datetime) -> bool:
        if self.as_of > as_of:
            return False
        return self.stale_after is None or self.stale_after >= as_of

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "as_of": self.as_of.isoformat(),
            "reported_at": self.reported_at.isoformat() if self.reported_at else None,
            "effective_at": self.effective_at.isoformat() if self.effective_at else None,
            "source": self.source,
            "stale_after": self.stale_after.isoformat() if self.stale_after else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FundamentalSnapshot:
    snapshot_id: str
    sleeve_id: str
    universe_id: str | None
    as_of: datetime
    created_at: datetime
    symbols: tuple[str, ...]
    values: Mapping[str, Mapping[str, FundamentalValue]]
    source_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "values", _freeze_values(self.values))

    def value(self, symbol: Symbol | str, name: str) -> float | None:
        item = self.fundamental_value(symbol, name)
        return item.value if item is not None else None

    def fundamental_value(self, symbol: Symbol | str, name: str) -> FundamentalValue | None:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.values.get(symbol_key, {}).get(_normalize_name(name))

    def values_for(self, symbol: Symbol | str) -> dict[str, float]:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        return {
            name: item.value
            for name, item in self.values.get(symbol_key, {}).items()
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "as_of": self.as_of.isoformat(),
            "created_at": self.created_at.isoformat(),
            "symbols": list(self.symbols),
            "source_snapshot_id": self.source_snapshot_id,
            "values": {
                symbol_key: {
                    name: item.to_dict()
                    for name, item in symbol_values.items()
                }
                for symbol_key, symbol_values in self.values.items()
            },
        }


@dataclass(slots=True)
class PointInTimeFundamentalStore:
    _values_by_symbol_name: dict[tuple[str, str], list[FundamentalValue]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def add(
        self,
        symbol: Symbol | str,
        name: str,
        value: float,
        *,
        as_of: datetime,
        reported_at: datetime | None = None,
        effective_at: datetime | None = None,
        source: str = "",
        stale_after: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> FundamentalValue:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        item = FundamentalValue(
            name=name,
            value=value,
            as_of=as_of,
            reported_at=reported_at,
            effective_at=effective_at,
            source=source,
            stale_after=stale_after,
            metadata=dict(metadata or {}),
        )
        key = (symbol_key, item.name)
        with self._lock:
            values = self._values_by_symbol_name.setdefault(key, [])
            values.append(item)
            values.sort(key=lambda candidate: (candidate.as_of, candidate.source, candidate.value))
        return item

    def latest(self, symbol: Symbol | str, name: str, *, as_of: datetime) -> FundamentalValue | None:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        key = (symbol_key, _normalize_name(name))
        with self._lock:
            candidates = tuple(self._values_by_symbol_name.get(key, ()))
        for item in reversed(candidates):
            if item.is_available(as_of):
                return item
        return None

    def snapshot(
        self,
        *,
        sleeve_id: str,
        universe_id: str | None,
        symbols: tuple[Symbol, ...] | list[Symbol],
        as_of: datetime,
        names: tuple[str, ...] | list[str] | None = None,
        source_snapshot_id: str | None = None,
        created_at: datetime | None = None,
    ) -> FundamentalSnapshot:
        normalized_names = tuple(_normalize_name(name) for name in names) if names is not None else None
        values: dict[str, dict[str, FundamentalValue]] = {}
        for symbol in symbols:
            symbol_values: dict[str, FundamentalValue] = {}
            for name in normalized_names or self._names_for_symbol(symbol):
                item = self.latest(symbol, name, as_of=as_of)
                if item is not None:
                    symbol_values[item.name] = item
            values[symbol.key] = symbol_values
        return FundamentalSnapshot(
            snapshot_id=f"fundamentals-{uuid4()}",
            sleeve_id=sleeve_id,
            universe_id=universe_id,
            as_of=as_of,
            created_at=created_at or datetime.now(tz=as_of.tzinfo),
            symbols=tuple(symbol.key for symbol in symbols),
            values=values,
            source_snapshot_id=source_snapshot_id,
        )

    def _names_for_symbol(self, symbol: Symbol) -> tuple[str, ...]:
        with self._lock:
            names = [
                name
                for symbol_key, name in self._values_by_symbol_name
                if symbol_key == symbol.key
            ]
        return tuple(sorted(set(names)))


def _normalize_name(name: str) -> str:
    return str(name or "").strip().lower()


def _freeze_values(
    values: Mapping[str, Mapping[str, FundamentalValue]],
) -> Mapping[str, Mapping[str, FundamentalValue]]:
    frozen_symbols = {
        symbol_key: MappingProxyType(dict(symbol_values))
        for symbol_key, symbol_values in values.items()
    }
    return MappingProxyType(frozen_symbols)

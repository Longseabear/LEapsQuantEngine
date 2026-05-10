from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.fundamentals import FundamentalSnapshot, FundamentalValue
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, SnapshotQualityReport


class InsightDirection(str, Enum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class InsightType(str, Enum):
    PRICE = "price"
    VOLATILITY = "volatility"


@dataclass(frozen=True, slots=True)
class Insight:
    sleeve_id: str
    symbol: Symbol
    direction: InsightDirection
    generated_at: datetime
    source_snapshot_id: str | None
    alpha_id: str
    alpha_version: str
    insight_type: InsightType = InsightType.PRICE
    expires_at: datetime | None = None
    magnitude: float | None = None
    confidence: float = 1.0
    weight: float | None = None
    score: float | None = None
    group_id: str | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    insight_id: str = field(default_factory=lambda: f"insight-{uuid4()}")

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1.")
        if self.weight is not None and not -1.0 <= self.weight <= 1.0:
            raise ValueError("weight must be between -1 and 1 when set.")
        if self.expires_at is not None and self.expires_at < self.generated_at:
            raise ValueError("expires_at cannot be earlier than generated_at.")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def symbol_key(self) -> str:
        return self.symbol.key

    @property
    def source_model(self) -> str:
        return self.alpha_id

    def is_expired(self, as_of: datetime) -> bool:
        return self.expires_at is not None and self.expires_at < as_of

    def is_active(self, as_of: datetime) -> bool:
        return not self.is_expired(as_of)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "insight_id": self.insight_id,
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol.key,
            "type": self.insight_type.value,
            "direction": self.direction.value,
            "generated_at": self.generated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source_snapshot_id": self.source_snapshot_id,
            "alpha_id": self.alpha_id,
            "alpha_version": self.alpha_version,
            "magnitude": self.magnitude,
            "confidence": self.confidence,
            "weight": self.weight,
            "score": self.score,
            "group_id": self.group_id,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }
        return payload


@dataclass(frozen=True, slots=True)
class InsightBatch:
    sleeve_id: str
    universe_id: str | None
    source_snapshot_id: str | None
    generated_at: datetime
    alpha_ids: tuple[str, ...]
    insights: tuple[Insight, ...]
    batch_id: str = field(default_factory=lambda: f"insights-{uuid4()}")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "source_snapshot_id": self.source_snapshot_id,
            "generated_at": self.generated_at.isoformat(),
            "alpha_ids": list(self.alpha_ids),
            "insight_count": len(self.insights),
            "insights": [insight.to_dict() for insight in self.insights],
            "metadata": dict(self.metadata),
        }

    @property
    def insight_count(self) -> int:
        return len(self.insights)


@dataclass(frozen=True, slots=True)
class SnapshotContext:
    sleeve_id: str
    universe_id: str | None
    indicator_snapshot: IndicatorSnapshot
    as_of: datetime
    quality_report: SnapshotQualityReport | None = None
    fundamental_snapshot: FundamentalSnapshot | None = None
    input_symbol_keys: tuple[str, ...] | None = None

    @classmethod
    def from_indicator_snapshot(
        cls,
        snapshot: IndicatorSnapshot,
        *,
        fundamental_snapshot: FundamentalSnapshot | None = None,
    ) -> "SnapshotContext":
        if fundamental_snapshot is not None and fundamental_snapshot.as_of > snapshot.as_of:
            raise ValueError("fundamental_snapshot.as_of cannot be later than indicator_snapshot.as_of.")
        return cls(
            sleeve_id=snapshot.sleeve_id,
            universe_id=snapshot.universe_id,
            indicator_snapshot=snapshot,
            as_of=snapshot.as_of,
            quality_report=snapshot.quality_report,
            fundamental_snapshot=fundamental_snapshot,
        )

    @property
    def source_snapshot_id(self) -> str | None:
        return self.indicator_snapshot.source_snapshot_id

    @property
    def fundamental_snapshot_id(self) -> str | None:
        return self.fundamental_snapshot.snapshot_id if self.fundamental_snapshot is not None else None

    @property
    def symbol_keys(self) -> tuple[str, ...]:
        if self.input_symbol_keys is not None:
            return self.input_symbol_keys
        return self.indicator_snapshot.symbols

    @property
    def available_symbol_keys(self) -> tuple[str, ...]:
        return self.indicator_snapshot.symbols

    def with_input_symbols(self, symbols: Iterable[Symbol | str]) -> "SnapshotContext":
        return replace(self, input_symbol_keys=_dedupe_symbol_keys(symbols))

    def value(self, symbol: Symbol | str, name: str, *, ready_only: bool = True) -> float | None:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.indicator_snapshot.value(symbol_key, name, ready_only=ready_only)

    def ready_values(self, symbol: Symbol | str) -> dict[str, float]:
        symbol_key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.indicator_snapshot.ready_values(symbol_key)

    def fundamental(self, symbol: Symbol | str, name: str) -> float | None:
        if self.fundamental_snapshot is None:
            return None
        return self.fundamental_snapshot.value(symbol, name)

    def fundamental_value(self, symbol: Symbol | str, name: str) -> FundamentalValue | None:
        if self.fundamental_snapshot is None:
            return None
        return self.fundamental_snapshot.fundamental_value(symbol, name)

    def fundamental_values(self, symbol: Symbol | str) -> dict[str, float]:
        if self.fundamental_snapshot is None:
            return {}
        return self.fundamental_snapshot.values_for(symbol)

    def symbol(self, symbol_key: str) -> Symbol:
        market, ticker = symbol_key.split(":", 1)
        return Symbol(ticker=ticker, market=market)

    @property
    def allows_new_entries(self) -> bool:
        return self.quality_report is None or self.quality_report.allows_new_entries


class AlphaModel(Protocol):
    alpha_id: str
    version: str
    evaluation_cadence: str

    def generate(self, context: SnapshotContext) -> list[Insight] | tuple[Insight, ...]:
        """Generate alpha insights from an immutable indicator snapshot."""


def _dedupe_symbol_keys(symbols: Iterable[Symbol | str]) -> tuple[str, ...]:
    keys: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        key = symbol.key if isinstance(symbol, Symbol) else symbol
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return tuple(keys)

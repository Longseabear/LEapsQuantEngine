from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping

from leaps_quant_engine.alpha.domain import Insight, InsightBatch
from leaps_quant_engine.models import Symbol


class InsightState(str, Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class InsightRecord:
    insight: Insight
    state: InsightState
    state_updated_at: datetime
    state_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "insight": self.insight.to_dict(),
            "state": self.state.value,
            "state_updated_at": self.state_updated_at.isoformat(),
            "state_reason": self.state_reason,
        }


@dataclass(frozen=True, slots=True)
class InsightManagerUpdate:
    added: tuple[Insight, ...] = ()
    expired: tuple[Insight, ...] = ()
    cancelled: tuple[Insight, ...] = ()
    superseded: tuple[Insight, ...] = ()

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def expired_count(self) -> int:
        return len(self.expired)

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled)

    @property
    def superseded_count(self) -> int:
        return len(self.superseded)

    def combine(self, other: "InsightManagerUpdate") -> "InsightManagerUpdate":
        return InsightManagerUpdate(
            added=(*self.added, *other.added),
            expired=(*self.expired, *other.expired),
            cancelled=(*self.cancelled, *other.cancelled),
            superseded=(*self.superseded, *other.superseded),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "added_count": self.added_count,
            "expired_count": self.expired_count,
            "cancelled_count": self.cancelled_count,
            "superseded_count": self.superseded_count,
            "added": [insight.to_dict() for insight in self.added],
            "expired": [insight.to_dict() for insight in self.expired],
            "cancelled": [insight.to_dict() for insight in self.cancelled],
            "superseded": [insight.to_dict() for insight in self.superseded],
        }


@dataclass(slots=True)
class InsightManager:
    _records_by_id: dict[str, InsightRecord] = field(default_factory=dict)
    _active_ids: set[str] = field(default_factory=set)
    _active_ids_by_key: dict[tuple[str, str, str, str], set[str]] = field(default_factory=dict)
    _tracked_symbols_by_sleeve: dict[str, dict[str, Symbol]] = field(default_factory=dict)

    def ingest(self, batch: InsightBatch, *, as_of: datetime | None = None) -> InsightManagerUpdate:
        now = as_of or batch.generated_at
        update = self.expire(now)
        added: list[Insight] = []
        expired_on_ingest: list[Insight] = []
        superseded: list[Insight] = []
        for insight in batch.insights:
            superseded.extend(self._supersede_matching(insight, as_of=now))
            state = InsightState.EXPIRED if insight.is_expired(now) else InsightState.ACTIVE
            self._records_by_id[insight.insight_id] = InsightRecord(
                insight=insight,
                state=state,
                state_updated_at=now,
                state_reason="ingested" if state is InsightState.ACTIVE else "expired_on_ingest",
            )
            self._track_symbol(insight)
            if state is InsightState.ACTIVE:
                self._activate(insight)
                added.append(insight)
            else:
                expired_on_ingest.append(insight)
        return update.combine(
            InsightManagerUpdate(
                added=tuple(added),
                expired=tuple(expired_on_ingest),
                superseded=tuple(superseded),
            )
        )

    def expire(self, as_of: datetime) -> InsightManagerUpdate:
        expired: list[Insight] = []
        for insight_id in list(self._active_ids):
            record = self._records_by_id[insight_id]
            if not record.insight.is_expired(as_of):
                continue
            expired.append(self._deactivate(insight_id, record, InsightState.EXPIRED, as_of, "expired"))
        return InsightManagerUpdate(expired=tuple(expired))

    def active(self, as_of: datetime | None = None, *, sleeve_id: str | None = None) -> tuple[Insight, ...]:
        if as_of is not None:
            self.expire(as_of)
        return tuple(
            record.insight
            for record in sorted(
                (self._records_by_id[insight_id] for insight_id in self._active_ids),
                key=lambda item: (item.insight.generated_at, item.insight.insight_id),
            )
            if sleeve_id is None or record.insight.sleeve_id == sleeve_id
        )

    def cancel_symbol(
        self,
        sleeve_id: str,
        symbol: Symbol,
        *,
        as_of: datetime,
        alpha_id: str | None = None,
    ) -> InsightManagerUpdate:
        return self.cancel_where(
            lambda insight: insight.sleeve_id == sleeve_id
            and insight.symbol_key == symbol.key
            and (alpha_id is None or insight.alpha_id == alpha_id),
            as_of=as_of,
            reason="cancel_symbol",
        )

    def cancel_where(
        self,
        predicate: Callable[[Insight], bool],
        *,
        as_of: datetime,
        reason: str = "cancelled",
    ) -> InsightManagerUpdate:
        cancelled: list[Insight] = []
        for insight_id in list(self._active_ids):
            record = self._records_by_id[insight_id]
            if not predicate(record.insight):
                continue
            cancelled.append(self._deactivate(insight_id, record, InsightState.CANCELLED, as_of, reason))
        return InsightManagerUpdate(cancelled=tuple(cancelled))

    def tracked_symbols(self, sleeve_id: str | None = None) -> tuple[Symbol, ...]:
        if sleeve_id is not None:
            return tuple(self._tracked_symbols_by_sleeve.get(sleeve_id, {}).values())
        result: list[Symbol] = []
        seen: set[str] = set()
        for symbols in self._tracked_symbols_by_sleeve.values():
            for symbol_key, symbol in symbols.items():
                if symbol_key in seen:
                    continue
                seen.add(symbol_key)
                result.append(symbol)
        return tuple(result)

    def latest_by_symbol(self, sleeve_id: str | None = None) -> Mapping[str, Insight]:
        latest: dict[str, Insight] = {}
        for record in self._records_by_id.values():
            insight = record.insight
            if sleeve_id is not None and insight.sleeve_id != sleeve_id:
                continue
            previous = latest.get(insight.symbol_key)
            if previous is None or insight.generated_at > previous.generated_at:
                latest[insight.symbol_key] = insight
        return MappingProxyType(latest)

    def state_for(self, insight_id: str) -> InsightState | None:
        record = self._records_by_id.get(insight_id)
        return record.state if record is not None else None

    def records(self) -> tuple[InsightRecord, ...]:
        return tuple(self._records_by_id.values())

    def _supersede_matching(self, insight: Insight, *, as_of: datetime) -> tuple[Insight, ...]:
        superseded: list[Insight] = []
        for insight_id in list(self._active_ids_by_key.get(_active_key(insight), ())):
            record = self._records_by_id[insight_id]
            superseded.append(
                self._deactivate(
                    insight_id,
                    record,
                    InsightState.SUPERSEDED,
                    as_of,
                    f"superseded_by:{insight.insight_id}",
                )
            )
        return tuple(superseded)

    def _activate(self, insight: Insight) -> None:
        self._active_ids.add(insight.insight_id)
        self._active_ids_by_key.setdefault(_active_key(insight), set()).add(insight.insight_id)

    def _deactivate(
        self,
        insight_id: str,
        record: InsightRecord,
        state: InsightState,
        as_of: datetime,
        reason: str,
    ) -> Insight:
        self._active_ids.discard(insight_id)
        key = _active_key(record.insight)
        ids = self._active_ids_by_key.get(key)
        if ids is not None:
            ids.discard(insight_id)
            if not ids:
                del self._active_ids_by_key[key]
        self._records_by_id[insight_id] = InsightRecord(
            insight=record.insight,
            state=state,
            state_updated_at=as_of,
            state_reason=reason,
        )
        return record.insight

    def _track_symbol(self, insight: Insight) -> None:
        self._tracked_symbols_by_sleeve.setdefault(insight.sleeve_id, {}).setdefault(
            insight.symbol_key,
            insight.symbol,
        )


def _active_key(insight: Insight) -> tuple[str, str, str, str]:
    return (
        insight.sleeve_id,
        insight.symbol_key,
        insight.alpha_id,
        insight.insight_type.value,
    )

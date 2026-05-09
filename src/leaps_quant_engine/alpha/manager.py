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

    def ingest(self, batch: InsightBatch, *, as_of: datetime | None = None) -> InsightManagerUpdate:
        now = as_of or batch.generated_at
        update = self.expire(now)
        added: list[Insight] = []
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
            if state is InsightState.ACTIVE:
                added.append(insight)
            else:
                update = update.combine(InsightManagerUpdate(expired=(insight,)))
        return update.combine(InsightManagerUpdate(added=tuple(added), superseded=tuple(superseded)))

    def expire(self, as_of: datetime) -> InsightManagerUpdate:
        expired: list[Insight] = []
        for insight_id, record in list(self._records_by_id.items()):
            if record.state is not InsightState.ACTIVE:
                continue
            if not record.insight.is_expired(as_of):
                continue
            self._records_by_id[insight_id] = InsightRecord(
                insight=record.insight,
                state=InsightState.EXPIRED,
                state_updated_at=as_of,
                state_reason="expired",
            )
            expired.append(record.insight)
        return InsightManagerUpdate(expired=tuple(expired))

    def active(self, as_of: datetime | None = None, *, sleeve_id: str | None = None) -> tuple[Insight, ...]:
        if as_of is not None:
            self.expire(as_of)
        return tuple(
            record.insight
            for record in sorted(
                self._records_by_id.values(),
                key=lambda item: (item.insight.generated_at, item.insight.insight_id),
            )
            if record.state is InsightState.ACTIVE
            and (sleeve_id is None or record.insight.sleeve_id == sleeve_id)
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
        for insight_id, record in list(self._records_by_id.items()):
            if record.state is not InsightState.ACTIVE or not predicate(record.insight):
                continue
            self._records_by_id[insight_id] = InsightRecord(
                insight=record.insight,
                state=InsightState.CANCELLED,
                state_updated_at=as_of,
                state_reason=reason,
            )
            cancelled.append(record.insight)
        return InsightManagerUpdate(cancelled=tuple(cancelled))

    def tracked_symbols(self, sleeve_id: str | None = None) -> tuple[Symbol, ...]:
        result: list[Symbol] = []
        seen: set[str] = set()
        for record in self._records_by_id.values():
            insight = record.insight
            if sleeve_id is not None and insight.sleeve_id != sleeve_id:
                continue
            if insight.symbol_key in seen:
                continue
            seen.add(insight.symbol_key)
            result.append(insight.symbol)
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
        for insight_id, record in list(self._records_by_id.items()):
            previous = record.insight
            if record.state is not InsightState.ACTIVE:
                continue
            if (
                previous.sleeve_id != insight.sleeve_id
                or previous.symbol_key != insight.symbol_key
                or previous.alpha_id != insight.alpha_id
                or previous.insight_type is not insight.insight_type
            ):
                continue
            self._records_by_id[insight_id] = InsightRecord(
                insight=previous,
                state=InsightState.SUPERSEDED,
                state_updated_at=as_of,
                state_reason=f"superseded_by:{insight.insight_id}",
            )
            superseded.append(previous)
        return tuple(superseded)


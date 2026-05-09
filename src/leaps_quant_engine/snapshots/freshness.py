from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SnapshotQualityStatus(str, Enum):
    FRESH = "fresh"
    DEGRADED = "degraded"
    STALE = "stale"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SnapshotQualityReport:
    status: SnapshotQualityStatus
    complete_ratio: float
    age_seconds: float
    collection_seconds: float
    requested_symbol_count: int
    collected_symbol_count: int
    failed_symbol_count: int
    reasons: tuple[str, ...] = ()

    @property
    def allows_new_entries(self) -> bool:
        return self.status == SnapshotQualityStatus.FRESH

    @property
    def allows_risk_checks(self) -> bool:
        return self.status in {
            SnapshotQualityStatus.FRESH,
            SnapshotQualityStatus.DEGRADED,
            SnapshotQualityStatus.STALE,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "complete_ratio": self.complete_ratio,
            "age_seconds": self.age_seconds,
            "collection_seconds": self.collection_seconds,
            "requested_symbol_count": self.requested_symbol_count,
            "collected_symbol_count": self.collected_symbol_count,
            "failed_symbol_count": self.failed_symbol_count,
            "reasons": list(self.reasons),
            "allows_new_entries": self.allows_new_entries,
            "allows_risk_checks": self.allows_risk_checks,
        }


@dataclass(frozen=True, slots=True)
class SnapshotFreshnessPolicy:
    max_age_seconds: float = 90.0
    min_complete_ratio: float = 0.95
    degraded_complete_ratio: float = 0.75
    max_collection_seconds: float = 60.0

    def evaluate(
        self,
        *,
        requested_symbol_count: int,
        collected_symbol_count: int,
        failed_symbol_count: int,
        completed_at: datetime,
        elapsed_ms: float,
        now: datetime | None = None,
    ) -> SnapshotQualityReport:
        now = now or datetime.now(tz=completed_at.tzinfo)
        requested = max(requested_symbol_count, 0)
        collected = max(collected_symbol_count, 0)
        complete_ratio = 0.0 if requested == 0 else collected / requested
        age_seconds = max((now - completed_at).total_seconds(), 0.0)
        collection_seconds = max(elapsed_ms / 1000.0, 0.0)

        reasons: list[str] = []
        status = SnapshotQualityStatus.FRESH

        if requested == 0:
            reasons.append("no_symbols_requested")
            status = SnapshotQualityStatus.INVALID
        elif collected == 0:
            reasons.append("no_symbols_collected")
            status = SnapshotQualityStatus.INVALID
        elif complete_ratio < self.degraded_complete_ratio:
            reasons.append("complete_ratio_below_degraded_threshold")
            status = SnapshotQualityStatus.INVALID
        elif age_seconds > self.max_age_seconds:
            reasons.append("snapshot_too_old")
            status = SnapshotQualityStatus.STALE
        else:
            if complete_ratio < self.min_complete_ratio:
                reasons.append("complete_ratio_below_fresh_threshold")
                status = SnapshotQualityStatus.DEGRADED
            if collection_seconds > self.max_collection_seconds:
                reasons.append("collection_too_slow")
                status = SnapshotQualityStatus.DEGRADED

        return SnapshotQualityReport(
            status=status,
            complete_ratio=complete_ratio,
            age_seconds=age_seconds,
            collection_seconds=collection_seconds,
            requested_symbol_count=requested_symbol_count,
            collected_symbol_count=collected_symbol_count,
            failed_symbol_count=failed_symbol_count,
            reasons=tuple(reasons),
        )

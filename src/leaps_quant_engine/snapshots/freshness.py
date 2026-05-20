from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
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
    max_age_seconds: float = 10.0
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


@dataclass(frozen=True, slots=True)
class FreshnessThresholds:
    quote_max_age_seconds: float = 10.0
    extended_quote_max_age_seconds: float = 30.0
    account_max_age_seconds: float = 60.0
    open_ticket_status_max_age_seconds: float = 10.0
    closed_ticket_status_max_age_seconds: float = 60.0
    confirmed_minute_lag_tolerance_bars: int = 1


@dataclass(frozen=True, slots=True)
class DataFreshnessReport:
    status: SnapshotQualityStatus
    as_of: datetime | None
    expected_as_of: datetime | date | None
    age_seconds: float | None
    reasons: tuple[str, ...] = ()

    @property
    def is_fresh(self) -> bool:
        return self.status is SnapshotQualityStatus.FRESH

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "as_of": self.as_of.isoformat() if isinstance(self.as_of, datetime) else None,
            "expected_as_of": self.expected_as_of.isoformat() if self.expected_as_of is not None else None,
            "age_seconds": self.age_seconds,
            "reasons": list(self.reasons),
        }


def evaluate_quote_freshness(
    *,
    as_of: datetime | None,
    now: datetime,
    session_phase: str = "regular",
    thresholds: FreshnessThresholds = FreshnessThresholds(),
) -> DataFreshnessReport:
    max_age = (
        thresholds.quote_max_age_seconds
        if str(session_phase or "regular").lower() in {"regular", "regular_market"}
        else thresholds.extended_quote_max_age_seconds
    )
    return _evaluate_age_freshness(
        as_of=as_of,
        now=now,
        max_age_seconds=max_age,
        stale_reason="quote_too_old",
        missing_reason="quote_missing",
    )


def evaluate_account_freshness(
    *,
    as_of: datetime | None,
    now: datetime,
    thresholds: FreshnessThresholds = FreshnessThresholds(),
) -> DataFreshnessReport:
    return _evaluate_age_freshness(
        as_of=as_of,
        now=now,
        max_age_seconds=thresholds.account_max_age_seconds,
        stale_reason="account_snapshot_too_old",
        missing_reason="account_snapshot_missing",
    )


def evaluate_order_status_freshness(
    *,
    as_of: datetime | None,
    now: datetime,
    has_open_ticket: bool,
    thresholds: FreshnessThresholds = FreshnessThresholds(),
) -> DataFreshnessReport:
    max_age = (
        thresholds.open_ticket_status_max_age_seconds
        if has_open_ticket
        else thresholds.closed_ticket_status_max_age_seconds
    )
    return _evaluate_age_freshness(
        as_of=as_of,
        now=now,
        max_age_seconds=max_age,
        stale_reason="order_status_too_old",
        missing_reason="order_status_missing",
    )


def evaluate_confirmed_minute_freshness(
    *,
    bar_time: datetime | None,
    now: datetime,
    interval_minutes: int = 1,
    thresholds: FreshnessThresholds = FreshnessThresholds(),
) -> DataFreshnessReport:
    expected = expected_confirmed_minute_time(now, interval_minutes=interval_minutes)
    if bar_time is None:
        return DataFreshnessReport(
            status=SnapshotQualityStatus.INVALID,
            as_of=None,
            expected_as_of=expected,
            age_seconds=None,
            reasons=("confirmed_minute_missing",),
        )
    age_seconds = max((now - bar_time).total_seconds(), 0.0)
    interval = max(int(interval_minutes), 1)
    lag_bars = max(int((expected - bar_time).total_seconds() // (interval * 60)), 0)
    if bar_time >= expected:
        status = SnapshotQualityStatus.FRESH
        reasons: tuple[str, ...] = ()
    elif lag_bars <= max(thresholds.confirmed_minute_lag_tolerance_bars, 0):
        status = SnapshotQualityStatus.DEGRADED
        reasons = ("confirmed_minute_lagging_within_tolerance",)
    else:
        status = SnapshotQualityStatus.STALE
        reasons = ("confirmed_minute_too_old",)
    return DataFreshnessReport(
        status=status,
        as_of=bar_time,
        expected_as_of=expected,
        age_seconds=age_seconds,
        reasons=reasons,
    )


def evaluate_daily_confirmed_freshness(
    *,
    bar_date: date | datetime | None,
    now: datetime,
    market_close_time: dt_time = dt_time(15, 30),
    trading_days: tuple[date, ...] = (),
) -> DataFreshnessReport:
    expected = expected_confirmed_daily_date(now, market_close_time=market_close_time, trading_days=trading_days)
    normalized = bar_date.date() if isinstance(bar_date, datetime) else bar_date
    if normalized is None:
        return DataFreshnessReport(
            status=SnapshotQualityStatus.INVALID,
            as_of=None,
            expected_as_of=expected,
            age_seconds=None,
            reasons=("daily_confirmed_missing",),
        )
    if normalized >= expected:
        return DataFreshnessReport(
            status=SnapshotQualityStatus.FRESH,
            as_of=datetime.combine(normalized, dt_time.min, tzinfo=now.tzinfo),
            expected_as_of=expected,
            age_seconds=None,
            reasons=(),
        )
    return DataFreshnessReport(
        status=SnapshotQualityStatus.STALE,
        as_of=datetime.combine(normalized, dt_time.min, tzinfo=now.tzinfo),
        expected_as_of=expected,
        age_seconds=None,
        reasons=("daily_confirmed_too_old",),
    )


def expected_confirmed_minute_time(now: datetime, *, interval_minutes: int = 1) -> datetime:
    interval = max(int(interval_minutes), 1)
    minute_bucket = (now.minute // interval) * interval
    current_bucket = now.replace(minute=minute_bucket, second=0, microsecond=0)
    return current_bucket - timedelta(minutes=interval)


def expected_confirmed_daily_date(
    now: datetime,
    *,
    market_close_time: dt_time = dt_time(15, 30),
    trading_days: tuple[date, ...] = (),
) -> date:
    candidate = now.date() if now.time() >= market_close_time else now.date() - timedelta(days=1)
    if trading_days:
        eligible = [day for day in trading_days if day <= candidate]
        if eligible:
            return max(eligible)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _evaluate_age_freshness(
    *,
    as_of: datetime | None,
    now: datetime,
    max_age_seconds: float,
    stale_reason: str,
    missing_reason: str,
) -> DataFreshnessReport:
    if as_of is None:
        return DataFreshnessReport(
            status=SnapshotQualityStatus.INVALID,
            as_of=None,
            expected_as_of=None,
            age_seconds=None,
            reasons=(missing_reason,),
        )
    age_seconds = max((now - as_of).total_seconds(), 0.0)
    if age_seconds <= max_age_seconds:
        return DataFreshnessReport(
            status=SnapshotQualityStatus.FRESH,
            as_of=as_of,
            expected_as_of=None,
            age_seconds=age_seconds,
            reasons=(),
        )
    return DataFreshnessReport(
        status=SnapshotQualityStatus.STALE,
        as_of=as_of,
        expected_as_of=None,
        age_seconds=age_seconds,
        reasons=(stale_reason,),
    )

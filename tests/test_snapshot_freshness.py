from datetime import datetime, timedelta

from leaps_quant_engine.snapshots import (
    FreshnessThresholds,
    SnapshotFreshnessPolicy,
    SnapshotQualityStatus,
    evaluate_account_freshness,
    evaluate_confirmed_minute_freshness,
    evaluate_daily_confirmed_freshness,
    evaluate_order_status_freshness,
    evaluate_quote_freshness,
    expected_confirmed_minute_time,
)


def test_snapshot_freshness_policy_marks_complete_recent_snapshot_fresh():
    policy = SnapshotFreshnessPolicy()
    completed_at = datetime(2026, 5, 8, 10, 0)

    report = policy.evaluate(
        requested_symbol_count=200,
        collected_symbol_count=198,
        failed_symbol_count=2,
        completed_at=completed_at,
        elapsed_ms=45_000,
        now=completed_at + timedelta(seconds=5),
    )

    assert report.status == SnapshotQualityStatus.FRESH
    assert report.complete_ratio == 0.99
    assert report.allows_new_entries is True
    assert report.allows_risk_checks is True
    assert report.reasons == ()


def test_snapshot_freshness_policy_marks_partial_snapshot_degraded():
    policy = SnapshotFreshnessPolicy()
    completed_at = datetime(2026, 5, 8, 10, 0)

    report = policy.evaluate(
        requested_symbol_count=200,
        collected_symbol_count=180,
        failed_symbol_count=20,
        completed_at=completed_at,
        elapsed_ms=10_000,
        now=completed_at,
    )

    assert report.status == SnapshotQualityStatus.DEGRADED
    assert report.reasons == ("complete_ratio_below_fresh_threshold",)
    assert report.allows_new_entries is False
    assert report.allows_risk_checks is True


def test_snapshot_freshness_policy_marks_old_snapshot_stale():
    policy = SnapshotFreshnessPolicy(max_age_seconds=60)
    completed_at = datetime(2026, 5, 8, 10, 0)

    report = policy.evaluate(
        requested_symbol_count=200,
        collected_symbol_count=200,
        failed_symbol_count=0,
        completed_at=completed_at,
        elapsed_ms=10_000,
        now=completed_at + timedelta(seconds=61),
    )

    assert report.status == SnapshotQualityStatus.STALE
    assert report.reasons == ("snapshot_too_old",)


def test_snapshot_freshness_policy_marks_low_completion_invalid():
    policy = SnapshotFreshnessPolicy()
    completed_at = datetime(2026, 5, 8, 10, 0)

    report = policy.evaluate(
        requested_symbol_count=200,
        collected_symbol_count=80,
        failed_symbol_count=120,
        completed_at=completed_at,
        elapsed_ms=10_000,
        now=completed_at,
    )

    assert report.status == SnapshotQualityStatus.INVALID
    assert report.reasons == ("complete_ratio_below_degraded_threshold",)
    assert report.allows_new_entries is False
    assert report.allows_risk_checks is False


def test_snapshot_quality_report_to_dict_uses_string_status():
    policy = SnapshotFreshnessPolicy()
    completed_at = datetime(2026, 5, 8, 10, 0)

    payload = policy.evaluate(
        requested_symbol_count=1,
        collected_symbol_count=1,
        failed_symbol_count=0,
        completed_at=completed_at,
        elapsed_ms=100,
        now=completed_at,
    ).to_dict()

    assert payload["status"] == "fresh"
    assert payload["allows_new_entries"] is True


def test_quote_freshness_uses_regular_and_extended_thresholds():
    now = datetime(2026, 5, 21, 9, 31, 12)

    regular = evaluate_quote_freshness(as_of=now - timedelta(seconds=11), now=now, session_phase="regular")
    extended = evaluate_quote_freshness(as_of=now - timedelta(seconds=29), now=now, session_phase="pre_market")

    assert regular.status == SnapshotQualityStatus.STALE
    assert regular.reasons == ("quote_too_old",)
    assert extended.status == SnapshotQualityStatus.FRESH


def test_confirmed_minute_freshness_uses_expected_previous_completed_bar():
    now = datetime(2026, 5, 21, 9, 31, 20)

    fresh = evaluate_confirmed_minute_freshness(bar_time=datetime(2026, 5, 21, 9, 30), now=now)
    degraded = evaluate_confirmed_minute_freshness(bar_time=datetime(2026, 5, 21, 9, 29), now=now)
    stale = evaluate_confirmed_minute_freshness(
        bar_time=datetime(2026, 5, 21, 9, 29),
        now=now,
        thresholds=FreshnessThresholds(confirmed_minute_lag_tolerance_bars=0),
    )

    assert expected_confirmed_minute_time(now) == datetime(2026, 5, 21, 9, 30)
    assert fresh.status == SnapshotQualityStatus.FRESH
    assert degraded.status == SnapshotQualityStatus.DEGRADED
    assert degraded.reasons == ("confirmed_minute_lagging_within_tolerance",)
    assert stale.status == SnapshotQualityStatus.STALE


def test_daily_confirmed_freshness_expects_previous_business_day_before_close():
    now = datetime(2026, 5, 21, 9, 30)

    fresh = evaluate_daily_confirmed_freshness(bar_date=datetime(2026, 5, 20), now=now)
    stale = evaluate_daily_confirmed_freshness(bar_date=datetime(2026, 5, 19), now=now)

    assert fresh.status == SnapshotQualityStatus.FRESH
    assert fresh.expected_as_of.isoformat() == "2026-05-20"
    assert stale.status == SnapshotQualityStatus.STALE


def test_account_and_order_status_freshness_have_different_thresholds():
    now = datetime(2026, 5, 21, 9, 31)

    account = evaluate_account_freshness(as_of=now - timedelta(seconds=59), now=now)
    open_order = evaluate_order_status_freshness(as_of=now - timedelta(seconds=11), now=now, has_open_ticket=True)
    closed_order = evaluate_order_status_freshness(as_of=now - timedelta(seconds=59), now=now, has_open_ticket=False)

    assert account.status == SnapshotQualityStatus.FRESH
    assert open_order.status == SnapshotQualityStatus.STALE
    assert closed_order.status == SnapshotQualityStatus.FRESH

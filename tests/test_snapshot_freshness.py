from datetime import datetime, timedelta

from leaps_quant_engine.snapshots import SnapshotFreshnessPolicy, SnapshotQualityStatus


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

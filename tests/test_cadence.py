from datetime import datetime

from leaps_quant_engine.cadence import cadence_due, normalize_cadence, within_time_window


def test_once_per_day_legacy_cadence_still_runs_first_cycle_of_day():
    assert cadence_due("once_per_day", datetime(2026, 5, 18, 8, 0), None) is True
    assert cadence_due(
        "once_per_day",
        datetime(2026, 5, 18, 9, 0),
        datetime(2026, 5, 18, 8, 0),
    ) is False
    assert cadence_due(
        "once_per_day",
        datetime(2026, 5, 19, 8, 0),
        datetime(2026, 5, 18, 8, 0),
    ) is True


def test_daily_at_fires_only_after_configured_time_once_per_trading_day():
    cadence = "daily_at 08:50 Asia/Seoul"

    assert cadence_due(cadence, datetime(2026, 5, 18, 8, 49), None) is False
    assert cadence_due(cadence, datetime(2026, 5, 18, 8, 50), None) is True
    assert cadence_due(
        cadence,
        datetime(2026, 5, 18, 9, 0),
        datetime(2026, 5, 18, 8, 50),
    ) is False
    assert cadence_due(
        cadence,
        datetime(2026, 5, 19, 8, 50),
        datetime(2026, 5, 18, 8, 50),
    ) is True


def test_week_start_at_fires_once_on_first_cycle_after_weekly_time():
    cadence = "week_start_at 08:55 Asia/Seoul"

    assert cadence_due(cadence, datetime(2026, 5, 18, 8, 54), None) is False
    assert cadence_due(cadence, datetime(2026, 5, 18, 8, 55), None) is True
    assert cadence_due(
        cadence,
        datetime(2026, 5, 19, 8, 55),
        datetime(2026, 5, 18, 8, 55),
    ) is False
    assert cadence_due(
        cadence,
        datetime(2026, 5, 25, 8, 55),
        datetime(2026, 5, 18, 8, 55),
    ) is True


def test_weekly_at_supports_explicit_weekday():
    cadence = "weekly_at Wednesday 08:55 Asia/Seoul"

    assert cadence_due(cadence, datetime(2026, 5, 19, 9, 0), None) is False
    assert cadence_due(cadence, datetime(2026, 5, 20, 8, 55), None) is True
    assert cadence_due(
        cadence,
        datetime(2026, 5, 20, 9, 0),
        datetime(2026, 5, 20, 8, 55),
    ) is False


def test_every_n_days_waits_for_date_interval():
    cadence = "every_3_days"

    assert normalize_cadence("3d") == cadence
    assert cadence_due(cadence, datetime(2026, 5, 18, 9, 0), None) is True
    assert cadence_due(
        cadence,
        datetime(2026, 5, 20, 9, 0),
        datetime(2026, 5, 18, 9, 0),
    ) is False
    assert cadence_due(
        cadence,
        datetime(2026, 5, 21, 9, 0),
        datetime(2026, 5, 18, 9, 0),
    ) is True


def test_time_window_includes_bounds_and_supports_timezone_suffix():
    assert within_time_window(datetime(2026, 5, 18, 9, 5), "09:05-14:50 Asia/Seoul") is True
    assert within_time_window(datetime(2026, 5, 18, 14, 50), "09:05-14:50 Asia/Seoul") is True
    assert within_time_window(datetime(2026, 5, 18, 14, 51), "09:05-14:50 Asia/Seoul") is False


def test_normalize_preserves_schedule_text_shape():
    assert normalize_cadence("every-trading-day-at 08:50 Asia/Seoul") == "every_trading_day_at 08:50 asia/seoul"

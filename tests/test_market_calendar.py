from datetime import datetime
from zoneinfo import ZoneInfo

from leaps_quant_engine.market_calendar import calendar_for_market_scope, session_report_for_market_scope


def test_krx_calendar_closes_weekends_and_reports_next_open():
    report = session_report_for_market_scope(
        "domestic",
        now=datetime(2026, 5, 16, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")),
    )

    assert report.is_trading_day is False
    assert report.session.session_phase == "closed"
    assert report.session.is_orderable is False
    assert report.next_open is not None
    assert report.next_open.date().isoformat() == "2026-05-18"


def test_krx_calendar_uses_optional_holiday_file(tmp_path):
    holidays = tmp_path / "krx_holidays.json"
    holidays.write_text('{"holidays": ["2026-05-15"]}', encoding="utf-8")

    report = calendar_for_market_scope("domestic", holiday_file=holidays).session_at(
        datetime(2026, 5, 15, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    )

    assert report.holiday is True
    assert report.is_trading_day is False
    assert report.session.session_phase == "closed"


def test_calendar_missing_holiday_file_is_degraded_but_weekend_only_usable(tmp_path):
    report = calendar_for_market_scope("domestic", holiday_file=tmp_path / "missing.json").session_at(
        datetime(2026, 5, 14, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    )

    assert report.quality.status == "degraded"
    assert report.session.session_phase == "regular_continuous"


def test_us_calendar_handles_pre_market_with_dst_timezone():
    report = session_report_for_market_scope(
        "overseas",
        now=datetime(2026, 5, 15, 8, 0, tzinfo=ZoneInfo("America/New_York")),
    )

    assert report.is_trading_day is True
    assert report.session.session_phase == "pre_market"
    assert report.session.is_orderable is True

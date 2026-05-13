from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from leaps_quant_engine.market_rules import (
    DOMESTIC_ORDER_SESSION_TO_DIVISION,
    synthetic_domestic_market_session,
    synthetic_us_market_session,
)


def test_domestic_after_hours_sessions_are_orderable_and_map_to_kis_divisions():
    pre_open = synthetic_domestic_market_session(datetime(2026, 5, 13, 8, 35, tzinfo=ZoneInfo("Asia/Seoul")))
    after_close = synthetic_domestic_market_session(datetime(2026, 5, 13, 15, 45, tzinfo=ZoneInfo("Asia/Seoul")))
    single_price = synthetic_domestic_market_session(datetime(2026, 5, 13, 16, 1, tzinfo=ZoneInfo("Asia/Seoul")))

    assert pre_open.is_orderable is True
    assert after_close.is_orderable is True
    assert single_price.is_orderable is True
    assert DOMESTIC_ORDER_SESSION_TO_DIVISION[pre_open.session_phase] == "05"
    assert DOMESTIC_ORDER_SESSION_TO_DIVISION[after_close.session_phase] == "06"
    assert DOMESTIC_ORDER_SESSION_TO_DIVISION[single_price.session_phase] == "07"


def test_us_pre_and_after_market_are_orderable_but_not_regular_open():
    pre_market = synthetic_us_market_session(datetime(2026, 5, 13, 11, 0, tzinfo=timezone.utc))
    regular = synthetic_us_market_session(datetime(2026, 5, 13, 14, 0, tzinfo=timezone.utc))
    after_market = synthetic_us_market_session(datetime(2026, 5, 13, 21, 0, tzinfo=timezone.utc))

    assert pre_market.session_phase == "pre_market"
    assert pre_market.is_orderable is True
    assert pre_market.is_regular_market_open is False
    assert regular.session_phase == "regular_continuous"
    assert regular.is_regular_market_open is True
    assert after_market.session_phase == "after_market"
    assert after_market.is_orderable is True
    assert after_market.is_regular_market_open is False

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from leaps_quant_engine.market_rules import MarketSession, synthetic_domestic_market_session, synthetic_us_market_session


_DEFAULT_HOLIDAY_FILES = {
    "domestic": Path("configs/market-calendars/krx_holidays.json"),
    "overseas": Path("configs/market-calendars/us_holidays.json"),
}


@dataclass(frozen=True, slots=True)
class MarketCalendarQuality:
    status: str = "ok"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "warnings": list(self.warnings)}


@dataclass(frozen=True, slots=True)
class MarketCalendarReport:
    market_scope: str
    market: str
    now: datetime
    local_now: datetime
    is_trading_day: bool
    holiday: bool
    session: MarketSession
    next_open: datetime | None = None
    next_close: datetime | None = None
    quality: MarketCalendarQuality = field(default_factory=MarketCalendarQuality)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_scope": self.market_scope,
            "market": self.market,
            "now": self.now.isoformat(),
            "local_now": self.local_now.isoformat(),
            "is_trading_day": self.is_trading_day,
            "holiday": self.holiday,
            "session": self.session.to_dict(),
            "next_open": self.next_open.isoformat() if self.next_open else None,
            "next_close": self.next_close.isoformat() if self.next_close else None,
            "quality": self.quality.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ExchangeCalendar:
    market_scope: str
    market: str
    timezone: str
    holidays: frozenset[date] = field(default_factory=frozenset)
    quality: MarketCalendarQuality = field(default_factory=MarketCalendarQuality)

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def session_at(self, now: datetime | None = None) -> MarketCalendarReport:
        now = now or datetime.now(self.zone)
        local_now = _localize(now, self.zone)
        holiday = local_now.date() in self.holidays
        trading_day = local_now.weekday() < 5 and not holiday
        if not trading_day:
            session = MarketSession(
                market_scope=self.market_scope,
                session_phase="closed",
                is_orderable=False,
                is_regular_market_open=False,
                source=f"calendar:{self.market}",
            )
        elif self.market_scope == "overseas":
            session = synthetic_us_market_session(local_now)
            session = MarketSession(
                market_scope=session.market_scope,
                session_phase=session.session_phase,
                is_orderable=session.is_orderable,
                is_regular_market_open=session.is_regular_market_open,
                source=f"calendar:{self.market}",
            )
        else:
            session = synthetic_domestic_market_session(local_now)
            session = MarketSession(
                market_scope=session.market_scope,
                session_phase=session.session_phase,
                is_orderable=session.is_orderable,
                is_regular_market_open=session.is_regular_market_open,
                source=f"calendar:{self.market}",
            )
        next_open = self._next_session_boundary(local_now, open_boundary=True)
        next_close = self._next_session_boundary(local_now, open_boundary=False)
        return MarketCalendarReport(
            market_scope=self.market_scope,
            market=self.market,
            now=now,
            local_now=local_now,
            is_trading_day=trading_day,
            holiday=holiday,
            session=session,
            next_open=next_open,
            next_close=next_close,
            quality=self.quality,
        )

    def _next_session_boundary(self, local_now: datetime, *, open_boundary: bool) -> datetime | None:
        boundary_time = time(9, 0) if self.market_scope == "domestic" else time(9, 30)
        if not open_boundary:
            boundary_time = time(15, 30) if self.market_scope == "domestic" else time(16, 0)
        for offset in range(0, 14):
            candidate_date = local_now.date() + timedelta(days=offset)
            if candidate_date.weekday() >= 5 or candidate_date in self.holidays:
                continue
            candidate = datetime.combine(candidate_date, boundary_time, tzinfo=self.zone)
            if candidate > local_now:
                return candidate
        return None


def calendar_for_market_scope(
    market_scope: str,
    *,
    holiday_file: str | Path | None = None,
) -> ExchangeCalendar:
    scope = str(market_scope or "domestic").strip().lower()
    market = "US" if scope == "overseas" else "KRX"
    timezone = "America/New_York" if scope == "overseas" else "Asia/Seoul"
    if holiday_file is None:
        holiday_file = _DEFAULT_HOLIDAY_FILES.get(scope)
    holidays, quality = _load_holidays(holiday_file)
    return ExchangeCalendar(
        market_scope=scope,
        market=market,
        timezone=timezone,
        holidays=frozenset(holidays),
        quality=quality,
    )


def session_report_for_market_scope(
    market_scope: str,
    *,
    now: datetime | None = None,
    holiday_file: str | Path | None = None,
) -> MarketCalendarReport:
    return calendar_for_market_scope(market_scope, holiday_file=holiday_file).session_at(now)


def _load_holidays(path: str | Path | None) -> tuple[tuple[date, ...], MarketCalendarQuality]:
    if path is None:
        return (), MarketCalendarQuality()
    candidate = Path(path)
    if not candidate.exists():
        return (), MarketCalendarQuality(
            status="degraded",
            warnings=(f"holiday_file_missing:{candidate}",),
        )
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    raw_items: Iterable[Any]
    if isinstance(payload, dict):
        raw_items = payload.get("holidays", ())
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = ()
    holidays = tuple(_parse_date(item) for item in raw_items)
    return holidays, MarketCalendarQuality()


def _parse_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(str(value))


def _localize(value: datetime, zone: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)

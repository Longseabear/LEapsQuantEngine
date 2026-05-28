from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo


_MINUTE_CADENCE_PATTERN = re.compile(r"^(?:every_)?(\d+)(?:_?m|_?min|_?minute|_?minutes)$")
_DAY_CADENCE_PATTERN = re.compile(r"^(?:every_)?(\d+)(?:_?d|_?day|_?days)$")
_DAILY_AT_PATTERN = re.compile(r"^(?:daily_at|every_trading_day_at)\s+(\d{1,2}:\d{2})(?:\s+(\S+))?$")
_WEEK_START_AT_PATTERN = re.compile(r"^week_start_at\s+(\d{1,2}:\d{2})(?:\s+(\S+))?$")
_WEEKLY_AT_PATTERN = re.compile(r"^weekly_at\s+(\w+)\s+(\d{1,2}:\d{2})(?:\s+(\S+))?$")
_WEEKDAY_BY_NAME = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
}


@dataclass(frozen=True, slots=True)
class ScheduledCadence:
    kind: str
    scheduled_time: time
    timezone: str = "UTC"
    weekday: int | None = None


def normalize_cadence(value: str | None) -> str:
    cadence = str(value or "every_cycle").strip().lower().replace("-", "_")
    if cadence in {"cycle", "every", "always"}:
        return "every_cycle"
    if cadence in {"startup", "startup_only", "once_at_startup"}:
        return "startup_only"
    if cadence in {"day", "daily", "once_daily"}:
        return "once_per_day"
    if cadence in {"month", "monthly", "once_monthly"}:
        return "once_per_month"
    minute_interval = _minute_interval(cadence)
    if minute_interval is not None:
        return f"every_{minute_interval}_minutes"
    day_interval = _day_interval(cadence)
    if day_interval is not None:
        return f"every_{day_interval}_days"
    return cadence or "every_cycle"


def cadence_due(cadence: str | None, as_of: datetime, last_run_at: datetime | None) -> bool:
    normalized = normalize_cadence(cadence)
    scheduled = parse_scheduled_cadence(normalized)
    if scheduled is not None:
        return scheduled_cadence_due(scheduled, as_of, last_run_at)
    if normalized == "every_cycle":
        return True
    if last_run_at is None:
        return True
    if normalized == "startup_only":
        return False
    if normalized in {"once_per_day", "daily"}:
        return as_of.date() != last_run_at.date()
    if normalized in {"once_per_month", "monthly"}:
        return (as_of.year, as_of.month) != (last_run_at.year, last_run_at.month)
    minute_interval = _minute_interval(normalized)
    if minute_interval is not None:
        return as_of - last_run_at >= timedelta(minutes=minute_interval)
    day_interval = _day_interval(normalized)
    if day_interval is not None:
        return as_of.date() >= last_run_at.date() + timedelta(days=day_interval)
    if normalized == "manual":
        return False
    raise ValueError(f"Unsupported cadence: {cadence}")


def parse_scheduled_cadence(cadence: str | None) -> ScheduledCadence | None:
    normalized = normalize_cadence(cadence)
    match = _DAILY_AT_PATTERN.match(normalized)
    if match is not None:
        return ScheduledCadence(
            kind="daily_at",
            scheduled_time=_parse_clock(match.group(1)),
            timezone=match.group(2) or "UTC",
        )
    match = _WEEK_START_AT_PATTERN.match(normalized)
    if match is not None:
        return ScheduledCadence(
            kind="week_start_at",
            scheduled_time=_parse_clock(match.group(1)),
            timezone=match.group(2) or "UTC",
        )
    match = _WEEKLY_AT_PATTERN.match(normalized)
    if match is not None:
        return ScheduledCadence(
            kind="weekly_at",
            scheduled_time=_parse_clock(match.group(2)),
            timezone=match.group(3) or "UTC",
            weekday=_parse_weekday(match.group(1)),
        )
    return None


def scheduled_cadence_due(
    schedule: ScheduledCadence,
    as_of: datetime,
    last_run_at: datetime | None,
) -> bool:
    local_as_of = _localize(as_of, schedule.timezone)
    if local_as_of.weekday() >= 5:
        return False
    scheduled_at = datetime.combine(local_as_of.date(), schedule.scheduled_time, tzinfo=local_as_of.tzinfo)
    if local_as_of < scheduled_at:
        return False
    local_last_run = _localize(last_run_at, schedule.timezone) if last_run_at is not None else None
    if schedule.kind == "daily_at":
        return local_last_run is None or local_last_run.date() != local_as_of.date() or local_last_run < scheduled_at
    if schedule.kind == "week_start_at":
        return local_last_run is None or local_last_run.isocalendar()[:2] != local_as_of.isocalendar()[:2]
    if schedule.kind == "weekly_at":
        if schedule.weekday is None or local_as_of.weekday() != schedule.weekday:
            return False
        return local_last_run is None or local_last_run.isocalendar()[:2] != local_as_of.isocalendar()[:2]
    raise ValueError(f"Unsupported scheduled cadence kind: {schedule.kind}")


def within_time_window(
    as_of: datetime,
    window: str | None,
    *,
    timezone: str = "UTC",
) -> bool:
    if not str(window or "").strip():
        return True
    start, end, window_timezone = parse_time_window(window, default_timezone=timezone)
    local_as_of = _localize(as_of, window_timezone)
    current = local_as_of.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def parse_time_window(window: str, *, default_timezone: str = "UTC") -> tuple[time, time, str]:
    text = str(window or "").strip()
    if not text:
        raise ValueError("time window cannot be empty.")
    parts = text.split()
    bounds = parts[0]
    timezone = parts[1] if len(parts) > 1 else default_timezone
    if "-" not in bounds:
        raise ValueError(f"Time window must be like '09:05-14:50 Asia/Seoul': {window!r}")
    start_text, end_text = bounds.split("-", 1)
    return _parse_clock(start_text), _parse_clock(end_text), timezone


def _minute_interval(cadence: str) -> int | None:
    match = _MINUTE_CADENCE_PATTERN.match(cadence)
    if match is None:
        return None
    interval = int(match.group(1))
    if interval <= 0:
        raise ValueError(f"Minute cadence must be positive: {cadence}")
    return interval


def _day_interval(cadence: str) -> int | None:
    match = _DAY_CADENCE_PATTERN.match(cadence)
    if match is None:
        return None
    interval = int(match.group(1))
    if interval <= 0:
        raise ValueError(f"Day cadence must be positive: {cadence}")
    return interval


def _parse_clock(value: str) -> time:
    text = str(value or "").strip()
    match = re.match(r"^(\d{1,2}):(\d{2})$", text)
    if match is None:
        raise ValueError(f"Time must be HH:MM: {value!r}")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Time must be HH:MM: {value!r}")
    return time(hour, minute)


def _parse_weekday(value: str) -> int:
    text = str(value or "").strip().lower()
    if text.isdigit():
        parsed = int(text)
        if 0 <= parsed <= 4:
            return parsed
    if text in _WEEKDAY_BY_NAME:
        return _WEEKDAY_BY_NAME[text]
    raise ValueError(f"weekly_at weekday must be Monday-Friday or 0-4: {value!r}")


def _localize(value: datetime, timezone: str) -> datetime:
    zone = ZoneInfo(_canonical_timezone(timezone))
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _canonical_timezone(value: str) -> str:
    text = str(value or "UTC").strip()
    mapping = {
        "utc": "UTC",
        "asia/seoul": "Asia/Seoul",
        "america/new_york": "America/New_York",
    }
    return mapping.get(text.lower(), text)

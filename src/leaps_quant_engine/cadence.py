from __future__ import annotations

from datetime import datetime, timedelta
import re


_MINUTE_CADENCE_PATTERN = re.compile(r"^(?:every_)?(\d+)(?:_?m|_?min|_?minute|_?minutes)$")


def normalize_cadence(value: str | None) -> str:
    cadence = str(value or "every_cycle").strip().lower().replace("-", "_")
    if cadence in {"cycle", "every", "always"}:
        return "every_cycle"
    if cadence in {"day", "daily", "once_daily"}:
        return "once_per_day"
    if cadence in {"month", "monthly", "once_monthly"}:
        return "once_per_month"
    minute_interval = _minute_interval(cadence)
    if minute_interval is not None:
        return f"every_{minute_interval}_minutes"
    return cadence or "every_cycle"


def cadence_due(cadence: str | None, as_of: datetime, last_run_at: datetime | None) -> bool:
    normalized = normalize_cadence(cadence)
    if normalized == "every_cycle":
        return True
    if last_run_at is None:
        return True
    if normalized in {"once_per_day", "daily"}:
        return as_of.date() != last_run_at.date()
    if normalized in {"once_per_month", "monthly"}:
        return (as_of.year, as_of.month) != (last_run_at.year, last_run_at.month)
    minute_interval = _minute_interval(normalized)
    if minute_interval is not None:
        return as_of - last_run_at >= timedelta(minutes=minute_interval)
    if normalized == "manual":
        return False
    raise ValueError(f"Unsupported cadence: {cadence}")


def _minute_interval(cadence: str) -> int | None:
    match = _MINUTE_CADENCE_PATTERN.match(cadence)
    if match is None:
        return None
    interval = int(match.group(1))
    if interval <= 0:
        raise ValueError(f"Minute cadence must be positive: {cadence}")
    return interval

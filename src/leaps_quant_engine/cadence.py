from __future__ import annotations

from datetime import datetime


def normalize_cadence(value: str | None) -> str:
    cadence = str(value or "every_cycle").strip().lower()
    if cadence in {"cycle", "every", "always"}:
        return "every_cycle"
    if cadence in {"day", "daily", "once_daily"}:
        return "once_per_day"
    return cadence or "every_cycle"


def cadence_due(cadence: str | None, as_of: datetime, last_run_at: datetime | None) -> bool:
    normalized = normalize_cadence(cadence)
    if normalized == "every_cycle":
        return True
    if last_run_at is None:
        return True
    if normalized in {"once_per_day", "daily"}:
        return as_of.date() != last_run_at.date()
    if normalized == "manual":
        return False
    raise ValueError(f"Unsupported cadence: {cadence}")

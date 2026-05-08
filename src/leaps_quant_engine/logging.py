from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_RESERVED_RECORD_KEYS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "asctime",
    "message",
}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in _RESERVED_RECORD_KEYS:
                continue
            payload[key] = _json_safe(value)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    *,
    level: str | int = "WARNING",
    log_file: str | Path | None = None,
    json_logs: bool = False,
    max_bytes: int = 10_000_000,
    backup_count: int = 5,
) -> None:
    numeric_level = _parse_log_level(level)
    formatter: logging.Formatter
    if json_logs:
        formatter = JsonLogFormatter()
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=max(0, max_bytes),
                backupCount=max(0, backup_count),
                encoding="utf-8",
            )
        )

    for handler in handlers:
        handler.setLevel(numeric_level)
        handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(numeric_level)
    for handler in handlers:
        root_logger.addHandler(handler)

    logging.getLogger("urllib3").setLevel(max(numeric_level, logging.WARNING))


def _parse_log_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = level.strip().upper()
    if normalized.isdigit():
        return int(normalized)
    value = logging.getLevelName(normalized)
    if isinstance(value, int):
        return value
    raise ValueError(f"Unknown log level: {level}")


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)

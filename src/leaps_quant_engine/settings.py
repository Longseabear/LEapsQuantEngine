from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Raised when runtime configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class KISSettings:
    app_key: str
    app_secret: str
    base_url: str = "https://openapi.koreainvestment.com:9443"
    hts_id: str | None = None
    cano: str | None = None
    account_product_code: str | None = None
    mock: bool = False
    rate_limit_per_second: int = 15
    broker_engine_base_url: str = "http://127.0.0.1:8755"
    default_domestic_symbol: str = "005930"
    default_overseas_symbol: str = "AAPL"
    default_overseas_exchange: str = "NAS"


def load_kis_settings(env_file: str | Path = ".env", *, override: bool = False) -> KISSettings:
    env_path = Path(_configured_env_file(env_file))
    if env_path.exists():
        load_dotenv(env_path, override=override)

    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    if not app_key:
        raise ConfigurationError("KIS_APP_KEY is required.")
    if not app_secret:
        raise ConfigurationError("KIS_APP_SECRET is required.")

    return KISSettings(
        app_key=app_key,
        app_secret=app_secret,
        base_url=os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443").strip(),
        hts_id=os.getenv("KIS_HTS_ID", "").strip() or None,
        cano=os.getenv("KIS_CANO", "").strip() or None,
        account_product_code=os.getenv("KIS_ACNT_PRDT_CD", "").strip() or None,
        mock=_parse_bool(os.getenv("KIS_MOCK"), default=False),
        rate_limit_per_second=_parse_positive_int("KIS_API_RATE_LIMIT_PER_SECOND", default=15),
        broker_engine_base_url=os.getenv("BROKER_ENGINE_BASE_URL", "http://127.0.0.1:8755").strip().rstrip("/"),
        default_domestic_symbol=os.getenv("DEFAULT_DOMESTIC_SYMBOL", "005930").strip() or "005930",
        default_overseas_symbol=os.getenv("DEFAULT_OVERSEAS_SYMBOL", "AAPL").strip() or "AAPL",
        default_overseas_exchange=os.getenv("DEFAULT_OVERSEAS_EXCHANGE", "NAS").strip() or "NAS",
    )


def _configured_env_file(default: str | Path) -> str:
    for name in ("LEAPS_ENV_FILE", "STOCKPROGRAM_ENV_FILE", "MARKET_DATA_ENGINE_ENV_FILE"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return str(default)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigurationError(f"Invalid boolean value: {value}")


def _parse_positive_int(name: str, *, default: int) -> int:
    raw = os.getenv(name, str(default)).strip() or str(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return value

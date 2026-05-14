from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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
    market_data_engine_rate_limit_per_second: int = 15
    broker_engine_base_url: str = "http://127.0.0.1:8755"
    market_data_engine_base_url: str = "http://127.0.0.1:8765"
    default_domestic_symbol: str = "005930"
    default_overseas_symbol: str = "AAPL"
    default_overseas_exchange: str = "NAS"


def load_kis_settings(env_file: str | Path = ".env", *, override: bool = False) -> KISSettings:
    _load_configured_env_file(env_file, override=override)

    app_key = os.getenv("KIS_APP_KEY", "").strip()
    app_secret = os.getenv("KIS_APP_SECRET", "").strip()
    cano = os.getenv("KIS_CANO", "").strip()
    account_product_code = os.getenv("KIS_ACNT_PRDT_CD", "").strip()
    if not app_key:
        app_key = os.getenv("KIS_ACCOUNT_DEFAULT_APP_KEY", "").strip()
        app_secret = os.getenv("KIS_ACCOUNT_DEFAULT_APP_SECRET", "").strip() or app_secret
        cano = os.getenv("KIS_ACCOUNT_DEFAULT_CANO", "").strip() or cano
        account_product_code = os.getenv("KIS_ACCOUNT_DEFAULT_ACNT_PRDT_CD", "").strip() or account_product_code

    return _settings_from_env(
        app_key=app_key,
        app_secret=app_secret,
        cano=cano,
        account_product_code=account_product_code,
    )


def load_kis_settings_for_account(
    account_id: str | None,
    *,
    metadata: Mapping[str, object] | None = None,
    env_file: str | Path = ".env",
    override: bool = False,
) -> KISSettings:
    """Load KIS settings with StockProgram-style account-scoped overrides."""
    _load_configured_env_file(env_file, override=override)
    data = dict(metadata or {})
    account_scoped_id = str(data.get("kis_account_id") or account_id or "").strip()
    credential_scoped_id = str(data.get("credential_account_id") or account_scoped_id).strip()
    if not account_scoped_id and not credential_scoped_id:
        return load_kis_settings(env_file, override=override)
    credential_prefix = kis_account_env_prefix(credential_scoped_id or account_scoped_id)
    account_prefix = kis_account_env_prefix(account_scoped_id or credential_scoped_id)
    app_key = _scoped_or_base(f"{credential_prefix}_APP_KEY", os.getenv("KIS_APP_KEY", "").strip())
    app_secret = _scoped_or_base(f"{credential_prefix}_APP_SECRET", os.getenv("KIS_APP_SECRET", "").strip())
    cano = str(data.get("kis_cano") or "").strip() or _scoped_or_base(
        f"{account_prefix}_CANO",
        os.getenv("KIS_CANO", "").strip(),
    )
    account_product_code = (
        str(data.get("kis_acnt_prdt_cd") or data.get("kis_account_product_code") or "").strip()
        or _scoped_or_base(f"{account_prefix}_ACNT_PRDT_CD", os.getenv("KIS_ACNT_PRDT_CD", "").strip())
    )
    return _settings_from_env(
        app_key=app_key,
        app_secret=app_secret,
        base_url=str(data.get("kis_base_url") or os.getenv("KIS_BASE_URL", "")).strip(),
        hts_id=str(data.get("kis_hts_id") or os.getenv("KIS_HTS_ID", "")).strip(),
        cano=cano or None,
        account_product_code=account_product_code or None,
        mock=(
            _parse_bool(str(data.get("kis_mock")), default=_parse_bool(os.getenv("KIS_MOCK"), default=False))
            if "kis_mock" in data
            else None
        ),
    )


def kis_account_env_prefix(account_id: str) -> str:
    normalized = "".join(ch if ch.isalnum() else "_" for ch in account_id.strip().upper())
    compact = "_".join(part for part in normalized.split("_") if part)
    if not compact:
        raise ConfigurationError("account_id must contain at least one letter or number.")
    return f"KIS_ACCOUNT_{compact}"


def _scoped_or_base(name: str, base_value: str) -> str:
    return os.getenv(name, "").strip() or base_value


def _load_configured_env_file(env_file: str | Path, *, override: bool) -> None:
    env_path = Path(_configured_env_file(env_file))
    if env_path.exists():
        load_dotenv(env_path, override=override)


def _configured_env_file(default: str | Path) -> str:
    for name in ("LEAPS_ENV_FILE", "STOCKPROGRAM_ENV_FILE", "MARKET_DATA_ENGINE_ENV_FILE"):
        value = os.getenv(name, "").strip()
        if value:
            return value
    return str(default)


def _settings_from_env(
    *,
    app_key: str,
    app_secret: str,
    cano: str | None,
    account_product_code: str | None,
    base_url: str | None = None,
    hts_id: str | None = None,
    mock: bool | None = None,
) -> KISSettings:
    app_key = app_key.strip()
    app_secret = app_secret.strip()
    if not app_key:
        raise ConfigurationError("KIS_APP_KEY is required.")
    if not app_secret:
        raise ConfigurationError("KIS_APP_SECRET is required.")

    return KISSettings(
        app_key=app_key,
        app_secret=app_secret,
        base_url=(base_url or os.getenv("KIS_BASE_URL", "https://openapi.koreainvestment.com:9443")).strip()
        or "https://openapi.koreainvestment.com:9443",
        hts_id=(hts_id or os.getenv("KIS_HTS_ID", "")).strip() or None,
        cano=(cano or "").strip() or None,
        account_product_code=(account_product_code or "").strip() or None,
        mock=_parse_bool(os.getenv("KIS_MOCK"), default=False) if mock is None else mock,
        rate_limit_per_second=_parse_positive_int("KIS_API_RATE_LIMIT_PER_SECOND", default=15),
        market_data_engine_rate_limit_per_second=_parse_positive_int(
            "MARKET_DATA_ENGINE_RATE_LIMIT_PER_SECOND",
            default=15,
        ),
        broker_engine_base_url=os.getenv("BROKER_ENGINE_BASE_URL", "http://127.0.0.1:8755").strip().rstrip("/"),
        market_data_engine_base_url=os.getenv(
            "MARKET_DATA_ENGINE_BASE_URL",
            "http://127.0.0.1:8765",
        ).strip().rstrip("/"),
        default_domestic_symbol=os.getenv("DEFAULT_DOMESTIC_SYMBOL", "005930").strip() or "005930",
        default_overseas_symbol=os.getenv("DEFAULT_OVERSEAS_SYMBOL", "AAPL").strip() or "AAPL",
        default_overseas_exchange=os.getenv("DEFAULT_OVERSEAS_EXCHANGE", "NAS").strip() or "NAS",
    )


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

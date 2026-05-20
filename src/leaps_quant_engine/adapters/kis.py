from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import json
import logging
from pathlib import Path
from threading import Lock
import time
from typing import Any, Mapping

import requests

from leaps_quant_engine.adapters.kis_direct import KISDirectClient
from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, DataResolution, Symbol
from leaps_quant_engine.settings import KISSettings, load_kis_settings


logger = logging.getLogger(__name__)


class BrokerEngineClientError(RuntimeError):
    """Raised when the local broker-engine bridge cannot serve a request."""


class MarketDataEngineClientError(RuntimeError):
    """Raised when the local market-data-engine bridge cannot serve a request."""


@dataclass(slots=True)
class BrokerEngineClient:
    base_url: str
    session: requests.Session
    rate_limit_per_second: int = 10
    _lock: Lock = field(default_factory=Lock)
    _last_request_at: float = 0.0

    @classmethod
    def from_settings(cls, settings: KISSettings) -> "BrokerEngineClient":
        return cls(
            base_url=settings.broker_engine_base_url.rstrip("/"),
            session=requests.Session(),
            rate_limit_per_second=min(settings.rate_limit_per_second, 10),
        )

    def health_check(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/health", timeout=5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BrokerEngineClientError("broker-engine health returned a non-object payload.")
        return payload

    def call_operation(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.base_url}/broker/call",
                json={"operation": operation, "arguments": arguments or {}},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise BrokerEngineClientError(
                f"Failed to call broker-engine operation '{operation}' at {self.base_url}."
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerEngineClientError(
                f"broker-engine returned non-JSON for operation '{operation}' (HTTP {response.status_code})."
            ) from exc
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise BrokerEngineClientError(f"broker-engine operation '{operation}' failed: {detail}")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise BrokerEngineClientError(f"broker-engine operation '{operation}' returned an unexpected payload.")
        return result

    def enqueue_command(
        self,
        operation: str,
        *,
        arguments: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.base_url}/broker/commands",
                json={
                    "operation": operation,
                    "arguments": arguments or {},
                    "metadata": metadata or {},
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            raise BrokerEngineClientError(f"Failed to enqueue broker-engine operation '{operation}'.") from exc
        return self._extract_result(response, f"broker-engine enqueue '{operation}'")

    def consume_events(self, *, consumer_id: str, limit: int = 200) -> dict[str, Any]:
        self._wait_for_turn()
        try:
            response = self.session.get(
                f"{self.base_url}/broker/events",
                params={"consumer_id": consumer_id, "limit": limit},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise BrokerEngineClientError("Failed to fetch broker-engine events.") from exc
        return self._extract_result(response, "broker-engine event fetch")

    def get_snapshots(
        self,
        *,
        consumer_id: str,
        snapshot_type: str = "",
        resource_id: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        self._wait_for_turn()
        params: dict[str, Any] = {"consumer_id": consumer_id, "limit": limit}
        if snapshot_type:
            params["snapshot_type"] = snapshot_type
        if resource_id:
            params["resource_id"] = resource_id
        try:
            response = self.session.get(
                f"{self.base_url}/broker/snapshots",
                params=params,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise BrokerEngineClientError("Failed to fetch broker-engine snapshots.") from exc
        return self._extract_result(response, "broker-engine snapshot fetch")

    def process_commands(self, *, max_commands: int = 16) -> dict[str, Any]:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.base_url}/broker/commands/process",
                params={"max_commands": max_commands},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise BrokerEngineClientError("Failed to process broker-engine commands.") from exc
        return self._extract_result(response, "broker-engine command processing")

    def _wait_for_turn(self) -> None:
        min_interval = 1.0 / max(self.rate_limit_per_second, 1)
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_at = time.monotonic()

    def _extract_result(self, response: requests.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerEngineClientError(f"{label} returned non-JSON (HTTP {response.status_code}).") from exc
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise BrokerEngineClientError(f"{label} failed: {detail}")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise BrokerEngineClientError(f"{label} returned an unexpected payload.")
        return result


@dataclass(slots=True)
class MarketDataEngineClient:
    base_url: str
    session: requests.Session
    rate_limit_per_second: int = 2
    _lock: Lock = field(default_factory=Lock)
    _last_request_at: float = 0.0

    @classmethod
    def from_settings(cls, settings: KISSettings) -> "MarketDataEngineClient":
        return cls(
            base_url=settings.market_data_engine_base_url.rstrip("/"),
            session=requests.Session(),
            rate_limit_per_second=_cap_kis_rate(settings.market_data_engine_rate_limit_per_second),
        )

    def health_check(self) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}/health", timeout=5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise MarketDataEngineClientError("market-data-engine health returned a non-object payload.")
        return payload

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(4):
            self._wait_for_turn()
            request_started = time.perf_counter()
            safe_arguments = _safe_market_data_arguments(arguments or {})
            logger.debug(
                "market_data_engine.call.start",
                extra={
                    "tool": tool,
                    "attempt": attempt + 1,
                    "base_url": self.base_url,
                    **safe_arguments,
                },
            )
            try:
                response = self.session.post(
                    f"{self.base_url}/tools/call",
                    json={"tool": tool, "arguments": arguments or {}},
                    timeout=60,
                )
            except requests.RequestException as exc:
                logger.warning(
                    "market_data_engine.call.request_failed",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "base_url": self.base_url,
                        "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                        "error": str(exc),
                        **safe_arguments,
                    },
                )
                raise MarketDataEngineClientError(
                    f"Failed to call market-data-engine tool '{tool}' at {self.base_url}."
                ) from exc
            try:
                payload = response.json()
            except ValueError as exc:
                logger.warning(
                    "market_data_engine.call.non_json_response",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "status_code": response.status_code,
                        "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                        **safe_arguments,
                    },
                )
                raise MarketDataEngineClientError(
                    f"market-data-engine returned non-JSON for tool '{tool}' (HTTP {response.status_code})."
                ) from exc
            if response.status_code < 400:
                result = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(result, dict):
                    logger.warning(
                        "market_data_engine.call.unexpected_payload",
                        extra={
                            "tool": tool,
                            "attempt": attempt + 1,
                            "status_code": response.status_code,
                            "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                            **safe_arguments,
                        },
                    )
                    raise MarketDataEngineClientError(
                        f"market-data-engine tool '{tool}' returned an unexpected payload."
                    )
                logger.debug(
                    "market_data_engine.call.success",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "status_code": response.status_code,
                        "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                        **safe_arguments,
                    },
                )
                return result
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            if attempt < 3 and _is_kis_rate_limit_error(detail):
                logger.warning(
                    "market_data_engine.call.rate_limited",
                    extra={
                        "tool": tool,
                        "attempt": attempt + 1,
                        "status_code": response.status_code,
                        "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                        "error": str(detail),
                        **safe_arguments,
                    },
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            logger.warning(
                "market_data_engine.call.failed",
                extra={
                    "tool": tool,
                    "attempt": attempt + 1,
                    "status_code": response.status_code,
                    "elapsed_ms": (time.perf_counter() - request_started) * 1000,
                    "error": str(detail),
                    **safe_arguments,
                },
            )
            raise MarketDataEngineClientError(f"market-data-engine tool '{tool}' failed: {detail}")
        raise MarketDataEngineClientError(f"market-data-engine tool '{tool}' failed after retries.")

    def _wait_for_turn(self) -> None:
        min_interval = 1.0 / max(self.rate_limit_per_second, 1)
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_at = time.monotonic()


@dataclass(slots=True)
class KISBrokerEngineMarketDataProvider(MarketDataProvider):
    """Legacy-compatible KIS provider name backed by an in-process KIS boundary by default."""

    client: BrokerEngineClient
    exchange_by_symbol: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        exchange_by_symbol: Mapping[str, str] | None = None,
    ) -> "KISBrokerEngineMarketDataProvider":
        return cls(
            client=KISDirectClient.from_settings(load_kis_settings()),
            exchange_by_symbol=dict(exchange_by_symbol or {}),
        )

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        arguments = _latest_quote_arguments(symbol, exchange_by_symbol=self.exchange_by_symbol)
        arguments.setdefault("exchange", None)
        result = self.client.call_operation(
            "get_stock_price",
            arguments,
        )
        price = _extract_price(result)
        return Bar(
            symbol=symbol,
            time=datetime.now(),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=int(_first_present(result, ("volume", "acml_vol", "accumulated_volume"), default=0)),
            resolution=DataResolution.LIVE.value,
        )

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        result = self.client.call_operation(
            "get_daily_ohlcv",
            _daily_history_arguments(
                symbol,
                start=start,
                end=end,
                exchange_by_symbol=self.exchange_by_symbol,
            ),
        )
        rows = _extract_history_rows(result)
        return _daily_rows_to_bars(symbol, rows)

    def get_cached_daily_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh: bool = False,
    ) -> list[Bar]:
        result = self.client.call_operation(
            "get_or_cache_daily_ohlcv",
            {
                **_daily_history_arguments(symbol, start=start, end=end),
                **_exchange_argument(symbol, self.exchange_by_symbol),
                "refresh": refresh,
            },
        )
        rows = _extract_history_rows(result)
        return _daily_rows_to_bars(symbol, rows)


@dataclass(slots=True)
class KISCachedMarketDataProvider(MarketDataProvider):
    """Cache-first KIS provider backed by an in-process file cache by default."""

    client: MarketDataEngineClient
    exchange_by_symbol: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        exchange_by_symbol: Mapping[str, str] | None = None,
    ) -> "KISCachedMarketDataProvider":
        return cls(
            client=KISDirectClient.from_settings(load_kis_settings()),
            exchange_by_symbol=dict(exchange_by_symbol or {}),
        )

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        result = self.client.call_tool(
            "get_stock_price",
            _latest_quote_arguments(symbol, exchange_by_symbol=self.exchange_by_symbol),
        )
        price = _extract_price(result)
        return Bar(
            symbol=symbol,
            time=datetime.now(),
            open=price,
            high=price,
            low=price,
            close=price,
            volume=int(_first_present(result, ("volume", "acml_vol", "accumulated_volume"), default=0)),
            resolution=DataResolution.LIVE.value,
        )

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        result = self.client.call_tool(
            "get_daily_ohlcv",
            _daily_history_arguments(
                symbol,
                start=start,
                end=end,
                exchange_by_symbol=self.exchange_by_symbol,
            ),
        )
        rows = _extract_history_rows(result)
        return _daily_rows_to_bars(symbol, rows)

    def get_cached_daily_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh: bool = False,
    ) -> list[Bar]:
        result = self.client.call_tool(
            "get_or_cache_daily_ohlcv",
            {
                **_daily_history_arguments(symbol, start=start, end=end),
                **_exchange_argument(symbol, self.exchange_by_symbol),
                "refresh": refresh,
            },
        )
        rows = _extract_history_rows(result)
        return _daily_rows_to_bars(symbol, rows)

    def get_cached_minute_history(
        self,
        symbol: Symbol,
        *,
        trade_date: datetime,
        start_time: str | None = None,
        end_time: str | None = None,
        interval_minutes: int = 1,
        refresh: bool = False,
    ) -> list[Bar]:
        if _kis_market(symbol.market) == "overseas":
            exchange = _resolve_exchange(symbol, self.exchange_by_symbol)
            result = self.client.call_tool(
                "get_or_cache_overseas_minute_bars",
                {
                    "symbol": symbol.ticker,
                    "exchange": exchange,
                    "trade_date": trade_date.strftime("%Y-%m-%d"),
                    "start_time": start_time,
                    "end_time": end_time,
                    "interval_minutes": interval_minutes,
                    "refresh": refresh,
                },
            )
            rows = _extract_history_rows(result)
            return sorted(
                (_row_to_bar(symbol, row, default_date=trade_date, resolution=DataResolution.MINUTE.value) for row in rows),
                key=lambda bar: bar.time,
            )
        result = self.client.call_tool(
            "get_or_cache_domestic_minute_bars",
            {
                "symbol": symbol.ticker,
                "trade_date": trade_date.strftime("%Y-%m-%d"),
                "start_time": start_time,
                "end_time": end_time,
                "interval_minutes": interval_minutes,
                "refresh": refresh,
            },
        )
        rows = _extract_history_rows(result)
        return sorted(
            (_row_to_bar(symbol, row, default_date=trade_date, resolution=DataResolution.MINUTE.value) for row in rows),
            key=lambda bar: bar.time,
        )


@dataclass(slots=True)
class MarketDataEngineLiveQuoteProvider(MarketDataProvider):
    """Live quote adapter backed by an in-process KIS boundary by default."""

    client: MarketDataEngineClient
    exchange_by_symbol: Mapping[str, str] = field(default_factory=dict)
    live_quote_cache_max_age_seconds: float = 90.0
    prefer_live_quote_cache: bool = True

    @classmethod
    def from_env(
        cls,
        exchange_by_symbol: Mapping[str, str] | None = None,
        rate_limit_per_second: int | None = None,
    ) -> "MarketDataEngineLiveQuoteProvider":
        client = KISDirectClient.from_settings(load_kis_settings())
        if rate_limit_per_second is not None:
            client.rate_limit_per_second = _cap_kis_rate(rate_limit_per_second)
        return cls(
            client=client,
            exchange_by_symbol=dict(exchange_by_symbol or {}),
        )

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        arguments = _latest_quote_arguments(symbol, exchange_by_symbol=self.exchange_by_symbol)
        if self.prefer_live_quote_cache:
            cached = _read_live_quote_cache_bar(
                self.client,
                symbol,
                max_age_seconds=self.live_quote_cache_max_age_seconds,
            )
            if cached is not None:
                return cached
        try:
            result = self.client.call_tool(
                "get_stock_price",
                arguments,
            )
            bar = _quote_to_bar(symbol, result, require_live_price=True, allow_reference_price=True)
            _write_live_quote_cache_bar(self.client, bar)
            return bar
        except Exception:
            cached = _read_live_quote_cache_bar(
                self.client,
                symbol,
                max_age_seconds=self.live_quote_cache_max_age_seconds,
            )
            if cached is not None:
                return cached
            raise

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        raise MarketDataError("MarketDataEngineLiveQuoteProvider does not support history.")


def _read_live_quote_cache_bar(
    client: Any,
    symbol: Symbol,
    *,
    max_age_seconds: float,
) -> Bar | None:
    path = _live_quote_cache_path(client)
    if path is None:
        return None
    payload = _read_json_object(path)
    entry = payload.get(symbol.key)
    if not isinstance(entry, dict):
        return None
    cached_at = _parse_cache_datetime(entry.get("cached_at"))
    if cached_at is None:
        return None
    now = datetime.now(tz=cached_at.tzinfo) if cached_at.tzinfo is not None else datetime.now()
    cache_age_seconds = max((now - cached_at).total_seconds(), 0.0)
    if cache_age_seconds > max_age_seconds:
        return None
    bar = _bar_from_live_quote_cache_entry(symbol, entry)
    if bar is None:
        return None
    metadata = dict(bar.metadata)
    metadata.update(
        {
            "live_quote_cache_status": "hit",
            "live_quote_cache_age_seconds": cache_age_seconds,
            "live_quote_cache_cached_at": cached_at.isoformat(),
        }
    )
    return replace(bar, metadata=metadata)


def _write_live_quote_cache_bar(client: Any, bar: Bar) -> None:
    path = _live_quote_cache_path(client)
    if path is None:
        return
    payload = _read_json_object(path)
    payload[bar.symbol.key] = _bar_to_live_quote_cache_entry(bar)
    _write_json_object(path, payload)


def _live_quote_cache_path(client: Any) -> Path | None:
    cache_dir = getattr(client, "cache_dir", None)
    if cache_dir is None:
        return None
    return Path(cache_dir) / "live-quotes" / "latest.json"


def _bar_to_live_quote_cache_entry(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol.key,
        "ticker": bar.symbol.ticker,
        "market": bar.symbol.market,
        "time": bar.time.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "resolution": bar.resolution,
        "metadata": dict(bar.metadata),
        "cached_at": datetime.now().isoformat(),
    }


def _bar_from_live_quote_cache_entry(symbol: Symbol, entry: Mapping[str, Any]) -> Bar | None:
    bar_time = _parse_cache_datetime(entry.get("time"))
    if bar_time is None:
        return None
    try:
        return Bar(
            symbol=symbol,
            time=bar_time,
            open=float(entry.get("open")),
            high=float(entry.get("high")),
            low=float(entry.get("low")),
            close=float(entry.get("close")),
            volume=int(float(entry.get("volume") or 0)),
            resolution=_live_quote_cache_resolution(entry),
            metadata=dict(entry.get("metadata") or {}),
        )
    except (TypeError, ValueError):
        return None


def _live_quote_cache_resolution(entry: Mapping[str, Any]) -> str:
    resolution = str(entry.get("resolution") or "").strip().lower()
    if resolution in {"", DataResolution.ANY.value, "unknown", "*"}:
        return DataResolution.LIVE.value
    return resolution


def _parse_cache_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_object(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.{time.monotonic_ns()}.tmp")
    temporary_path.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _daily_history_arguments(
    symbol: Symbol,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    exchange_by_symbol: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "market": _kis_market(symbol.market),
        "symbol": symbol.ticker,
        "period_code": "D",
        "adjusted_price": True,
        "start_date": start.strftime("%Y%m%d") if start else None,
        "end_date": end.strftime("%Y%m%d") if end else None,
    }
    exchange = _resolve_optional_exchange(symbol, exchange_by_symbol or {})
    if exchange:
        arguments["exchange"] = exchange
    return arguments


def _exchange_argument(symbol: Symbol, exchange_by_symbol: Mapping[str, str]) -> dict[str, str]:
    exchange = _resolve_optional_exchange(symbol, exchange_by_symbol)
    return {"exchange": exchange} if exchange else {}


def _safe_market_data_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key in (
        "market",
        "symbol",
        "exchange",
        "period_code",
        "start_date",
        "end_date",
        "trade_date",
        "start_time",
        "end_time",
        "interval_minutes",
        "refresh",
    ):
        if key in arguments:
            safe[key] = arguments[key]
    return safe


def _cap_kis_rate(value: int) -> int:
    return min(max(int(value), 1), 18)


def _latest_quote_arguments(
    symbol: Symbol,
    *,
    exchange_by_symbol: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    market = _kis_market(symbol.market)
    arguments: dict[str, Any] = {
        "market": market,
        "symbol": symbol.ticker,
    }
    exchange = _resolve_exchange(symbol, exchange_by_symbol or {})
    if exchange:
        arguments["exchange"] = exchange
    return arguments


def _resolve_exchange(symbol: Symbol, exchange_by_symbol: Mapping[str, str]) -> str | None:
    if _kis_market(symbol.market) == "domestic":
        return None
    normalized_market = symbol.market.strip().upper()
    if normalized_market in {"NAS", "NYS", "AMS"}:
        return normalized_market
    exchange = exchange_by_symbol.get(symbol.key) or exchange_by_symbol.get(symbol.ticker)
    if exchange:
        return str(exchange).strip().upper()
    raise MarketDataError(f"Exchange is required for overseas symbol {symbol.key}.")


def _resolve_optional_exchange(symbol: Symbol, exchange_by_symbol: Mapping[str, str]) -> str | None:
    if _kis_market(symbol.market) == "domestic":
        return None
    normalized_market = symbol.market.strip().upper()
    if normalized_market in {"NAS", "NYS", "AMS"}:
        return normalized_market
    exchange = exchange_by_symbol.get(symbol.key) or exchange_by_symbol.get(symbol.ticker)
    if exchange:
        return str(exchange).strip().upper()
    return _kis_exchange(symbol.market)


def _is_kis_rate_limit_error(detail: Any) -> bool:
    text = str(detail)
    return "EGW00201" in text or "초당 거래건수" in text


def _kis_market(market: str) -> str:
    normalized = market.strip().upper()
    if normalized in {"KR", "KRX", "KOR", "DOMESTIC"}:
        return "domestic"
    return "overseas"


def _kis_exchange(market: str) -> str | None:
    normalized = market.strip().upper()
    if normalized in {"KR", "KRX", "KOR", "DOMESTIC"}:
        return None
    if normalized in {"US", "USA", "OVERSEAS", "MIXED"}:
        return None
    return normalized


def _extract_price(payload: dict[str, Any]) -> float:
    value = _first_present(
        payload,
        (
            "price",
            "last_price",
            "current_price",
            "stck_prpr",
            "ovrs_nmix_prpr",
            "close",
            "close_price",
        ),
    )
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError) as exc:
        raise MarketDataError(f"Could not extract price from KIS payload keys={sorted(payload)}") from exc


def _extract_history_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = (
        payload.get("bars"),
        payload.get("candles"),
        payload.get("prices"),
        payload.get("output2"),
        payload.get("rows"),
    )
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    raise MarketDataError(f"Could not extract history rows from KIS payload keys={sorted(payload)}")


def _row_to_bar(
    symbol: Symbol,
    row: dict[str, Any],
    *,
    default_date: datetime | None = None,
    resolution: str = DataResolution.DAILY.value,
) -> Bar:
    return Bar(
        symbol=symbol,
        time=_parse_row_datetime(row, default_date=default_date),
        open=_float_field(row, "open", "open_price", "stck_oprc", "ovrs_nmix_oprc"),
        high=_float_field(row, "high", "high_price", "stck_hgpr", "ovrs_nmix_hgpr"),
        low=_float_field(row, "low", "low_price", "stck_lwpr", "ovrs_nmix_lwpr"),
        close=_float_field(row, "close", "close_price", "last", "stck_clpr", "stck_prpr", "ovrs_nmix_prpr"),
        volume=int(_first_present(row, ("volume", "cntg_vol", "evol", "acml_vol", "acml_vol_qty"), default=0)),
        resolution=resolution,
    )


def _quote_to_bar(
    symbol: Symbol,
    payload: dict[str, Any],
    *,
    require_live_price: bool = False,
    allow_reference_price: bool = False,
) -> Bar:
    price = _extract_price(payload)
    raw_open_price = _float_first_present(payload, ("open", "open_price", "day_open"), default=price)
    raw_high_price = _float_first_present(payload, ("high", "high_price", "day_high"), default=max(raw_open_price, price))
    raw_low_price = _float_first_present(payload, ("low", "low_price", "day_low"), default=min(raw_open_price, price))
    volume = int(_first_present(payload, ("volume", "acml_vol", "accumulated_volume"), default=0))
    live_price_usable = payload.get("live_price_usable")
    price_quality_reason = str(payload.get("price_quality_reason") or "").strip()
    looks_like_reference_price = _looks_like_zero_ohlc_reference_price(
        payload,
        price,
        raw_open_price,
        raw_high_price,
        raw_low_price,
    )
    if require_live_price and _kis_market(symbol.market) == "domestic":
        if live_price_usable is False and not allow_reference_price:
            reason = price_quality_reason or "domestic live quote is not usable"
            raise MarketDataError(f"Domestic live quote for {symbol.key} is not usable: {reason}")
        if looks_like_reference_price:
            live_price_usable = False
            price_quality_reason = price_quality_reason or "reference_price_without_distinct_orderbook_price"
        if looks_like_reference_price and not allow_reference_price:
            raise MarketDataError(f"Domestic live quote for {symbol.key} looks like a reference price.")
    open_price = raw_open_price if raw_open_price > 0 else price
    high_price = raw_high_price if raw_high_price > 0 else max(open_price, price)
    low_price = raw_low_price if raw_low_price > 0 else min(open_price, price)
    metadata: dict[str, Any] = {}
    if live_price_usable is not None:
        metadata["live_price_usable"] = bool(live_price_usable)
    if price_quality_reason:
        metadata["price_quality_reason"] = price_quality_reason
    if payload.get("price_source"):
        metadata["price_source"] = str(payload.get("price_source"))
    return Bar(
        symbol=symbol,
        time=datetime.now(),
        open=open_price,
        high=high_price,
        low=low_price,
        close=price,
        volume=volume,
        resolution=DataResolution.LIVE.value,
        metadata=metadata,
    )


def _daily_rows_to_bars(symbol: Symbol, rows: list[dict[str, Any]]) -> list[Bar]:
    bars = sorted((_row_to_bar(symbol, row, resolution=DataResolution.DAILY.value) for row in rows), key=lambda bar: bar.time)
    return _quarantine_daily_history_outliers(bars)


def _quarantine_daily_history_outliers(bars: list[Bar]) -> list[Bar]:
    if len(bars) < 2:
        return bars
    result: list[Bar] = []
    previous: Bar | None = None
    for bar in bars:
        if previous is not None and _looks_like_adjusted_price_discontinuity(previous, bar):
            continue
        result.append(bar)
        previous = bar
    return result


def _looks_like_adjusted_price_discontinuity(previous: Bar, current: Bar) -> bool:
    if current.volume > 0 or previous.close <= 0 or current.close <= 0:
        return False
    ratio = current.close / previous.close
    return ratio >= 3.0 or ratio <= (1.0 / 3.0)


def _parse_date(value: str) -> datetime:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)


def _parse_row_datetime(row: dict[str, Any], *, default_date: datetime | None = None) -> datetime:
    for name in ("datetime", "timestamp", "ts"):
        value = row.get(name)
        if value not in (None, ""):
            return _parse_date(str(value))
    date_value = _first_present(row, ("date", "trade_date", "stck_bsop_date", "xymd"), default=None)
    time_value = _first_present(row, ("time", "stck_cntg_hour", "hour", "hhmmss", "xhms", "local_time"), default=None)
    if date_value not in (None, "") and time_value not in (None, ""):
        return _combine_date_time(str(date_value), str(time_value))
    if time_value not in (None, "") and default_date is not None:
        return _combine_date_time(default_date.strftime("%Y%m%d"), str(time_value))
    if date_value not in (None, ""):
        return _parse_date(str(date_value))
    value = _first_present(row, ("time",), default=None)
    if value not in (None, ""):
        return _parse_date(str(value))
    raise MarketDataError(f"Could not extract datetime from KIS history row keys={sorted(row)}")


def _combine_date_time(date_value: str, time_value: str) -> datetime:
    date_text = date_value.strip().replace("-", "")
    time_text = time_value.strip().replace(":", "")
    if "." in time_text:
        time_text = time_text.split(".", 1)[0]
    if len(time_text) == 4 and time_text.isdigit():
        time_text = f"{time_text}00"
    if len(time_text) == 5 and time_text.isdigit():
        time_text = f"0{time_text}"
    if len(date_text) == 8 and time_text[:6].isdigit():
        return datetime.strptime(f"{date_text}{time_text[:6]}", "%Y%m%d%H%M%S")
    return _parse_date(f"{date_value}T{time_value}")


def _float_field(row: dict[str, Any], *names: str) -> float:
    value = _first_present(row, names)
    return float(str(value).replace(",", ""))


def _float_first_present(payload: dict[str, Any], names: tuple[str, ...], *, default: float) -> float:
    value = _first_present(payload, names, default=default)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _looks_like_zero_ohlc_reference_price(
    payload: dict[str, Any],
    price: float,
    open_price: float,
    high_price: float,
    low_price: float,
) -> bool:
    raw_output = payload.get("raw_output")
    if not isinstance(raw_output, dict):
        return False
    standard = _float_first_present(raw_output, ("stck_sdpr",), default=0.0)
    change = _float_first_present(raw_output, ("prdy_vrss",), default=0.0)
    change_rate = _float_first_present(raw_output, ("prdy_ctrt",), default=0.0)
    return (
        price > 0
        and standard > 0
        and price == standard
        and open_price <= 0
        and high_price <= 0
        and low_price <= 0
        and change == 0
        and abs(change_rate) < 0.000001
    )


def _first_present(payload: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return default

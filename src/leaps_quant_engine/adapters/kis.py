from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from threading import Lock
import time
from typing import Any, Mapping

import requests

from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol
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
    """MarketDataProvider adapter backed by the local legacy broker-engine."""

    client: BrokerEngineClient

    @classmethod
    def from_env(cls) -> "KISBrokerEngineMarketDataProvider":
        return cls(client=BrokerEngineClient.from_settings(load_kis_settings()))

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        result = self.client.call_operation(
            "get_stock_price",
            {
                "market": _kis_market(symbol.market),
                "symbol": symbol.ticker,
                "exchange": _kis_exchange(symbol.market),
            },
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
            {
                "market": _kis_market(symbol.market),
                "symbol": symbol.ticker,
                "period_code": "D",
                "adjusted_price": True,
                "start_date": start.strftime("%Y%m%d") if start else None,
                "end_date": end.strftime("%Y%m%d") if end else None,
            },
        )
        rows = _extract_history_rows(result)
        return sorted((_row_to_bar(symbol, row) for row in rows), key=lambda bar: bar.time)

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
                "market": _kis_market(symbol.market),
                "symbol": symbol.ticker,
                "period_code": "D",
                "adjusted_price": True,
                "start_date": start.strftime("%Y%m%d") if start else None,
                "end_date": end.strftime("%Y%m%d") if end else None,
                "refresh": refresh,
            },
        )
        rows = _extract_history_rows(result)
        return sorted((_row_to_bar(symbol, row) for row in rows), key=lambda bar: bar.time)


@dataclass(slots=True)
class KISCachedMarketDataProvider(MarketDataProvider):
    """Cache-first historical KIS provider backed by local market-data-engine."""

    client: MarketDataEngineClient

    @classmethod
    def from_env(cls) -> "KISCachedMarketDataProvider":
        return cls(client=MarketDataEngineClient.from_settings(load_kis_settings()))

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        result = self.client.call_tool(
            "get_stock_price",
            {
                "market": _kis_market(symbol.market),
                "symbol": symbol.ticker,
            },
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
            _daily_history_arguments(symbol, start=start, end=end),
        )
        rows = _extract_history_rows(result)
        return sorted((_row_to_bar(symbol, row) for row in rows), key=lambda bar: bar.time)

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
                "refresh": refresh,
            },
        )
        rows = _extract_history_rows(result)
        return sorted((_row_to_bar(symbol, row) for row in rows), key=lambda bar: bar.time)

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
        if _kis_market(symbol.market) != "domestic":
            raise MarketDataError("Cached minute history is currently supported for domestic symbols only.")
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
            (_row_to_bar(symbol, row, default_date=trade_date) for row in rows),
            key=lambda bar: bar.time,
        )


@dataclass(slots=True)
class MarketDataEngineLiveQuoteProvider(MarketDataProvider):
    """Live quote adapter backed by the local market-data-engine tier."""

    client: MarketDataEngineClient
    exchange_by_symbol: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(
        cls,
        exchange_by_symbol: Mapping[str, str] | None = None,
        rate_limit_per_second: int | None = None,
    ) -> "MarketDataEngineLiveQuoteProvider":
        client = MarketDataEngineClient.from_settings(load_kis_settings())
        if rate_limit_per_second is not None:
            client.rate_limit_per_second = _cap_kis_rate(rate_limit_per_second)
        return cls(
            client=client,
            exchange_by_symbol=dict(exchange_by_symbol or {}),
        )

    def health_check(self) -> dict[str, Any]:
        return self.client.health_check()

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        result = self.client.call_tool(
            "get_stock_price",
            _latest_quote_arguments(symbol, exchange_by_symbol=self.exchange_by_symbol),
        )
        return _quote_to_bar(symbol, result)

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        raise MarketDataError("MarketDataEngineLiveQuoteProvider does not support history.")


def _daily_history_arguments(
    symbol: Symbol,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict[str, Any]:
    return {
        "market": _kis_market(symbol.market),
        "symbol": symbol.ticker,
        "period_code": "D",
        "adjusted_price": True,
        "start_date": start.strftime("%Y%m%d") if start else None,
        "end_date": end.strftime("%Y%m%d") if end else None,
    }


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
    return min(max(int(value), 1), 20)


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


def _row_to_bar(symbol: Symbol, row: dict[str, Any], *, default_date: datetime | None = None) -> Bar:
    return Bar(
        symbol=symbol,
        time=_parse_row_datetime(row, default_date=default_date),
        open=_float_field(row, "open", "open_price", "stck_oprc", "ovrs_nmix_oprc"),
        high=_float_field(row, "high", "high_price", "stck_hgpr", "ovrs_nmix_hgpr"),
        low=_float_field(row, "low", "low_price", "stck_lwpr", "ovrs_nmix_lwpr"),
        close=_float_field(row, "close", "close_price", "stck_clpr", "stck_prpr", "ovrs_nmix_prpr"),
        volume=int(_first_present(row, ("volume", "cntg_vol", "acml_vol", "acml_vol_qty"), default=0)),
    )


def _quote_to_bar(symbol: Symbol, payload: dict[str, Any]) -> Bar:
    price = _extract_price(payload)
    open_price = _float_first_present(payload, ("open", "open_price", "day_open"), default=price)
    high_price = _float_first_present(payload, ("high", "high_price", "day_high"), default=max(open_price, price))
    low_price = _float_first_present(payload, ("low", "low_price", "day_low"), default=min(open_price, price))
    volume = int(_first_present(payload, ("volume", "acml_vol", "accumulated_volume"), default=0))
    return Bar(
        symbol=symbol,
        time=datetime.now(),
        open=open_price,
        high=high_price,
        low=low_price,
        close=price,
        volume=volume,
    )


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
    time_value = _first_present(row, ("time", "stck_cntg_hour", "hour", "hhmmss"), default=None)
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


def _first_present(payload: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return default

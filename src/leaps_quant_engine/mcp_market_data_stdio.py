from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

from leaps_quant_engine.adapters.kis_direct import KISDirectClient
from leaps_quant_engine.settings import load_kis_settings


JSON_RPC_VERSION = "2.0"
SERVER_INFO = {"name": "leaps-quant-market-data", "version": "0.1.0"}
DEFAULT_TRACE_LOG_PATH = Path("logs") / "market_data_mcp_stdio.jsonl"


class UnknownToolError(KeyError):
    """Raised when an MCP tool name is not registered."""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]

    def to_mcp_tool(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class LeapsMarketDataToolRegistry:
    """Small stdio MCP tool registry backed by the local LEaps KIS boundary."""

    def __init__(self, client: KISDirectClient | Any):
        self.client = client
        self._tools = {
            tool.name: tool
            for tool in (
                ToolDefinition(
                    name="health_check",
                    description="Return a low-risk health payload for the local LEaps market-data MCP.",
                    input_schema=_object_schema({}),
                    handler=self._health_check,
                ),
                ToolDefinition(
                    name="get_stock_price",
                    description="Return one normalized domestic or overseas quote through the local KIS adapter.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "market": {"type": "string", "enum": ["domestic", "overseas"], "default": "domestic"},
                            "exchange": {"type": "string"},
                        },
                        required=("symbol",),
                    ),
                    handler=lambda args: self._call_operation("get_stock_price", args),
                ),
                ToolDefinition(
                    name="get_daily_ohlcv",
                    description="Return normalized daily, weekly, or monthly OHLCV rows through the local KIS adapter.",
                    input_schema=_history_schema(cache=False),
                    handler=lambda args: self._call_operation("get_daily_ohlcv", args),
                ),
                ToolDefinition(
                    name="get_or_cache_daily_ohlcv",
                    description="Fetch or reuse local cache for normalized daily OHLCV rows.",
                    input_schema=_history_schema(cache=True),
                    handler=lambda args: self._call_operation("get_or_cache_daily_ohlcv", args),
                ),
                ToolDefinition(
                    name="get_overseas_daily_ohlcv",
                    description="Compatibility wrapper for overseas daily OHLCV. Requires exchange such as NAS or NYS.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "exchange": {"type": "string"},
                            "period_code": {"type": "string", "default": "D"},
                            "adjusted_price": {"type": "boolean", "default": True},
                            "start_date": {"type": "string"},
                            "end_date": {"type": "string"},
                        },
                        required=("symbol", "exchange"),
                    ),
                    handler=self._get_overseas_daily_ohlcv,
                ),
                ToolDefinition(
                    name="get_intraday_bars",
                    description="Return same-day domestic intraday bars through the local KIS adapter.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "market": {"type": "string", "default": "domestic"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "include_previous_data": {"type": "boolean"},
                        },
                        required=("symbol",),
                    ),
                    handler=lambda args: self._call_operation("get_intraday_bars", args),
                ),
                ToolDefinition(
                    name="get_overseas_intraday_bars",
                    description="Return recent overseas intraday bars through the local KIS adapter.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "exchange": {"type": "string"},
                            "interval_minutes": {"type": "integer", "default": 1},
                            "record_count": {"type": "integer", "default": 120},
                        },
                        required=("symbol", "exchange"),
                    ),
                    handler=lambda args: self._call_operation("get_overseas_intraday_bars", args),
                ),
                ToolDefinition(
                    name="get_or_cache_domestic_minute_bars",
                    description="Fetch or reuse local cache for domestic minute bars by trade date.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "trade_date": {"type": "string"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "interval_minutes": {"type": "integer", "default": 1},
                            "refresh": {"type": "boolean", "default": False},
                            "include_previous_data": {"type": "boolean"},
                        },
                        required=("symbol", "trade_date"),
                    ),
                    handler=lambda args: self._call_operation("get_or_cache_domestic_minute_bars", args),
                ),
                ToolDefinition(
                    name="get_or_cache_overseas_minute_bars",
                    description="Fetch or reuse local cache for overseas minute bars by trade date.",
                    input_schema=_object_schema(
                        {
                            "symbol": {"type": "string"},
                            "exchange": {"type": "string"},
                            "trade_date": {"type": "string"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                            "interval_minutes": {"type": "integer", "default": 1},
                            "refresh": {"type": "boolean", "default": False},
                        },
                        required=("symbol", "exchange", "trade_date"),
                    ),
                    handler=lambda args: self._call_operation("get_or_cache_overseas_minute_bars", args),
                ),
                ToolDefinition(
                    name="build_whitelist_live_facts",
                    description="Build quote fact packets for a bounded whitelist using the local KIS adapter.",
                    input_schema=_object_schema(
                        {
                            "symbols": {"type": "array", "items": {"type": "string"}},
                            "market_scope": {
                                "type": "string",
                                "enum": ["domestic", "overseas"],
                                "default": "domestic",
                            },
                            "exchange": {"type": "string"},
                            "max_symbols": {"type": "integer", "default": 20},
                        },
                    ),
                    handler=self._build_whitelist_live_facts,
                ),
                ToolDefinition(
                    name="get_market_session_status",
                    description="Return a local lightweight market-session estimate. Holiday calendars are not applied.",
                    input_schema=_object_schema(
                        {
                            "market": {"type": "string", "default": "domestic"},
                            "now": {"type": "string"},
                        },
                    ),
                    handler=self._get_market_session_status,
                ),
            )
        }

    @classmethod
    def from_env(cls) -> "LeapsMarketDataToolRegistry":
        return cls.with_default_client()

    @classmethod
    def with_default_client(cls) -> "LeapsMarketDataToolRegistry":
        settings = load_kis_settings()
        cache_dir = Path(os.getenv("LEAPS_KIS_CACHE_DIR", "data/kis-cache")).resolve()
        return cls(
            KISDirectClient(
                settings=settings,
                cache_dir=cache_dir,
                rate_limit_per_second=min(max(settings.rate_limit_per_second, 1), 20),
            )
        )

    def list_tool_definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools.values())

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(f"Unknown LEaps market-data MCP tool: {name}")
        args = dict(arguments or {})
        return tool.handler(args)

    def _health_check(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        payload = self.client.health_check()
        return {
            "status": "ok",
            "server": SERVER_INFO["name"],
            "transport": "stdio",
            "kis": payload,
        }

    def _call_operation(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        return self.client.call_operation(operation, args)

    def _get_overseas_daily_ohlcv(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = dict(args)
        payload["market"] = "overseas"
        return self.client.call_operation("get_daily_ohlcv", payload)

    def _build_whitelist_live_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        market_scope = str(args.get("market_scope") or "domestic").strip().lower()
        market = "overseas" if market_scope == "overseas" else "domestic"
        symbols = [str(symbol).strip() for symbol in args.get("symbols") or [] if str(symbol).strip()]
        max_symbols = max(int(args.get("max_symbols") or 20), 0)
        selected = symbols[:max_symbols] if max_symbols else []
        facts: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for symbol in selected:
            request = {"market": market, "symbol": symbol}
            if market == "overseas" and args.get("exchange"):
                request["exchange"] = str(args["exchange"])
            try:
                quote = self.client.call_operation("get_stock_price", request)
            except Exception as exc:  # noqa: BLE001
                failures.append({"symbol": symbol, "error": str(exc)})
                continue
            facts.append(
                {
                    "symbol": symbol,
                    "market_scope": market,
                    "quote": quote,
                    "last_price": quote.get("last_price"),
                    "volume": quote.get("volume"),
                    "source": "leaps_kis_direct",
                }
            )
        return {
            "status": "ok" if not failures else "partial",
            "market_scope": market,
            "requested_symbol_count": len(symbols),
            "fact_count": len(facts),
            "failure_count": len(failures),
            "facts": facts,
            "failures": failures,
        }

    def _get_market_session_status(self, args: dict[str, Any]) -> dict[str, Any]:
        market = str(args.get("market") or "domestic").strip().lower()
        now = _parse_datetime(args.get("now")) or datetime.now(timezone(timedelta(hours=9)))
        if market not in {"domestic", "kr", "krx"}:
            return {
                "status": "unknown",
                "market": market,
                "session_status": "unknown",
                "reason": "local_session_estimate_currently_supports_domestic_krx_only",
            }
        kst = timezone(timedelta(hours=9))
        local_now = now.astimezone(kst) if now.tzinfo else now.replace(tzinfo=kst)
        is_weekday = local_now.weekday() < 5
        is_regular_hours = dt_time(9, 0) <= local_now.time() <= dt_time(15, 30)
        session_status = "open" if is_weekday and is_regular_hours else "closed"
        return {
            "status": "ok",
            "market": "domestic",
            "session_status": session_status,
            "as_of": local_now.isoformat(),
            "timezone": "UTC+09:00",
            "warning": "holiday_calendar_not_applied",
        }


def _object_schema(properties: dict[str, Any], *, required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": True,
    }


def _history_schema(*, cache: bool) -> dict[str, Any]:
    properties = {
        "symbol": {"type": "string"},
        "market": {"type": "string", "enum": ["domestic", "overseas"], "default": "domestic"},
        "exchange": {"type": "string"},
        "period_code": {"type": "string", "default": "D"},
        "adjusted_price": {"type": "boolean", "default": True},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
    }
    if cache:
        properties["refresh"] = {"type": "boolean", "default": False}
    return _object_schema(properties, required=("symbol",))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _read_message() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8").strip())


def _write_message(payload: dict[str, Any]) -> None:
    sys.stdout.buffer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def _success(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSON_RPC_VERSION, "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": JSON_RPC_VERSION, "id": message_id, "error": {"code": code, "message": message}}


def _format_tool_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _tool_result_text(tool_name, result)}],
        "structuredContent": result,
        "isError": False,
    }


def _tool_result_text(tool_name: str, result: dict[str, Any]) -> str:
    lines = [f"tool: {tool_name}"]
    for key in ("status", "market", "market_scope", "symbol", "exchange", "session_status", "fact_count"):
        value = result.get(key)
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    if "last_price" in result:
        lines.append(f"last_price: {result.get('last_price')}")
    if "candles" in result and isinstance(result["candles"], list):
        lines.append(f"candle_count: {len(result['candles'])}")
    if len(lines) == 1:
        lines.append(json.dumps(result, ensure_ascii=False))
    return "\n".join(lines)


def _handle_initialize(message_id: Any, params: dict[str, Any] | None) -> dict[str, Any]:
    return _success(
        message_id,
        {
            "protocolVersion": (params or {}).get("protocolVersion", "2025-06-18"),
            "serverInfo": SERVER_INFO,
            "capabilities": {"tools": {"listChanged": False}},
        },
    )


def _handle_tools_list(message_id: Any, registry: LeapsMarketDataToolRegistry) -> dict[str, Any]:
    return _success(message_id, {"tools": [tool.to_mcp_tool() for tool in registry.list_tool_definitions()]})


def _handle_tools_call(
    message_id: Any,
    registry: LeapsMarketDataToolRegistry,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    params = params or {}
    tool_name = params.get("name")
    arguments = params.get("arguments", {})
    if not isinstance(tool_name, str) or not tool_name:
        return _error(message_id, -32602, "Tool name is required.")
    if not isinstance(arguments, dict):
        return _error(message_id, -32602, "Tool arguments must be a JSON object.")
    try:
        result = registry.call_tool(tool_name, arguments)
    except UnknownToolError as exc:
        return _error(message_id, -32601, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _error(message_id, -32000, str(exc))
    return _success(message_id, _format_tool_result(tool_name, result))


def _log_trace(event_type: str, payload: Mapping[str, Any]) -> None:
    path = Path(os.getenv("LEAPS_MARKET_DATA_MCP_TRACE_LOG_PATH", str(DEFAULT_TRACE_LOG_PATH)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_type": event_type, "payload": dict(payload)}, ensure_ascii=False) + "\n")
    except OSError:
        return


def serve_stdio(registry: LeapsMarketDataToolRegistry | None = None) -> int:
    registry = registry or LeapsMarketDataToolRegistry.with_default_client()
    _log_trace("mcp_server_started", {"server_name": SERVER_INFO["name"], "transport": "stdio"})
    while True:
        message = _read_message()
        if message is None:
            _log_trace("mcp_eof", {})
            return 0
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        _log_trace("mcp_message_received", {"message_id": str(message_id), "method": str(method)})
        if method == "notifications/initialized":
            continue
        if method == "ping":
            if message_id is not None:
                _write_message(_success(message_id, {}))
            continue
        if method == "initialize":
            if message_id is not None:
                _write_message(_handle_initialize(message_id, params if isinstance(params, dict) else None))
            continue
        if method == "tools/list":
            if message_id is not None:
                _write_message(_handle_tools_list(message_id, registry))
            continue
        if method == "tools/call":
            if message_id is not None:
                _write_message(_handle_tools_call(message_id, registry, params if isinstance(params, dict) else None))
            continue
        if message_id is not None:
            _write_message(_error(message_id, -32601, f"Unsupported method '{method}'."))


def main() -> int:
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())

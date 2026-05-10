from __future__ import annotations

from leaps_quant_engine.mcp_market_data_stdio import (
    LeapsMarketDataToolRegistry,
    _handle_initialize,
    _handle_tools_call,
    _handle_tools_list,
)


class FakeKISClient:
    def __init__(self) -> None:
        self.calls = []

    def health_check(self):
        return {"status": "ok", "transport": "fake"}

    def call_operation(self, operation, arguments=None):
        self.calls.append((operation, dict(arguments or {})))
        if operation == "get_stock_price":
            return {"symbol": arguments["symbol"], "last_price": 70000, "volume": 1000}
        if operation == "get_daily_ohlcv":
            return {"symbol": arguments["symbol"], "candles": [{"date": "20260508", "close_price": 70000}]}
        raise AssertionError(operation)


def test_leaps_market_data_registry_lists_local_tools():
    registry = LeapsMarketDataToolRegistry(FakeKISClient())

    names = {tool.name for tool in registry.list_tool_definitions()}

    assert "health_check" in names
    assert "get_stock_price" in names
    assert "get_or_cache_daily_ohlcv" in names
    assert "build_whitelist_live_facts" in names


def test_leaps_market_data_registry_health_check_is_local():
    registry = LeapsMarketDataToolRegistry(FakeKISClient())

    result = registry.call_tool("health_check", {})

    assert result["server"] == "leaps-quant-market-data"
    assert result["kis"]["transport"] == "fake"


def test_overseas_daily_ohlcv_wrapper_sets_market_scope():
    client = FakeKISClient()
    registry = LeapsMarketDataToolRegistry(client)

    registry.call_tool("get_overseas_daily_ohlcv", {"symbol": "NVDA", "exchange": "NAS"})

    assert client.calls == [("get_daily_ohlcv", {"symbol": "NVDA", "exchange": "NAS", "market": "overseas"})]


def test_whitelist_live_facts_uses_quote_operation_per_symbol():
    client = FakeKISClient()
    registry = LeapsMarketDataToolRegistry(client)

    result = registry.call_tool(
        "build_whitelist_live_facts",
        {"market_scope": "domestic", "symbols": ["005930", "000660"], "max_symbols": 1},
    )

    assert result["fact_count"] == 1
    assert result["facts"][0]["symbol"] == "005930"
    assert client.calls == [("get_stock_price", {"market": "domestic", "symbol": "005930"})]


def test_mcp_json_rpc_handlers_return_tool_payloads():
    registry = LeapsMarketDataToolRegistry(FakeKISClient())

    initialized = _handle_initialize(1, {"protocolVersion": "2025-06-18"})
    listed = _handle_tools_list(2, registry)
    called = _handle_tools_call(3, registry, {"name": "health_check", "arguments": {}})

    assert initialized["result"]["serverInfo"]["name"] == "leaps-quant-market-data"
    assert listed["result"]["tools"]
    assert called["result"]["structuredContent"]["server"] == "leaps-quant-market-data"

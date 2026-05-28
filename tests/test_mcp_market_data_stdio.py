from __future__ import annotations

import leaps_quant_engine.mcp_market_data_stdio as mcp
from leaps_quant_engine.mcp_market_data_stdio import (
    LeapsMarketDataToolRegistry,
    _handle_initialize,
    _handle_tools_call,
    _handle_tools_list,
)
from leaps_quant_engine.settings import KISSettings


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


def test_leaps_market_data_registry_health_check_is_local(monkeypatch):
    monkeypatch.delenv("LEAPS_MARKET_DATA_MCP_BACKEND", raising=False)
    registry = LeapsMarketDataToolRegistry(FakeKISClient())

    result = registry.call_tool("health_check", {})

    assert result["server"] == "leaps-quant-market-data"
    assert result["backend"] == "gateway"
    assert result["kis"]["transport"] == "fake"


def test_default_mcp_client_uses_shared_gateway_by_default(monkeypatch, tmp_path):
    seen = {}

    class FakeGatewayClient:
        def __init__(self, *, base_url, rate_limit_per_second, cache_dir):
            seen["base_url"] = base_url
            seen["rate_limit_per_second"] = rate_limit_per_second
            seen["cache_dir"] = cache_dir

        def health_check(self):
            return {"status": "ok", "server": "fake-gateway"}

    monkeypatch.delenv("LEAPS_MARKET_DATA_MCP_BACKEND", raising=False)
    monkeypatch.setenv("LEAPS_KIS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("LEAPS_KIS_GATEWAY_BASE_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(mcp, "KISGatewayClient", FakeGatewayClient)
    monkeypatch.setattr(
        mcp,
        "load_kis_settings",
        lambda: KISSettings(app_key="key", app_secret="secret", rate_limit_per_second=50),
    )

    registry = LeapsMarketDataToolRegistry.with_default_client()
    result = registry.call_tool("health_check", {})

    assert result["backend"] == "gateway"
    assert result["kis"]["server"] == "fake-gateway"
    assert seen == {
        "base_url": "http://127.0.0.1:9999",
        "rate_limit_per_second": 18,
        "cache_dir": (tmp_path / "cache").resolve(),
    }


def test_default_mcp_client_allows_explicit_direct_backend(monkeypatch, tmp_path):
    seen = {}

    class FakeDirectClient:
        def __init__(self, *, settings, cache_dir, rate_limit_per_second):
            seen["settings"] = settings
            seen["rate_limit_per_second"] = rate_limit_per_second
            seen["cache_dir"] = cache_dir

        def health_check(self):
            return {"status": "ok", "transport": "direct"}

    settings = KISSettings(app_key="key", app_secret="secret", rate_limit_per_second=50)
    monkeypatch.setenv("LEAPS_MARKET_DATA_MCP_BACKEND", "direct")
    monkeypatch.setenv("LEAPS_KIS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(mcp, "KISDirectClient", FakeDirectClient)
    monkeypatch.setattr(mcp, "load_kis_settings", lambda: settings)

    registry = LeapsMarketDataToolRegistry.with_default_client()
    result = registry.call_tool("health_check", {})

    assert result["backend"] == "direct"
    assert result["kis"]["transport"] == "direct"
    assert seen == {
        "settings": settings,
        "rate_limit_per_second": 18,
        "cache_dir": (tmp_path / "cache").resolve(),
    }


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

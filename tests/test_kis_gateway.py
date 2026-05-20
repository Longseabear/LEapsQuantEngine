from __future__ import annotations

from fastapi.testclient import TestClient

from leaps_quant_engine.kis_gateway import KISGatewayClient, KISGatewayService, create_kis_gateway_app, fetch_kis_gateway_health
from leaps_quant_engine.settings import KISSettings


class _FakeKISClient:
    def __init__(self):
        self.settings = KISSettings(app_key="secret-app-key", app_secret="secret", rate_limit_per_second=18)
        self.calls = []

    def health_check(self):
        return {
            "status": "ok",
            "transport": "in_process_kis",
            "mock": False,
            "base_url": self.settings.base_url,
            "query_rate_limit_per_second": 18,
            "request_rate_limit_per_second": 18,
        }

    def call_operation(self, operation, arguments):
        self.calls.append((operation, dict(arguments)))
        return {"operation": operation, "arguments": dict(arguments)}


def test_kis_gateway_health_exposes_lane_without_secrets():
    service = KISGatewayService(_FakeKISClient())

    payload = service.health_check()

    assert payload["status"] == "ok"
    assert payload["server"] == "leaps-kis-gateway"
    assert payload["lane"]["query_rate_limit_per_second"] == 18
    assert payload["lane"]["request_rate_limit_per_second"] == 18
    assert "secret-app-key" not in str(payload)
    assert payload["lane"]["app_key_fingerprint"]


def test_kis_gateway_call_operation_updates_counters():
    client = _FakeKISClient()
    service = KISGatewayService(client)

    result = service.call_operation("get_stock_price", {"symbol": "005930"})
    payload = service.health_check()

    assert result["operation"] == "get_stock_price"
    assert client.calls == [("get_stock_price", {"symbol": "005930"})]
    assert payload["counters"]["total_calls"] == 1
    assert payload["counters"]["total_failures"] == 0
    assert payload["counters"]["calls_by_operation"] == {"get_stock_price": 1}


def test_fetch_kis_gateway_health_reads_json_payload(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok"}

    captured = {}

    def fake_get(url, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("leaps_quant_engine.kis_gateway.requests.get", fake_get)

    assert fetch_kis_gateway_health("http://127.0.0.1:8766/", timeout_seconds=1.5) == {"status": "ok"}
    assert captured == {"url": "http://127.0.0.1:8766/health", "timeout": 1.5}


def test_kis_gateway_fastapi_health_and_call_endpoints():
    client = _FakeKISClient()
    app = create_kis_gateway_app(KISGatewayService(client))
    test_client = TestClient(app)

    health = test_client.get("/health")
    call = test_client.post("/call", json={"operation": "get_stock_price", "arguments": {"symbol": "005930"}})

    assert health.status_code == 200
    assert health.json()["server"] == "leaps-kis-gateway"
    assert call.status_code == 200
    assert call.json()["result"] == {
        "operation": "get_stock_price",
        "arguments": {"symbol": "005930"},
    }


def test_kis_gateway_client_calls_http_gateway():
    class _Response:
        status_code = 200

        def json(self):
            return {"status": "ok", "result": {"price": 100}}

    class _Session:
        def __init__(self):
            self.calls = []

        def post(self, url, json, timeout):
            self.calls.append((url, json, timeout))
            return _Response()

    session = _Session()
    client = KISGatewayClient(base_url="http://127.0.0.1:8766/", session=session, rate_limit_per_second=100)

    result = client.call_tool("get_stock_price", {"symbol": "005930"})

    assert result == {"price": 100}
    assert session.calls == [
        (
            "http://127.0.0.1:8766/call",
            {"operation": "get_stock_price", "arguments": {"symbol": "005930"}},
            60.0,
        )
    ]

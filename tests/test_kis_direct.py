from leaps_quant_engine.adapters.kis_direct import KISDirectClient, KISDirectClientError, _TOKEN_CACHE
from leaps_quant_engine.settings import KISSettings


class _FakeResponse:
    def __init__(self, payload, *, status_code=200, headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.get_calls = []
        self.post_calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": dict(json or {}), "headers": dict(headers or {})})
        if url.endswith("/oauth2/tokenP"):
            return _FakeResponse({"access_token": "token-1", "expires_in": 3600})
        if url.endswith("/uapi/domestic-stock/v1/trading/order-cash"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "msg_cd": "ok",
                    "msg1": "accepted",
                    "output": {
                        "KRX_FWDG_ORD_ORGNO": "001",
                        "ODNO": "00012345",
                        "ORD_TMD": "093001",
                    },
                }
            )
        raise AssertionError(url)

    def get(self, url, *, headers=None, params=None, timeout=None):
        self.get_calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        if url.endswith("/uapi/domestic-stock/v1/quotations/inquire-daily-price"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "stck_bsop_date": "20260508",
                            "stck_oprc": "69000",
                            "stck_hgpr": "71000",
                            "stck_lwpr": "68000",
                            "stck_clpr": "70000",
                            "acml_vol": "1000",
                        }
                    ],
                }
            )
        raise AssertionError(url)


def _settings() -> KISSettings:
    return KISSettings(
        app_key="key",
        app_secret="secret",
        base_url="https://fake-kis.test",
        cano="12345678",
        account_product_code="01",
        mock=True,
    )


def test_direct_kis_domestic_market_order_sends_kis_market_payload(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "place_domestic_cash_order",
        {
            "side": "buy",
            "symbol": "005930",
            "quantity": 4,
            "price": 70000,
            "order_division": "13",
            "exchange_scope": "KRX",
        },
    )

    order_call = session.post_calls[-1]
    assert order_call["json"]["ORD_DVSN"] == "13"
    assert order_call["json"]["ORD_UNPR"] == "0"
    assert result["branch_no"] == "001"
    assert result["order_no"] == "00012345"


def test_direct_kis_daily_history_uses_local_file_cache(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)
    args = {
        "market": "domestic",
        "symbol": "005930",
        "period_code": "D",
        "adjusted_price": True,
        "start_date": "2026-05-01",
        "end_date": "2026-05-08",
        "refresh": True,
    }

    first = client.call_tool("get_or_cache_daily_ohlcv", args)
    session.get_calls.clear()
    second = client.call_tool("get_or_cache_daily_ohlcv", {**args, "refresh": False})

    assert first["candles"][0]["close_price"] == 70000
    assert second["candles"][0]["date"] == "20260508"
    assert session.get_calls == []


def test_direct_kis_limit_order_rejects_zero_price(tmp_path):
    _TOKEN_CACHE.clear()
    client = KISDirectClient(settings=_settings(), session=_FakeSession(), cache_dir=tmp_path)

    try:
        client.call_operation(
            "place_domestic_cash_order",
            {
                "side": "buy",
                "symbol": "005930",
                "quantity": 1,
                "price": 0,
                "order_division": "00",
            },
        )
    except KISDirectClientError as exc:
        assert "limit order price" in str(exc)
    else:
        raise AssertionError("limit order with zero price should fail")

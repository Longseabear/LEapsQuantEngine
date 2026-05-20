import requests

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
        if url.endswith("/uapi/overseas-stock/v1/trading/order"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "msg_cd": "ok",
                    "msg1": "accepted",
                    "output": {
                        "KRX_FWDG_ORD_ORGNO": "910",
                        "ODNO": "99012345",
                        "ORD_TMD": "093001",
                    },
                }
            )
        if url.endswith("/uapi/overseas-stock/v1/trading/order-rvsecncl"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "msg_cd": "ok",
                    "msg1": "accepted",
                    "output": {
                        "KRX_FWDG_ORD_ORGNO": "910",
                        "ODNO": "99012346",
                        "ORD_TMD": "093101",
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
        if url.endswith("/uapi/domestic-stock/v1/quotations/inquire-price"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": {
                        "hts_kor_isnm": "삼성전자",
                        "rprs_mrkt_kor_name": "KOSPI",
                        "stck_prpr": "268500",
                        "stck_sdpr": "268500",
                        "prdy_vrss": "0",
                        "prdy_ctrt": "0.00",
                        "stck_oprc": "0",
                        "stck_hgpr": "0",
                        "stck_lwpr": "0",
                        "acml_vol": "157",
                    },
                }
            )
        if url.endswith("/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"):
            output = {
                "aspr_acpt_hour": "084201",
                "stck_prpr": "",
                "total_askp_rsqn": "100",
                "total_bidp_rsqn": "120",
            }
            for level in range(1, 11):
                output[f"askp{level}"] = str(290000 + level * 100)
                output[f"askp_rsqn{level}"] = str(level)
                output[f"bidp{level}"] = str(289000 - level * 100)
                output[f"bidp_rsqn{level}"] = str(level + 1)
            return _FakeResponse({"rt_cd": "0", "output1": output})
        if url.endswith("/uapi/domestic-stock/v1/quotations/news-title"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "cntt_usiq_srno": "123",
                            "news_ofer_entp_code": "2",
                            "data_dt": "20260514",
                            "data_tm": "093001",
                            "hts_pbnt_titl_cntt": "삼성전자 실적 발표",
                            "news_lrdv_code": "01",
                            "dorg": "KIS",
                            "iscd1": "005930",
                            "iscd2": "",
                        },
                        {
                            "cntt_usiq_srno": "124",
                            "news_ofer_entp_code": "2",
                            "data_dt": "20260514",
                            "data_tm": "093201",
                            "hts_pbnt_titl_cntt": "시장 시황",
                            "news_lrdv_code": "02",
                            "dorg": "KIS",
                            "iscd1": "",
                        },
                    ],
                }
            )
        if url.endswith("/uapi/overseas-stock/v1/trading/inquire-present-balance"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "pdno": "SMH",
                            "prdt_name": "VanEck Semiconductor ETF",
                            "ovrs_excg_cd": "NASD",
                            "crcy_cd": "USD",
                            "cblc_qty13": "2",
                            "ccld_qty_smtl1": "4",
                            "ord_psbl_qty1": "4",
                            "thdt_buy_ccld_qty1": "2",
                            "thdt_sll_ccld_qty1": "0",
                            "avg_unpr3": "570.25",
                            "frcr_evlu_amt2": "2288.00",
                        }
                    ],
                    "output3": {
                        "wdrw_psbl_tot_amt": "3434.25",
                        "nxdy_frcr_drwg_psbl_amt": "3434.25",
                        "tot_dncl_amt": "3434.25",
                        "tot_asst_amt": "4578.25",
                        "evlu_amt_smtl_amt": "1144.00",
                    },
                }
            )
        if url.endswith("/uapi/overseas-stock/v1/trading/inquire-psamount"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": {
                        "tr_crcy_cd": "USD",
                        "ord_psbl_frcr_amt": "123.45",
                        "ovrs_ord_psbl_amt": "123.45",
                        "frcr_ord_psbl_amt1": "123.45",
                        "sll_ruse_psbl_amt": "0.00",
                        "echm_af_ord_psbl_amt": "123.45",
                        "echm_af_ord_psbl_qty": "1",
                        "max_ord_psbl_qty": "1",
                        "ord_psbl_qty": "1",
                        "exrt": "1466.00",
                    },
                }
            )
        if url.endswith("/uapi/overseas-stock/v1/trading/inquire-ccnl"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "odno": "0030295557",
                            "pdno": "SMH",
                            "prdt_name": "VANECK SEMICONDUCTOR",
                            "sll_buy_dvsn_cd": "02",
                            "ord_dt": "20260511",
                            "ord_tmd": "230211",
                            "ft_ord_qty": "1",
                            "ft_ord_unpr3": "570.89",
                            "ft_ccld_qty": "1",
                            "nccs_qty": "0",
                            "ft_ccld_unpr3": "570.44",
                            "ft_ccld_amt3": "570.44",
                            "ovrs_excg_cd": "NASD",
                            "tr_crcy_cd": "USD",
                        }
                    ],
                }
            )
        if url.endswith("/uapi/overseas-price/v1/quotations/news-title"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "outblock1": [
                        {
                            "info_gb": "1",
                            "news_key": "us-1",
                            "data_dt": "20260514",
                            "data_tm": "221604",
                            "class_cd": "01",
                            "class_name": "시장",
                            "source": "KIS",
                            "nation_cd": "US",
                            "exchange_cd": "NAS",
                            "symb": "NVDA",
                            "symb_name": "NVIDIA",
                            "title": "NVIDIA rises after earnings",
                        }
                    ],
                }
            )
        if url.endswith("/uapi/overseas-price/v1/quotations/brknews-title"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output": [
                        {
                            "cntt_usiq_srno": "brk-1",
                            "news_ofer_entp_code": "0",
                            "data_dt": "20260514",
                            "data_tm": "222153",
                            "hts_pbnt_titl_cntt": "해외 속보 제목",
                            "news_lrdv_code": "03",
                            "dorg": "KIS",
                            "iscd1": "SMH",
                            "kor_isnm1": "VanEck Semiconductor ETF",
                        }
                    ],
                }
            )
        if url.endswith("/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice"):
            return _FakeResponse(
                {
                    "rt_cd": "0",
                    "output2": [
                        {
                            "xymd": "20260515",
                            "xhms": "093000",
                            "open": "570.10",
                            "high": "571.00",
                            "low": "569.90",
                            "last": "570.44",
                            "evol": "1200",
                        },
                        {
                            "xymd": "20260515",
                            "xhms": "093100",
                            "open": "570.44",
                            "high": "571.20",
                            "low": "570.00",
                            "last": "571.00",
                            "evol": "850",
                        },
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


def test_direct_kis_overseas_minute_history_uses_local_file_cache(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)
    args = {
        "symbol": "SMH",
        "exchange": "NAS",
        "trade_date": "2026-05-15",
        "start_time": "09:30:00",
        "end_time": "09:30:00",
        "interval_minutes": 1,
        "refresh": True,
    }

    first = client.call_tool("get_or_cache_overseas_minute_bars", args)
    session.get_calls.clear()
    second = client.call_tool("get_or_cache_overseas_minute_bars", {**args, "refresh": False})

    assert first["candles"] == [
        {
            "date": "20260515",
            "time": "093000",
            "local_date": "20260515",
            "local_time": "093000",
            "open_price": 570.10,
            "high_price": 571.00,
            "low_price": 569.90,
            "close_price": 570.44,
            "volume": 1200,
        }
    ]
    assert second["candles"][0]["close_price"] == 570.44
    assert session.get_calls == []


def test_direct_kis_quote_retries_rate_limit_response(tmp_path, monkeypatch):
    class _RateLimitThenSuccessSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.quote_attempts = 0

        def get(self, url, *, headers=None, params=None, timeout=None):
            if url.endswith("/uapi/domestic-stock/v1/quotations/inquire-price"):
                self.quote_attempts += 1
                if self.quote_attempts == 1:
                    self.get_calls.append(
                        {"url": url, "headers": dict(headers or {}), "params": dict(params or {})}
                    )
                    return _FakeResponse(
                        {
                            "rt_cd": "1",
                            "msg_cd": "EGW00201",
                            "msg1": "초당 거래건수를 초과하였습니다.",
                        }
                    )
            return super().get(url, headers=headers, params=params, timeout=timeout)

    monkeypatch.setattr("leaps_quant_engine.adapters.kis_direct.time.sleep", lambda seconds: None)
    _TOKEN_CACHE.clear()
    session = _RateLimitThenSuccessSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation("get_stock_price", {"market": "domestic", "symbol": "005930"})

    assert session.quote_attempts == 2
    assert result["last_price"] == 289500


def test_direct_kis_client_clamps_real_query_and_request_lanes_to_18():
    settings = KISSettings(app_key="key", app_secret="secret", rate_limit_per_second=500)

    client = KISDirectClient.from_settings(settings)

    assert client.rate_limit_per_second == 18
    assert client.query_rate_limit_per_second == 18
    assert client.request_rate_limit_per_second == 18


def test_direct_kis_client_clamps_mock_query_and_request_lanes_to_1():
    settings = KISSettings(app_key="key", app_secret="secret", mock=True, rate_limit_per_second=500)

    client = KISDirectClient.from_settings(settings)

    assert client.rate_limit_per_second == 1
    assert client.query_rate_limit_per_second == 1
    assert client.request_rate_limit_per_second == 1


def test_direct_kis_order_retries_explicit_rate_limit_response(tmp_path, monkeypatch):
    class _RateLimitThenSuccessSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.order_attempts = 0

        def post(self, url, *, json=None, headers=None, timeout=None):
            if url.endswith("/uapi/domestic-stock/v1/trading/order-cash"):
                self.order_attempts += 1
                if self.order_attempts == 1:
                    self.post_calls.append({"url": url, "json": dict(json or {}), "headers": dict(headers or {})})
                    return _FakeResponse(
                        {
                            "rt_cd": "1",
                            "msg_cd": "EGW00201",
                            "msg1": "초당 거래건수를 초과하였습니다.",
                        }
                    )
            return super().post(url, json=json, headers=headers, timeout=timeout)

    monkeypatch.setattr("leaps_quant_engine.adapters.kis_direct.time.sleep", lambda seconds: None)
    _TOKEN_CACHE.clear()
    session = _RateLimitThenSuccessSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "place_domestic_cash_order",
        {
            "side": "buy",
            "symbol": "005930",
            "quantity": 1,
            "price": 70000,
            "order_division": "00",
        },
    )

    assert session.order_attempts == 2
    assert result["order_no"] == "00012345"


def test_direct_kis_domestic_order_falls_back_to_krx_when_sor_is_unavailable(tmp_path):
    class _SorUnavailableThenSuccessSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.order_attempts = 0

        def post(self, url, *, json=None, headers=None, timeout=None):
            if url.endswith("/uapi/domestic-stock/v1/trading/order-cash"):
                self.order_attempts += 1
                self.post_calls.append({"url": url, "json": dict(json or {}), "headers": dict(headers or {})})
                if json["EXCG_ID_DVSN_CD"] == "SOR":
                    return _FakeResponse(
                        {
                            "rt_cd": "1",
                            "msg_cd": "APBK3009",
                            "msg1": "SOR 시장에서 거래가 불가능한 종목입니다.",
                        }
                    )
                return _FakeResponse(
                    {
                        "rt_cd": "0",
                        "msg_cd": "ok",
                        "msg1": "accepted",
                        "output": {
                            "KRX_FWDG_ORD_ORGNO": "001",
                            "ODNO": "00054321",
                            "ORD_TMD": "093001",
                        },
                    }
                )
            return super().post(url, json=json, headers=headers, timeout=timeout)

    _TOKEN_CACHE.clear()
    session = _SorUnavailableThenSuccessSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "place_domestic_cash_order",
        {
            "side": "sell",
            "symbol": "036930",
            "quantity": 1,
            "price": 155700,
            "order_division": "00",
            "exchange_scope": "SOR",
        },
    )

    assert session.order_attempts == 2
    assert session.post_calls[-2]["json"]["EXCG_ID_DVSN_CD"] == "SOR"
    assert session.post_calls[-1]["json"]["EXCG_ID_DVSN_CD"] == "KRX"
    assert result["order_no"] == "00054321"
    assert result["exchange_scope"] == "KRX"


def test_direct_kis_order_timeout_is_not_retried_to_avoid_duplicate_order(tmp_path):
    class _TimeoutOrderSession(_FakeSession):
        def __init__(self):
            super().__init__()
            self.order_attempts = 0

        def post(self, url, *, json=None, headers=None, timeout=None):
            if url.endswith("/uapi/domestic-stock/v1/trading/order-cash"):
                self.order_attempts += 1
                raise requests.Timeout("timed out")
            return super().post(url, json=json, headers=headers, timeout=timeout)

    _TOKEN_CACHE.clear()
    session = _TimeoutOrderSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    try:
        client.call_operation(
            "place_domestic_cash_order",
            {
                "side": "buy",
                "symbol": "005930",
                "quantity": 1,
                "price": 70000,
                "order_division": "00",
            },
        )
    except KISDirectClientError as exc:
        assert "reconcile broker order status before retrying" in str(exc)
    else:
        raise AssertionError("order timeout should require reconciliation before retry")
    assert session.order_attempts == 1


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


def test_direct_kis_domestic_after_hours_close_order_sends_order_division_06(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    client.call_operation(
        "place_domestic_cash_order",
        {
            "side": "sell",
            "symbol": "005930",
            "quantity": 4,
            "price": 70000,
            "order_division": "06",
            "exchange_scope": "KRX",
        },
    )

    order_call = session.post_calls[-1]
    assert order_call["json"]["ORD_DVSN"] == "06"
    assert order_call["json"]["ORD_UNPR"] == "70000"


def test_direct_kis_domestic_after_hours_limit_order_rejects_zero_price(tmp_path):
    _TOKEN_CACHE.clear()
    client = KISDirectClient(settings=_settings(), session=_FakeSession(), cache_dir=tmp_path)

    try:
        client.call_operation(
            "place_domestic_cash_order",
            {
                "side": "sell",
                "symbol": "005930",
                "quantity": 1,
                "price": 0,
                "order_division": "07",
            },
        )
    except KISDirectClientError as exc:
        assert "limit order price" in str(exc)
    else:
        raise AssertionError("after-hours limit order with zero price should fail")


def test_direct_kis_overseas_limit_order_uses_order_endpoint_and_exchange_alias(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "place_overseas_stock_order",
        {
            "side": "buy",
            "exchange": "NAS",
            "symbol": "SMH",
            "quantity": 1,
            "price": 569.55,
            "order_division": "00",
        },
    )

    order_call = session.post_calls[-1]
    assert order_call["url"].endswith("/uapi/overseas-stock/v1/trading/order")
    assert order_call["headers"]["tr_id"] == "VTTT1002U"
    assert order_call["json"]["OVRS_EXCG_CD"] == "NASD"
    assert order_call["json"]["PDNO"] == "SMH"
    assert order_call["json"]["ORD_QTY"] == "1"
    assert order_call["json"]["OVRS_ORD_UNPR"] == "569.55"
    assert result["market"] == "overseas"
    assert result["branch_no"] == "910"


def test_direct_kis_overseas_cancel_uses_revise_cancel_endpoint(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "revise_or_cancel_overseas_stock_order",
        {
            "exchange": "AMS",
            "symbol": "XLE",
            "original_order_no": "99012345",
            "rvse_cncl_dvsn_cd": "02",
            "quantity": 1,
            "price": 0,
        },
    )

    order_call = session.post_calls[-1]
    assert order_call["url"].endswith("/uapi/overseas-stock/v1/trading/order-rvsecncl")
    assert order_call["headers"]["tr_id"] == "VTTT1004U"
    assert order_call["json"]["OVRS_EXCG_CD"] == "AMEX"
    assert order_call["json"]["ORGN_ODNO"] == "99012345"
    assert result["order_no"] == "99012346"


def test_direct_kis_overseas_balance_uses_present_balance_endpoint(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    balance = client.call_operation("get_account_balance_summary", {"market": "overseas"})
    holdings = client.call_operation("get_account_holdings", {"market": "overseas"})

    balance_call = [call for call in session.get_calls if call["url"].endswith("/inquire-present-balance")][0]
    assert balance_call["headers"]["tr_id"] == "VTRP6504R"
    assert balance_call["params"]["WCRC_FRCR_DVSN_CD"] == "02"
    assert balance["account_type"] == "overseas_stock"
    assert balance["currency"] == "USD"
    assert balance["cash_balance"] == 123.45
    assert balance["present_cash_balance"] == 3434.25
    assert balance["buying_power"]["orderable_foreign_amount"] == 123.45
    assert holdings["holdings"][0]["symbol"] == "SMH"
    assert holdings["holdings"][0]["market"] == "US"
    assert holdings["holdings"][0]["holding_quantity"] == 4
    assert holdings["holdings"][0]["current_quantity"] == 4
    assert holdings["holdings"][0]["settled_quantity"] == 2
    assert holdings["holdings"][0]["orderable_quantity"] == 4
    assert holdings["holdings"][0]["today_buy_quantity"] == 2
    assert holdings["holdings"][0]["today_sell_quantity"] == 0


def test_direct_kis_overseas_execution_history_uses_ccnl_endpoint(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    history = client.call_operation(
        "get_account_execution_history",
        {
            "market": "overseas",
            "start_date": "2026-05-11",
            "end_date": "2026-05-11",
            "side": "buy",
        },
    )

    call = session.get_calls[-1]
    row = history["executions"][0]
    assert call["url"].endswith("/uapi/overseas-stock/v1/trading/inquire-ccnl")
    assert call["headers"]["tr_id"] == "VTTS3035R"
    assert call["params"]["SLL_BUY_DVSN"] == "02"
    assert row["order_id"] == "0030295557"
    assert row["symbol"] == "SMH"
    assert row["market"] == "US"
    assert row["execution_quantity"] == 1
    assert row["execution_price"] == 570.44


def test_direct_kis_domestic_quote_uses_orderbook_when_current_price_is_reference_price(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_tool("get_stock_price", {"market": "domestic", "symbol": "005930"})

    assert result["last_price"] == 289500
    assert result["price_source"] == "orderbook_best_bid_ask_mid"
    assert result["live_price_usable"] is True
    assert result["orderbook"]["best_ask"] == 290100
    assert any("inquire-asking-price-exp-ccn" in call["url"] for call in session.get_calls)


def test_direct_kis_domestic_news_titles_normalizes_rows(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "get_domestic_news_titles",
        {"symbol": "005930", "date": "2026-05-14", "time": "09:30", "max_results": 1},
    )

    call = session.get_calls[-1]
    assert call["url"].endswith("/uapi/domestic-stock/v1/quotations/news-title")
    assert call["headers"]["tr_id"] == "FHKST01011800"
    assert call["params"]["FID_INPUT_ISCD"] == "005930"
    assert call["params"]["FID_INPUT_DATE_1"] == "20260514"
    assert call["params"]["FID_INPUT_HOUR_1"] == "093000"
    assert result["market"] == "domestic"
    assert result["count"] == 1
    assert result["raw_count"] == 2
    assert result["items"][0]["title"] == "삼성전자 실적 발표"
    assert result["items"][0]["symbols"] == ["005930"]


def test_direct_kis_overseas_news_titles_normalizes_rows(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation(
        "get_overseas_news_titles",
        {"nation_code": "US", "exchange": "NAS", "symbol": "nvda"},
    )

    call = session.get_calls[-1]
    assert call["url"].endswith("/uapi/overseas-price/v1/quotations/news-title")
    assert call["headers"]["tr_id"] == "HHPSTH60100C1"
    assert call["params"]["NATION_CD"] == "US"
    assert call["params"]["EXCHANGE_CD"] == "NAS"
    assert call["params"]["SYMB"] == "NVDA"
    assert result["market"] == "overseas"
    assert result["items"][0]["id"] == "us-1"
    assert result["items"][0]["symbol"] == "NVDA"
    assert result["items"][0]["title"] == "NVIDIA rises after earnings"


def test_direct_kis_overseas_breaking_news_titles_uses_defaults(tmp_path):
    _TOKEN_CACHE.clear()
    session = _FakeSession()
    client = KISDirectClient(settings=_settings(), session=session, cache_dir=tmp_path)

    result = client.call_operation("get_overseas_breaking_news_titles", {})

    call = session.get_calls[-1]
    assert call["url"].endswith("/uapi/overseas-price/v1/quotations/brknews-title")
    assert call["headers"]["tr_id"] == "FHKST01011801"
    assert call["params"]["FID_NEWS_OFER_ENTP_CODE"] == "0"
    assert call["params"]["FID_COND_SCR_DIV_CODE"] == "11801"
    assert result["market"] == "overseas"
    assert result["items"][0]["id"] == "brk-1"
    assert result["items"][0]["symbols"] == ["SMH"]
    assert result["items"][0]["symbol_names"] == ["VanEck Semiconductor ETF"]

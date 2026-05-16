import json

from leaps_quant_engine.adapters.kis import MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.models import DataResolution, Symbol


class _FakeQuoteClient:
    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.calls = 0
        self.fail = False

    def call_tool(self, tool, arguments=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("EGW00201: 초당 거래건수를 초과하였습니다.")
        return {
            "last_price": 70000,
            "open_price": 69900,
            "high_price": 70100,
            "low_price": 69800,
            "volume": 1234,
            "price_source": "fake-live",
        }


def test_live_quote_provider_uses_fresh_file_cache_before_kis_call(tmp_path):
    client = _FakeQuoteClient(tmp_path)
    provider = MarketDataEngineLiveQuoteProvider(
        client=client,
        live_quote_cache_max_age_seconds=90,
        prefer_live_quote_cache=True,
    )
    symbol = Symbol("005930", "KRX")

    first = provider.get_latest_bar(symbol)
    client.fail = True
    second = provider.get_latest_bar(symbol)

    assert client.calls == 1
    assert first.close == 70000
    assert first.resolution == DataResolution.LIVE.value
    assert second.close == 70000
    assert second.resolution == DataResolution.LIVE.value
    assert second.metadata["live_quote_cache_status"] == "hit"
    assert second.metadata["price_source"] == "fake-live"


def test_live_quote_provider_coerces_legacy_any_cache_to_live_resolution(tmp_path):
    client = _FakeQuoteClient(tmp_path)
    provider = MarketDataEngineLiveQuoteProvider(
        client=client,
        live_quote_cache_max_age_seconds=90,
        prefer_live_quote_cache=True,
    )
    symbol = Symbol("005930", "KRX")

    provider.get_latest_bar(symbol)
    cache_path = tmp_path / "live-quotes" / "latest.json"
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload[symbol.key]["resolution"] = DataResolution.ANY.value
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    client.fail = True
    cached = provider.get_latest_bar(symbol)

    assert cached.resolution == DataResolution.LIVE.value

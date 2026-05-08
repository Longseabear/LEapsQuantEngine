from leaps_quant_engine.adapters.kis import MarketDataEngineClient, MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.settings import load_kis_settings


def test_market_data_engine_rate_limit_is_configurable_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAPS_ENV_FILE", raising=False)
    monkeypatch.delenv("STOCKPROGRAM_ENV_FILE", raising=False)
    monkeypatch.delenv("MARKET_DATA_ENGINE_ENV_FILE", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("MARKET_DATA_ENGINE_RATE_LIMIT_PER_SECOND", "18")

    settings = load_kis_settings(tmp_path / "missing.env")
    client = MarketDataEngineClient.from_settings(settings)

    assert settings.market_data_engine_rate_limit_per_second == 18
    assert client.rate_limit_per_second == 18


def test_market_data_live_provider_clamps_override_to_kis_limit(monkeypatch, tmp_path):
    monkeypatch.delenv("STOCKPROGRAM_ENV_FILE", raising=False)
    monkeypatch.delenv("MARKET_DATA_ENGINE_ENV_FILE", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("LEAPS_ENV_FILE", str(tmp_path / "missing.env"))

    provider = MarketDataEngineLiveQuoteProvider.from_env(rate_limit_per_second=30)

    assert provider.client.rate_limit_per_second == 20

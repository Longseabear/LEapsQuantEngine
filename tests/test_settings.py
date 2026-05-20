from leaps_quant_engine.adapters.kis import MarketDataEngineClient, MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.settings import kis_account_env_prefix, load_kis_settings, load_kis_settings_for_account


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


def test_kis_direct_real_rate_limit_default_matches_kis_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAPS_ENV_FILE", raising=False)
    monkeypatch.delenv("STOCKPROGRAM_ENV_FILE", raising=False)
    monkeypatch.delenv("MARKET_DATA_ENGINE_ENV_FILE", raising=False)
    monkeypatch.delenv("KIS_API_RATE_LIMIT_PER_SECOND", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")

    settings = load_kis_settings(tmp_path / "missing.env")

    assert settings.rate_limit_per_second == 18


def test_kis_direct_mock_rate_limit_default_matches_kis_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("LEAPS_ENV_FILE", raising=False)
    monkeypatch.delenv("STOCKPROGRAM_ENV_FILE", raising=False)
    monkeypatch.delenv("MARKET_DATA_ENGINE_ENV_FILE", raising=False)
    monkeypatch.delenv("KIS_API_RATE_LIMIT_PER_SECOND", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("KIS_MOCK", "true")

    settings = load_kis_settings(tmp_path / "missing.env")

    assert settings.mock is True
    assert settings.rate_limit_per_second == 1


def test_load_kis_settings_falls_back_to_default_scoped_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_CANO", raising=False)
    monkeypatch.setenv("KIS_APP_SECRET", "base-secret")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_KEY", "default-key")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_SECRET", "default-secret")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_CANO", "default-cano")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_ACNT_PRDT_CD", "01")

    settings = load_kis_settings(tmp_path / "missing.env")

    assert settings.app_key == "default-key"
    assert settings.app_secret == "default-secret"
    assert settings.cano == "default-cano"
    assert settings.account_product_code == "01"


def test_market_data_live_provider_clamps_override_to_kis_limit(monkeypatch, tmp_path):
    monkeypatch.delenv("STOCKPROGRAM_ENV_FILE", raising=False)
    monkeypatch.delenv("MARKET_DATA_ENGINE_ENV_FILE", raising=False)
    monkeypatch.setenv("KIS_APP_KEY", "key")
    monkeypatch.setenv("KIS_APP_SECRET", "secret")
    monkeypatch.setenv("LEAPS_ENV_FILE", str(tmp_path / "missing.env"))

    provider = MarketDataEngineLiveQuoteProvider.from_env(rate_limit_per_second=30)

    assert provider.client.rate_limit_per_second == 18


def test_kis_account_env_prefix_matches_stockprogram_normalization():
    assert kis_account_env_prefix("us-growth") == "KIS_ACCOUNT_US_GROWTH"
    assert kis_account_env_prefix("kis-overseas") == "KIS_ACCOUNT_KIS_OVERSEAS"


def test_load_kis_settings_for_account_uses_scoped_credentials_and_account(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "base-key")
    monkeypatch.setenv("KIS_APP_SECRET", "base-secret")
    monkeypatch.setenv("KIS_CANO", "base-cano")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_APP_KEY", "us-key")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_APP_SECRET", "us-secret")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_CANO", "us-cano")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_ACNT_PRDT_CD", "22")

    settings = load_kis_settings_for_account(
        "kis-overseas",
        metadata={"kis_account_id": "us-growth"},
        env_file=tmp_path / "missing.env",
    )

    assert settings.app_key == "us-key"
    assert settings.app_secret == "us-secret"
    assert settings.cano == "us-cano"
    assert settings.account_product_code == "22"


def test_load_kis_settings_for_account_allows_scoped_credentials_without_base_key(monkeypatch, tmp_path):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_CANO", raising=False)
    monkeypatch.setenv("KIS_APP_SECRET", "base-secret")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_KEY", "default-key")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_SECRET", "default-secret")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_CANO", "default-cano")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_ACNT_PRDT_CD", "01")

    settings = load_kis_settings_for_account(
        "kis-domestic",
        metadata={"kis_account_id": "default"},
        env_file=tmp_path / "missing.env",
    )

    assert settings.app_key == "default-key"
    assert settings.app_secret == "default-secret"
    assert settings.cano == "default-cano"
    assert settings.account_product_code == "01"


def test_load_kis_settings_for_account_can_split_credentials_from_account_number(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_APP_KEY", "base-key")
    monkeypatch.setenv("KIS_APP_SECRET", "base-secret")
    monkeypatch.setenv("KIS_CANO", "base-cano")
    monkeypatch.setenv("KIS_ACNT_PRDT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_KEY", "default-key")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_APP_SECRET", "default-secret")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_CANO", "default-cano")
    monkeypatch.setenv("KIS_ACCOUNT_DEFAULT_ACNT_PRDT_CD", "01")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_APP_KEY", "wrong-us-key")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_APP_SECRET", "wrong-us-secret")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_CANO", "us-cano")
    monkeypatch.setenv("KIS_ACCOUNT_US_GROWTH_ACNT_PRDT_CD", "22")

    settings = load_kis_settings_for_account(
        "kis-overseas",
        metadata={"kis_account_id": "us-growth", "credential_account_id": "default"},
        env_file=tmp_path / "missing.env",
    )

    assert settings.app_key == "default-key"
    assert settings.app_secret == "default-secret"
    assert settings.cano == "us-cano"
    assert settings.account_product_code == "22"

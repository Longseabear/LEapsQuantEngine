import json
from datetime import datetime

from leaps_quant_engine import cli
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.universe.loader import parse_universe_definition


def test_cli_indicators_backtest_once_outputs_configured_symbols(monkeypatch, capsys):
    engine = IndicatorEngine()
    engine.register_universe(
        "swing-kor",
        parse_universe_definition(
            {
                "id": "test",
                "market": "KRX",
                "symbols": ["005930"],
                "indicators": [{"name": "sma_2_close", "type": "sma", "period": 2}],
            }
        ),
    )
    monkeypatch.setattr(cli, "build_indicator_engine_from_file", lambda path: engine)

    exit_code = cli.main(["indicators-backtest-once", "sample_swing_kor_pipeline.json", "--sleeve-id", "swing-kor"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "sleeve_id": "swing-kor",
        "symbols": ["KRX:005930"],
        "values": {"KRX:005930": {"sma_2_close": None}},
    }


def test_cli_indicators_kis_once_updates_from_provider(monkeypatch, capsys):
    class FakeProvider:
        @classmethod
        def from_env(cls):
            return cls()

    called = {"updated": False}

    class FakeIndicatorEngine:
        def warm_up_from_provider(self, sleeve_id, provider, start=None, end=None):
            called["warmup"] = (sleeve_id, start, end)

        def update_from_provider(self, provider):
            called["updated"] = True

        def symbols_for_sleeve(self, sleeve_id):
            return [type("S", (), {"key": "KRX:005930"})()]

        def values_for(self, sleeve_id, symbols, ready_only=False):
            return {"KRX:005930": {}}

    monkeypatch.setattr(cli, "KISBrokerEngineMarketDataProvider", FakeProvider)
    monkeypatch.setattr(cli, "build_indicator_engine_from_file", lambda path: FakeIndicatorEngine())

    exit_code = cli.main(
        [
            "indicators-kis-once",
            "sample_swing_kor_pipeline.json",
            "--sleeve-id",
            "swing-kor",
            "--warmup-start",
            "2026-05-01",
            "--warmup-end",
            "2026-05-07",
        ]
    )

    assert exit_code == 0
    assert called["updated"]
    assert called["warmup"][0] == "swing-kor"
    assert json.loads(capsys.readouterr().out)["symbols"] == ["KRX:005930"]


def test_cli_benchmark_indicators_daily_outputs_report(monkeypatch, capsys):
    class FakeProvider:
        @classmethod
        def from_env(cls):
            return cls()

    universe = object()
    captured = {}

    def fake_run_daily_indicator_benchmark(universe_arg, provider_arg, **kwargs):
        captured["universe"] = universe_arg
        captured["provider"] = provider_arg
        captured.update(kwargs)
        return {
            "universe_size": 200,
            "updated_symbol_count": 200,
            "measurement_scope": "IndicatorEngine.on_data",
        }

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeProvider)
    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "run_daily_indicator_benchmark", fake_run_daily_indicator_benchmark)

    exit_code = cli.main(
        [
            "benchmark-indicators-daily",
            "configs/universes/benchmark_kor_200.json",
            "--sleeve-id",
            "benchmark-kor",
            "--start",
            "2026-01-01",
            "--end",
            "2026-05-01",
            "--refresh-history",
            "--include-daily",
        ]
    )

    assert exit_code == 0
    assert captured["universe"] is universe
    assert isinstance(captured["provider"], FakeProvider)
    assert captured["sleeve_id"] == "benchmark-kor"
    assert captured["start"] == datetime(2026, 1, 1)
    assert captured["end"] == datetime(2026, 5, 1)
    assert captured["refresh_history"] is True
    assert captured["include_daily"] is True
    assert captured["source"] == "kis-cache"
    assert json.loads(capsys.readouterr().out)["updated_symbol_count"] == 200


def test_cli_live_indicators_once_builds_exchange_map_and_outputs_report(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "us-live",
            "market": "US",
            "symbols": [{"ticker": "NVDA", "exchange": "NAS"}],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    captured = {}

    class FakeProvider:
        @classmethod
        def from_env(cls, exchange_by_symbol=None, rate_limit_per_second=None):
            captured["exchange_by_symbol"] = exchange_by_symbol
            captured["rate_limit_per_second"] = rate_limit_per_second
            return cls()

    def fake_run_live_indicator_snapshot(universe_arg, provider_arg, **kwargs):
        captured["universe"] = universe_arg
        captured["provider"] = provider_arg
        captured.update(kwargs)
        return {
            "universe_size": 1,
            "updated_symbol_count": 1,
            "failed_symbol_count": 0,
        }

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeProvider)
    monkeypatch.setattr(cli, "run_live_indicator_snapshot", fake_run_live_indicator_snapshot)

    exit_code = cli.main(
        [
            "live-indicators-once",
            "configs/universes/us_live_smoke.json",
            "--sleeve-id",
            "us-live",
            "--min-success",
            "1",
            "--include-failures",
        ]
    )

    assert exit_code == 0
    assert captured["exchange_by_symbol"] == {"US:NVDA": "NAS", "NVDA": "NAS"}
    assert captured["universe"] is universe
    assert isinstance(captured["provider"], FakeProvider)
    assert captured["sleeve_id"] == "us-live"
    assert captured["source"] == "market-data-engine"
    assert captured["min_success"] == 1
    assert captured["rate_limit_per_second"] is None
    assert captured["include_failures"] is True
    assert json.loads(capsys.readouterr().out)["updated_symbol_count"] == 1


def test_cli_live_indicators_once_accepts_rate_limit_override(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "us-live",
            "market": "US",
            "symbols": [{"ticker": "NVDA", "exchange": "NAS"}],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    captured = {}

    class FakeProvider:
        @classmethod
        def from_env(cls, exchange_by_symbol=None, rate_limit_per_second=None):
            captured["rate_limit_per_second"] = rate_limit_per_second
            return cls()

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeProvider)
    monkeypatch.setattr(
        cli,
        "run_live_indicator_snapshot",
        lambda *args, **kwargs: {"updated_symbol_count": 1},
    )

    exit_code = cli.main(
        [
            "live-indicators-once",
            "configs/universes/us_live_smoke.json",
            "--sleeve-id",
            "us-live",
            "--rate-limit-per-second",
            "20",
        ]
    )

    assert exit_code == 0
    assert captured["rate_limit_per_second"] == 20
    assert json.loads(capsys.readouterr().out)["updated_symbol_count"] == 1


def test_cli_global_logging_options_are_configured(monkeypatch, capsys, tmp_path):
    captured = {}

    def fake_configure_logging(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(cli, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(cli, "run_once_from_file", lambda path, time: [])

    log_file = tmp_path / "server.jsonl"
    exit_code = cli.main(
        [
            "--log-level",
            "INFO",
            "--log-file",
            str(log_file),
            "--log-json",
            "run-once",
            "sample_swing_kor_pipeline.json",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "level": "INFO",
        "log_file": log_file,
        "json_logs": True,
        "max_bytes": 10_000_000,
        "backup_count": 5,
    }
    assert json.loads(capsys.readouterr().out) == []

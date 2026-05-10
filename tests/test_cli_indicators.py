import json
from datetime import datetime
from types import SimpleNamespace

from leaps_quant_engine import cli
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar
from leaps_quant_engine.models import Symbol
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
    class FakeKISProvider:
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

    monkeypatch.setattr(cli, "KISBrokerEngineMarketDataProvider", FakeKISProvider)
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


def test_cli_framework_backtest_daily_outputs_framework_report(monkeypatch, capsys):
    class FakeKISProvider:
        @classmethod
        def from_env(cls):
            return cls()

    class FakeFinanceProvider:
        pass

    class FakeAlphaLoader:
        def load(self, path):
            captured["alpha_path"] = path
            return SimpleNamespace(
                model=object(),
                alpha_id="fake-alpha",
                version="1.0",
                path=path,
                content_hash="abc",
            )

    captured = {}
    universe = object()

    def fake_run_framework_backtest(universe_arg, provider_arg, **kwargs):
        captured["universe"] = universe_arg
        captured["provider"] = provider_arg
        captured.update(kwargs)
        return SimpleNamespace(
            to_report=lambda include_orders=True, include_insights=False, include_selection_details=None: {
                "framework_cycle_count": 3,
                "insight_count": 2,
                "order_count": 1,
                "include_orders": include_orders,
                "include_insights": include_insights,
                "include_selection_details": include_selection_details,
            }
        )

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeKISProvider)
    monkeypatch.setattr(cli, "FinanceDataReaderMarketDataProvider", FakeFinanceProvider)
    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "PythonAlphaLoader", FakeAlphaLoader)
    monkeypatch.setattr(cli, "run_framework_backtest", fake_run_framework_backtest)

    exit_code = cli.main(
        [
            "framework-backtest-daily",
            "configs/universes/swing_kor_core.json",
            "examples/alpha/price_above_sma_alpha.py",
            "--sleeve-id",
            "swing-kor",
            "--start",
            "2026-01-01",
            "--end",
            "2026-05-08",
            "--cash",
            "1000000",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["universe"] is universe
    assert isinstance(captured["provider"], FakeFinanceProvider)
    assert captured["sleeve_id"] == "swing-kor"
    assert captured["portfolio"].cash == 1_000_000
    assert captured["start"] == datetime(2026, 1, 1)
    assert captured["end"] == datetime(2026, 5, 8)
    assert captured["refresh_history"] is False
    assert captured["framework_runner"].sleeve_id == "swing-kor"
    output = json.loads(capsys.readouterr().out)
    assert output["framework_cycle_count"] == 3
    assert output["include_orders"] is False
    assert output["include_insights"] is False
    assert output["include_selection_details"] is False
    assert output["source"] == "finance-datareader"
    assert output["alpha"]["alpha_id"] == "fake-alpha"


def test_cli_warmup_indicators_daily_outputs_report(monkeypatch, capsys):
    class FakeProvider:
        @classmethod
        def from_env(cls):
            return cls()

    universe = object()
    captured = {}

    def fake_run_daily_indicator_warmup(universe_arg, provider_arg, **kwargs):
        captured["universe"] = universe_arg
        captured["provider"] = provider_arg
        captured.update(kwargs)
        return SimpleNamespace(
            report=SimpleNamespace(
                to_dict=lambda include_symbols=True: {
                    "requested_symbol_count": 200,
                    "ready_symbol_count": 200,
                    "is_ready": True,
                    "include_symbols": include_symbols,
                }
            )
        )

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeProvider)
    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "run_daily_indicator_warmup", fake_run_daily_indicator_warmup)

    exit_code = cli.main(
        [
            "warmup-indicators-daily",
            "configs/universes/benchmark_kor_200.json",
            "--sleeve-id",
            "benchmark-kor",
            "--start",
            "2026-01-01",
            "--end",
            "2026-05-01",
            "--refresh-history",
            "--extra-bars",
            "2",
            "--min-ready-ratio",
            "0.9",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["universe"] is universe
    assert isinstance(captured["provider"], FakeProvider)
    assert captured["sleeve_id"] == "benchmark-kor"
    assert captured["start"] == datetime(2026, 1, 1)
    assert captured["end"] == datetime(2026, 5, 1)
    assert captured["refresh_history"] is True
    assert captured["source"] == "kis-cache"
    assert captured["policy"].extra_bars == 2
    assert captured["policy"].min_ready_ratio == 0.9
    output = json.loads(capsys.readouterr().out)
    assert output["ready_symbol_count"] == 200
    assert output["include_symbols"] is False


def test_cli_select_active_universe_outputs_selection_report(monkeypatch, capsys):
    class FakeProvider:
        @classmethod
        def from_env(cls):
            return cls()

    universe = parse_universe_definition(
        {
            "id": "coarse",
            "market": "KRX",
            "symbols": ["000001"],
            "indicators": [{"name": "identity_close", "type": "identity", "period": 1}],
        }
    )
    captured = {}

    class FakeIndicatorEngine:
        def snapshot(self, sleeve_id, universe_id=None):
            captured["snapshot"] = (sleeve_id, universe_id)
            return object()

    class FakeWarmupResult:
        indicator_engine = FakeIndicatorEngine()
        report = SimpleNamespace(to_dict=lambda include_symbols=True: {"warmup": True})

    class FakeSelection:
        def to_dict(self, include_candidates=True):
            return {
                "selected_count": 1,
                "live_symbols": ["KRX:000001", "KRX:005930"],
                "include_candidates": include_candidates,
            }

    class FakeModel:
        def __init__(self, **kwargs):
            captured["model_kwargs"] = kwargs

        def select(self, context):
            captured["context"] = context
            return FakeSelection()

    def fake_warmup(universe_arg, provider_arg, **kwargs):
        captured["universe"] = universe_arg
        captured["provider"] = provider_arg
        captured.update(kwargs)
        return FakeWarmupResult()

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeProvider)
    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "run_daily_indicator_warmup", fake_warmup)
    monkeypatch.setattr(cli, "MomentumUniverseSelectionModel", FakeModel)

    exit_code = cli.main(
        [
            "select-active-universe",
            "configs/universes/benchmark_kor_200.json",
            "--sleeve-id",
            "benchmark-kor",
            "--top-n",
            "60",
            "--held",
            "005930",
            "--open-order",
            "KRX:000660",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["universe"] is universe
    assert isinstance(captured["provider"], FakeProvider)
    assert captured["sleeve_id"] == "benchmark-kor"
    assert captured["model_kwargs"]["max_active_symbols"] == 60
    assert captured["context"].held_symbols == (Symbol("005930", "KRX"),)
    assert captured["context"].open_order_symbols == (Symbol("000660", "KRX"),)
    assert captured["snapshot"] == ("benchmark-kor", "coarse")
    output = json.loads(capsys.readouterr().out)
    assert output["selection"]["include_candidates"] is False
    assert output["selection"]["live_symbols"] == ["KRX:000001", "KRX:005930"]


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


def test_cli_fine_universe_refresh_outputs_fine_cache_report(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "us-coarse",
            "market": "US",
            "symbols": [
                {"ticker": "NVDA", "exchange": "NAS"},
                {"ticker": "MSFT", "exchange": "NAS"},
            ],
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

        def get_latest_bar(self, symbol):
            return Bar(symbol=symbol, time=datetime(2026, 5, 9), open=1, high=1, low=1, close=1, volume=1)

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeProvider)

    exit_code = cli.main(
        [
            "fine-universe-refresh",
            "configs/universes/us_live_smoke.json",
            "--rate-limit-per-second",
            "20",
            "--max-symbols",
            "1",
            "--include-entries",
        ]
    )

    assert exit_code == 0
    assert captured["exchange_by_symbol"] == {"US:NVDA": "NAS", "NVDA": "NAS", "US:MSFT": "NAS", "MSFT": "NAS"}
    assert captured["rate_limit_per_second"] == 20
    output = json.loads(capsys.readouterr().out)
    assert output["refresh"]["requested_symbol_count"] == 1
    assert output["refresh"]["updated_symbol_count"] == 1
    assert output["fine_universe"]["symbols"] == ["US:NVDA"]
    assert "US:NVDA" in output["entries"]


def test_cli_snapshot_worker_run_outputs_report(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "us-live",
            "market": "US",
            "symbols": [{"ticker": "NVDA", "exchange": "NAS"}],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    captured = {}

    class FakeLiveProvider:
        @classmethod
        def from_env(cls, exchange_by_symbol=None, rate_limit_per_second=None):
            captured["exchange_by_symbol"] = exchange_by_symbol
            captured["rate_limit_per_second"] = rate_limit_per_second
            return cls()

        def get_latest_bar(self, symbol):
            captured.setdefault("fine_refresh_symbols", []).append(symbol.key)
            return Bar(symbol=symbol, time=datetime(2026, 5, 9), open=1, high=1, low=1, close=1, volume=1)

        def get_latest_bar(self, symbol):
            captured.setdefault("fine_refresh_symbols", []).append(symbol.key)
            return Bar(symbol=symbol, time=datetime(2026, 5, 9), open=1, high=1, low=1, close=1, volume=1)

    class FakeHistoryProvider:
        @classmethod
        def from_env(cls):
            return cls()

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs

        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return SimpleNamespace(
                to_dict=lambda include_warmup_symbols=True, include_failures=True: {
                    "cycles_completed": 2,
                    "include_warmup_symbols": include_warmup_symbols,
                    "include_failures": include_failures,
                }
            )

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeLiveProvider)
    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeHistoryProvider)
    monkeypatch.setattr(cli, "BackgroundSnapshotWorker", FakeWorker)

    exit_code = cli.main(
        [
            "snapshot-worker-run",
            "configs/universes/us_live_smoke.json",
            "--sleeve-id",
            "us-live",
            "--cycles",
            "2",
            "--interval-seconds",
            "0",
            "--min-success",
            "1",
            "--rate-limit-per-second",
            "20",
            "--skip-warmup",
            "--warmup-start",
            "2026-01-01",
            "--warmup-end",
            "2026-05-01",
            "--refresh-history",
            "--extra-bars",
            "3",
            "--min-ready-ratio",
            "0.8",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["exchange_by_symbol"] == {"US:NVDA": "NAS", "NVDA": "NAS"}
    assert captured["rate_limit_per_second"] == 20
    assert captured["worker_kwargs"]["universe"] is universe
    assert captured["worker_kwargs"]["sleeve_id"] == "us-live"
    assert captured["worker_kwargs"]["source"] == "market-data-engine"
    assert captured["worker_kwargs"]["history_source"] == "kis-cache"
    assert captured["worker_kwargs"]["min_success"] == 1
    assert captured["worker_kwargs"]["interval_seconds"] == 0.0
    assert captured["worker_kwargs"]["warmup_policy"].extra_bars == 3
    assert captured["worker_kwargs"]["warmup_policy"].min_ready_ratio == 0.8
    assert captured["run_kwargs"]["max_cycles"] == 2
    assert captured["run_kwargs"]["warmup"] is False
    assert captured["run_kwargs"]["warmup_start"] == datetime(2026, 1, 1)
    assert captured["run_kwargs"]["warmup_end"] == datetime(2026, 5, 1)
    assert captured["run_kwargs"]["refresh_history"] is True
    output = json.loads(capsys.readouterr().out)
    assert output["cycles_completed"] == 2
    assert output["include_warmup_symbols"] is False
    assert output["include_failures"] is False


def test_cli_active_snapshot_worker_run_updates_only_active_universe(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "us-coarse",
            "market": "US",
            "symbols": [
                {"ticker": "NVDA", "exchange": "NAS"},
                {"ticker": "MSFT", "exchange": "NAS"},
                {"ticker": "IBM", "exchange": "NYS"},
            ],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    captured = {}

    class FakeLiveProvider:
        @classmethod
        def from_env(cls, exchange_by_symbol=None, rate_limit_per_second=None):
            captured["exchange_by_symbol"] = exchange_by_symbol
            captured["rate_limit_per_second"] = rate_limit_per_second
            return cls()

        def get_latest_bar(self, symbol):
            captured.setdefault("fine_refresh_symbols", []).append(symbol.key)
            return Bar(symbol=symbol, time=datetime(2026, 5, 9), open=1, high=1, low=1, close=1, volume=1)

    class FakeHistoryProvider:
        @classmethod
        def from_env(cls):
            return cls()

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs

        def run(self, **kwargs):
            captured["run_kwargs"] = kwargs
            return SimpleNamespace(
                to_dict=lambda include_warmup_symbols=True, include_failures=True: {
                    "cycles_completed": 1,
                    "worker_symbols": [symbol.key for symbol in captured["worker_kwargs"]["universe"].symbols],
                    "include_warmup_symbols": include_warmup_symbols,
                    "include_failures": include_failures,
                }
            )

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeLiveProvider)
    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeHistoryProvider)
    monkeypatch.setattr(cli, "BackgroundSnapshotWorker", FakeWorker)

    exit_code = cli.main(
        [
            "active-snapshot-worker-run",
            "configs/universes/us_live_smoke.json",
            "--sleeve-id",
            "us-live",
            "--top-n",
            "1",
            "--fine-refresh",
            "--held",
            "IBM",
            "--cycles",
            "1",
            "--interval-seconds",
            "0",
            "--skip-worker-warmup",
            "--rate-limit-per-second",
            "20",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["rate_limit_per_second"] == 20
    assert captured["exchange_by_symbol"] == {
        "US:NVDA": "NAS",
        "NVDA": "NAS",
        "US:MSFT": "NAS",
        "MSFT": "NAS",
        "US:IBM": "NYS",
        "IBM": "NYS",
    }
    assert captured["fine_refresh_symbols"] == ["US:NVDA", "US:MSFT", "US:IBM"]
    assert captured["fine_refresh_symbols"] == ["US:NVDA", "US:MSFT", "US:IBM"]
    assert [symbol.key for symbol in captured["worker_kwargs"]["universe"].symbols] == ["US:NVDA", "US:IBM"]
    assert captured["run_kwargs"]["warmup"] is False
    output = json.loads(capsys.readouterr().out)
    assert output["fine_refresh"]["updated_symbol_count"] == 3
    assert output["selection"]["selected_symbols"] == ["US:NVDA"]
    assert output["selection"]["forced_symbols"] == ["US:IBM"]
    assert output["worker"]["worker_symbols"] == ["US:NVDA", "US:IBM"]


def test_cli_alpha_run_snapshot_loads_python_alpha_and_outputs_insights(monkeypatch, capsys):
    universe = parse_universe_definition(
        {
            "id": "alpha-live",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    captured = {}

    class FakeLiveProvider:
        @classmethod
        def from_env(cls, exchange_by_symbol=None, rate_limit_per_second=None):
            captured["rate_limit_per_second"] = rate_limit_per_second
            return cls()

    class FakeHistoryProvider:
        @classmethod
        def from_env(cls):
            return cls()

    class FakeAlphaLoader:
        def load(self, path):
            captured["alpha_path"] = path
            return SimpleNamespace(
                model=object(),
                alpha_id="loaded-alpha",
                version="1.0",
                path=path,
                content_hash="abc",
            )

    class FakeRuntime:
        def __init__(self):
            captured["runtime_created"] = True

        def stage(self, models, validation_context=None):
            captured["staged_models"] = models
            captured["validation_context"] = validation_context

        def run(self, context, activate_pending=True, publish_active=True):
            captured["run_context"] = context
            captured["activate_pending"] = activate_pending
            captured["publish_active"] = publish_active
            return SimpleNamespace(
                to_dict=lambda: {
                    "batch_id": "insights-test",
                    "insight_count": 1,
                    "insights": [],
                }
            )

    class FakeStore:
        def active(self):
            return object()

    class FakeWorker:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs
            self.stores_by_sleeve = {"alpha-sleeve": FakeStore()}

        def warm_up(self, **kwargs):
            captured["warmup_kwargs"] = kwargs

        def run_once(self):
            captured["run_once"] = True
            return SimpleNamespace(to_dict=lambda include_failures=True: {"cycle_index": 1})

    class FakeSnapshotContext:
        @classmethod
        def from_indicator_snapshot(cls, snapshot):
            captured["snapshot_for_context"] = snapshot
            return "context"

    monkeypatch.setattr(cli, "load_universe_definition", lambda path: universe)
    monkeypatch.setattr(cli, "MarketDataEngineLiveQuoteProvider", FakeLiveProvider)
    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeHistoryProvider)
    monkeypatch.setattr(cli, "PythonAlphaLoader", FakeAlphaLoader)
    monkeypatch.setattr(cli, "AlphaRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "BackgroundSnapshotWorker", FakeWorker)
    monkeypatch.setattr(cli, "SnapshotContext", FakeSnapshotContext)

    exit_code = cli.main(
        [
            "alpha-run-snapshot",
            "configs/universes/swing_kor_core.json",
            "examples/alpha/price_above_sma_alpha.py",
            "--sleeve-id",
            "alpha-sleeve",
            "--min-success",
            "1",
            "--rate-limit-per-second",
            "20",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["rate_limit_per_second"] == 20
    assert captured["worker_kwargs"]["sleeve_id"] == "alpha-sleeve"
    assert captured["warmup_kwargs"]["refresh_history"] is False
    assert captured["run_once"] is True
    assert captured["staged_models"] == [captured["staged_models"][0]]
    assert captured["validation_context"] == "context"
    assert captured["run_context"] == "context"
    output = json.loads(capsys.readouterr().out)
    assert output["alpha"]["alpha_id"] == "loaded-alpha"
    assert output["cycle"]["cycle_index"] == 1
    assert output["insights"]["insight_count"] == 1


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

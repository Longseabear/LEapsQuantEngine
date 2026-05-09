import json
from datetime import datetime
from types import SimpleNamespace

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies, bootstrap_sleeve_runtime
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot


class FakeLiveProvider:
    def __init__(self, bars_by_key):
        self.bars_by_key = bars_by_key
        self.calls = []

    def get_latest_bar(self, symbol):
        self.calls.append(symbol.key)
        return self.bars_by_key[symbol.key]

    def get_history(self, symbol, *, start=None, end=None):
        return []


class FakeHistoryProvider:
    def get_latest_bar(self, symbol):
        return Bar(symbol, datetime(2026, 5, 9), 1, 1, 1, 1, 1)

    def get_history(self, symbol, *, start=None, end=None):
        return []


class FakeAlphaModel:
    alpha_id = "fake-alpha"
    version = "1.0"

    def generate(self, context):
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(context.symbol_keys[0]),
                direction=InsightDirection.UP,
                generated_at=datetime(2026, 5, 9),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason="runtime_bootstrap",
            )
        ]


class FakeAlphaLoader:
    def __init__(self):
        self.paths = []

    def load(self, path):
        self.paths.append(path)
        return SimpleNamespace(
            model=FakeAlphaModel(),
            alpha_id="fake-alpha",
            version="1.0",
            path=path,
            content_hash="abc",
        )


def _bar(symbol: Symbol, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        time=datetime(2026, 5, 9, 9, 30),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
    )


def _write_universe(path):
    path.write_text(
        json.dumps(
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
        ),
        encoding="utf-8",
    )


def _write_runtime_config(path, universe_path):
    path.write_text(
        json.dumps(
            {
                "runtime_id": "live-us-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "market_data": {
                    "provider": "market-data-engine",
                    "history_provider": "kis-cache",
                    "rate_limit_per_second": 20,
                },
                "sleeves": [
                    {
                        "sleeve_id": "us-live",
                        "cash": 100_000,
                        "universe": {
                            "coarse_path": str(universe_path),
                            "fine": {
                                "enabled": True,
                                "max_symbols": 2,
                                "max_age_seconds": 300,
                            },
                            "active": {
                                "max_symbols": 1,
                                "selection_model": "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
                            },
                        },
                        "indicators": {
                            "warmup_enabled": False,
                        },
                        "alpha": {
                            "modules": [{"ref": "alpha.py"}],
                        },
                        "worker": {
                            "cycle_interval_seconds": 0,
                            "min_success": 2,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_bootstrap_sleeve_runtime_builds_active_worker_from_config(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(config_path, universe_path)
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    alpha_loader = FakeAlphaLoader()
    provider_calls = {}

    def live_provider_factory(universe, rate_limit_per_second):
        provider_calls["universe_id"] = universe.id
        provider_calls["rate_limit_per_second"] = rate_limit_per_second
        return live_provider

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=live_provider_factory,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=alpha_loader,
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )

    assert provider_calls == {"universe_id": "us-coarse", "rate_limit_per_second": 20}
    assert alpha_loader.paths == [tmp_path / "alpha.py"]
    assert runtime.fine_refresh_report is not None
    assert runtime.fine_refresh_report.updated_symbol_count == 2
    assert runtime.active_result.selection.selected_symbols == (Symbol("NVDA", "US"),)
    assert runtime.active_result.selection.forced_symbols == (Symbol("IBM", "US"),)
    assert runtime.worker.universe.symbol_keys == ("US:NVDA", "US:IBM")
    assert runtime.worker.min_success == 2
    assert runtime.worker.interval_seconds == 0
    assert runtime.worker.alpha_runtime is None
    assert runtime.framework_runner.alpha_runtime.active_alpha_ids() == ("fake-alpha",)
    assert runtime.portfolio.cash == 100_000


def test_bootstrapped_runtime_can_run_one_worker_cycle(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(config_path, universe_path)
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )

    report = runtime.run_once()

    assert report.runtime_id == "live-us-test"
    assert report.config_version.startswith("sha256:")
    assert report.worker.cycles_completed == 1
    assert report.worker.warmup is None
    assert report.worker.cycles[0].updated_symbol_count == 2
    assert report.worker.cycles[0].insight_count == 0
    assert report.framework is not None
    assert report.framework.new_insight_batch.insight_count == 1
    assert len(report.framework.order_intents) == 1
    payload = report.to_dict()
    assert payload["selection"]["live_symbols"] == ["US:NVDA", "US:IBM"]
    assert payload["worker"]["cycles"][0]["insight_count"] == 0
    assert payload["framework"]["new_insights"]["insight_count"] == 1
    assert len(payload["framework"]["order_intents"]) == 1

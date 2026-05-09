import json
import logging
from datetime import datetime
from types import SimpleNamespace

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel
from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits
from leaps_quant_engine.models import Bar, OrderSide, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio, StaticPortfolioProvider
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies, bootstrap_sleeve_runtime
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


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


class FakePortfolioModelLoader:
    def __init__(self):
        self.calls = []

    def load(self, ref, *, parameters=None):
        params = dict(parameters or {})
        self.calls.append((ref, params))
        return SimpleNamespace(
            model=EqualWeightPortfolioConstructionModel(
                max_portfolio_pct=float(params.get("max_portfolio_pct", 1.0))
            ),
            ref=ref,
            parameters=params,
            model_name="EqualWeightPortfolioConstructionModel",
        )


class FakeRiskModelLoader:
    def __init__(self):
        self.calls = []

    def load(self, ref, *, parameters=None):
        params = dict(parameters or {})
        self.calls.append((ref, params))
        return SimpleNamespace(
            model=BasicRiskManagementModel(
                limits=RiskLimits(
                    long_only=bool(params.get("long_only", True)),
                    max_position_pct=float(params.get("max_position_pct", 1.0)),
                    cash_buffer_pct=float(params.get("cash_buffer_pct", 0.0)),
                )
            ),
            ref=ref,
            parameters=params,
            model_name="BasicRiskManagementModel",
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


def _write_runtime_config(
    path,
    universe_path,
    *,
    sleeve_id="us-live",
    workspace_path=None,
    cash=100_000,
    min_order_notional=1000,
    worker_min_success=2,
    account_store_path=None,
):
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
                        "sleeve_id": sleeve_id,
                        **({"workspace_path": str(workspace_path)} if workspace_path is not None else {}),
                        "cash": cash,
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
                        "portfolio": {
                            "module": "portfolio.py",
                            **({"account_store_path": str(account_store_path)} if account_store_path is not None else {}),
                            "params": {
                                "max_portfolio_pct": 0.5,
                            },
                            "rebalance": {
                                "cash_reserve_pct": 0.1,
                                "min_order_notional": min_order_notional,
                            },
                        },
                        "risk": {
                            "module": "risk.py",
                            "params": {
                                "long_only": True,
                                "max_position_pct": 0.4,
                                "cash_buffer_pct": 0.05,
                            },
                        },
                        "worker": {
                            "cycle_interval_seconds": 0,
                            "min_success": worker_min_success,
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
    portfolio_model_loader = FakePortfolioModelLoader()
    risk_model_loader = FakeRiskModelLoader()
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
            portfolio_model_loader=portfolio_model_loader,
            risk_model_loader=risk_model_loader,
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )

    assert provider_calls == {"universe_id": "us-coarse", "rate_limit_per_second": 20}
    assert alpha_loader.paths == [tmp_path / "alpha.py"]
    assert portfolio_model_loader.calls == [(str(tmp_path / "portfolio.py"), {"max_portfolio_pct": 0.5})]
    assert risk_model_loader.calls == [
        (str(tmp_path / "risk.py"), {"long_only": True, "max_position_pct": 0.4, "cash_buffer_pct": 0.05})
    ]
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.cash_reserve_pct == 0.1
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.min_order_notional == 1000
    assert runtime.framework_runner.risk_model.limits.max_position_pct == 0.4
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
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
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
    assert report.portfolio_state is not None
    assert report.portfolio_state.pending.order_intent_count == 1
    payload = report.to_dict()
    assert payload["selection"]["live_symbols"] == ["US:NVDA", "US:IBM"]
    assert payload["worker"]["cycles"][0]["insight_count"] == 0
    assert payload["framework"]["new_insights"]["insight_count"] == 1
    assert len(payload["framework"]["order_intents"]) == 1
    assert payload["portfolio_state"]["pending"]["order_intent_count"] == 1


def test_runtime_fetches_current_portfolio_for_leaps_sleeve(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        sleeve_id="LEaps",
        cash=1_000,
        min_order_notional=0,
        worker_min_success=1,
    )
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    held = Symbol("NVDA", "US")
    provider = StaticPortfolioProvider(
        portfolios={
            "LEaps": Portfolio(
                cash=500,
                holdings={held.key: Holding(held, quantity=7, average_price=90.0)},
            )
        }
    )

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            portfolio_provider=provider,
        ),
        held_symbols=(held,),
    )

    report = runtime.run_once()

    assert runtime.sleeve_id == "LEaps"
    assert runtime.portfolio.cash == 500
    assert runtime.portfolio.quantity(held) == 7
    assert report.framework is not None
    assert report.framework.portfolio_target_batch.metadata["portfolio_equity"] == 1_200
    assert report.portfolio_state is not None
    assert report.portfolio_state.current.cash == 500
    assert report.portfolio_state.current.equity == 1_200
    plan = report.framework.portfolio_target_batch.plans[0]
    assert plan.current_quantity == 7


def test_runtime_uses_virtual_sleeve_account_store_from_config(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "runtime" / "virtual_accounts.json"
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        sleeve_id="LEaps",
        cash=1_000,
        min_order_notional=0,
        worker_min_success=1,
        account_store_path=account_store_path,
    )
    symbol = Symbol("NVDA", "US")
    store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 1_000})
    store.initialize_sleeve("LEaps", cash=500)
    store.apply_fill(
        VirtualFillEvent(
            fill_id="fill-1",
            order_id="order-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=2,
            fill_price=100.0,
            filled_at=datetime(2026, 5, 9, 9, 0),
        )
    )
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [symbol, Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({item.key: _bar(item, 100 + index) for index, item in enumerate(symbols)})

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
        ),
        held_symbols=(symbol,),
    )
    report = runtime.run_once()

    assert isinstance(runtime.portfolio_provider, VirtualSleeveAccountStore)
    assert runtime.portfolio.cash == 300
    assert runtime.portfolio.quantity(symbol) == 2
    assert report.portfolio_state is not None
    assert report.portfolio_state.current.cash == 300


def test_runtime_logs_agent_readable_engine_status(tmp_path, caplog):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(config_path, universe_path, sleeve_id="LEaps", worker_min_success=1)
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    caplog.set_level(logging.INFO, logger="leaps_quant_engine.agent_status")
    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
        ),
    )

    runtime.run_once()

    status_records = [
        record
        for record in caplog.records
        if record.name == "leaps_quant_engine.agent_status"
    ]
    assert len(status_records) == 1
    status = status_records[0].engine_status
    assert status["event"] == "engine_status"
    assert status["sleeve_id"] == "LEaps"
    assert status["snapshot"]["status"] == "fresh"
    assert status["framework"]["ran"] is True
    assert status["framework"]["order_intent_count"] == 1
    assert status["portfolio_engine_state"]["pending"]["order_intent_count"] == 1
    assert "engine_status" in status_records[0].getMessage()


def test_runtime_resolves_strategy_modules_from_sleeve_workspace(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    workspace_path = tmp_path / "sleeves" / "LEaps"
    workspace_path.mkdir(parents=True)
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        sleeve_id="LEaps",
        workspace_path=workspace_path,
        worker_min_success=1,
    )
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    alpha_loader = FakeAlphaLoader()
    portfolio_model_loader = FakePortfolioModelLoader()
    risk_model_loader = FakeRiskModelLoader()

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=alpha_loader,
            portfolio_model_loader=portfolio_model_loader,
            risk_model_loader=risk_model_loader,
        ),
    )

    assert runtime.sleeve_config.workspace_path == workspace_path
    assert alpha_loader.paths == [workspace_path / "alpha.py"]
    assert portfolio_model_loader.calls == [(str(workspace_path / "portfolio.py"), {"max_portfolio_pct": 0.5})]
    assert risk_model_loader.calls == [
        (str(workspace_path / "risk.py"), {"long_only": True, "max_position_pct": 0.4, "cash_buffer_pct": 0.05})
    ]

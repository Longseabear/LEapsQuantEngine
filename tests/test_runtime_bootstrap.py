import json
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.execution import ImmediateExecutionModel
from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel
from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits
from leaps_quant_engine.models import Bar, OrderSide, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio, StaticPortfolioProvider
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies, bootstrap_sleeve_runtime
from leaps_quant_engine.runtime_bootstrap import _FallbackHistoryProvider
from leaps_quant_engine.runtime_bootstrap import _confirmed_daily_warmup_end
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore
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
    def __init__(self, history_by_key=None):
        self.history_by_key = history_by_key or {}

    def get_latest_bar(self, symbol):
        return Bar(symbol, datetime(2026, 5, 9), 1, 1, 1, 1, 1)

    def get_history(self, symbol, *, start=None, end=None):
        return list(self.history_by_key.get(symbol.key, []))


class FailingHistoryProvider:
    def get_latest_bar(self, symbol):
        raise RuntimeError("primary unavailable")

    def get_history(self, symbol, *, start=None, end=None):
        raise RuntimeError("primary unavailable")


def test_live_runtime_preselect_warmup_ends_at_previous_confirmed_daily_bar():
    snapshot = SimpleNamespace(config=SimpleNamespace(mode="live", timezone="Asia/Seoul"))

    warmup_end = _confirmed_daily_warmup_end(snapshot, now=datetime(2026, 5, 15, 12, 30))

    assert warmup_end.date() == date(2026, 5, 14)


def test_backtest_runtime_preselect_warmup_keeps_default_end():
    snapshot = SimpleNamespace(config=SimpleNamespace(mode="backtest", timezone="Asia/Seoul"))

    assert _confirmed_daily_warmup_end(snapshot, now=datetime(2026, 5, 15, 12, 30)) is None


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


class AllSymbolsAlphaModel:
    alpha_id = "all-symbols"
    version = "1.0"

    def generate(self, context):
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason="runtime_bootstrap_selected_input",
            )
            for symbol_key in context.symbol_keys
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


class AllSymbolsAlphaLoader:
    def load(self, path):
        return SimpleNamespace(
            model=AllSymbolsAlphaModel(),
            alpha_id="all-symbols",
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


class FakeExecutionModelLoader:
    def __init__(self):
        self.calls = []

    def load(self, ref, *, parameters=None):
        params = dict(parameters or {})
        self.calls.append((ref, params))
        return SimpleNamespace(
            model=ImmediateExecutionModel(),
            ref=ref,
            parameters=params,
            model_name="ImmediateExecutionModel",
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


def _daily_bar(symbol: Symbol, day: int, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        time=datetime(2026, 5, day),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000 + day,
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
    reused_target_churn_guard=False,
    reused_target_churn_equity_bps=0.0,
    worker_min_success=2,
    account_store_path=None,
    broker_account_store_path=None,
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
                **(
                    {
                        "broker_accounts": [
                            {
                                "account_id": "kis-test",
                                "market_scope": "overseas",
                                "currency": "USD",
                                "account_store_path": str(broker_account_store_path),
                            }
                        ]
                    }
                    if broker_account_store_path is not None
                    else {}
                ),
                "sleeves": [
                    {
                        "sleeve_id": sleeve_id,
                        **({"workspace_path": str(workspace_path)} if workspace_path is not None else {}),
                        **(
                            {"broker_account_id": "kis-test", "broker_account_routes": {"overseas": "kis-test"}}
                            if broker_account_store_path is not None
                            else {}
                        ),
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
                                "reused_target_churn_guard": reused_target_churn_guard,
                                "reused_target_churn_equity_bps": reused_target_churn_equity_bps,
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
                        "execution": {
                            "module": "execution.py",
                            "params": {},
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
    execution_model_loader = FakeExecutionModelLoader()
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
            execution_model_loader=execution_model_loader,
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )

    assert provider_calls == {"universe_id": "us-coarse", "rate_limit_per_second": 20}
    assert alpha_loader.paths == [tmp_path / "alpha.py"]
    assert portfolio_model_loader.calls == [(str(tmp_path / "portfolio.py"), {"max_portfolio_pct": 0.5})]
    assert risk_model_loader.calls == [
        (str(tmp_path / "risk.py"), {"long_only": True, "max_position_pct": 0.4, "cash_buffer_pct": 0.05})
    ]
    assert execution_model_loader.calls == [(str(tmp_path / "execution.py"), {})]


def test_runtime_active_universe_startup_only_persists_without_refresh(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(config_path, universe_path)
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    runtime_state = InMemoryRuntimeStateStore()

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
            runtime_state_store=runtime_state,
        ),
    )

    refreshed = runtime.refresh_active_universe_if_due(as_of=datetime(2026, 5, 9, 9, 0))

    assert refreshed is False
    records = runtime_state.entries(model_id="engine-universe-selection", namespace="active_universe")
    assert len(records) == 1
    assert records[0].value["cadence"] == "startup_only"
    assert records[0].value["symbol_keys"] == ["US:NVDA"]


def test_runtime_active_universe_once_per_day_refreshes_worker_universe(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        reused_target_churn_guard=True,
        reused_target_churn_equity_bps=5.0,
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sleeves"][0]["universe"]["active"]["cadence"] = "once_per_day"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    runtime_state = InMemoryRuntimeStateStore()

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
            runtime_state_store=runtime_state,
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )

    assert runtime.refresh_active_universe_if_due(as_of=datetime(2026, 5, 9, 9, 0)) is True
    assert runtime.worker.universe.symbol_keys == ("US:NVDA", "US:IBM")
    assert runtime.refresh_active_universe_if_due(as_of=datetime(2026, 5, 9, 10, 0)) is False
    assert runtime.refresh_active_universe_if_due(as_of=datetime(2026, 5, 10, 9, 0)) is True
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.cash_reserve_pct == 0.1
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.min_order_notional == 1000
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.reused_target_churn_guard is True
    assert runtime.framework_runner.portfolio_engine.rebalance_policy.reused_target_churn_equity_bps == 5.0
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
            execution_model_loader=FakeExecutionModelLoader(),
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
    assert payload["engine_status"]["event"] == "engine_status"
    assert payload["engine_status"]["framework"]["order_intent_count"] == 1


def test_runtime_wires_active_selection_symbols_into_alpha_context(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_path)
    _write_runtime_config(config_path, universe_path, worker_min_success=1, min_order_notional=0)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sleeves"][0]["universe"]["active"]["selection_models"] = [
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
        "leaps_quant_engine.universe.selection:MomentumUniverseSelectionModel",
    ]
    payload["sleeves"][0]["alpha"]["input_selections"] = {
        "all-symbols": "static-top-n",
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=AllSymbolsAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
        ),
        held_symbols=(Symbol("IBM", "US"),),
    )
    report = runtime.run_once()

    assert list(runtime.active_result.selection.selections) == ["static-top-n", "momentum-active-selection"]
    assert runtime.active_result.selection.live_symbols == (Symbol("NVDA", "US"), Symbol("IBM", "US"))
    assert report.framework is not None
    assert [insight.symbol.key for insight in report.framework.new_insight_batch.insights] == ["US:NVDA"]


def test_runtime_forces_portfolio_holdings_into_operational_alpha_input(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    selector_path = tmp_path / "operational_selector.py"
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        sleeve_id="LEaps",
        worker_min_success=1,
        min_order_notional=0,
    )
    selector_path.write_text(
        "\n".join(
            [
                "from dataclasses import dataclass",
                "from leaps_quant_engine.universe.selection import UniverseSelectionCandidate, build_universe_selection_result",
                "",
                "@dataclass(frozen=True, slots=True)",
                "class OperationalSymbolsSelectionModel:",
                "    selection_id = 'leaps-operational-symbols'",
                "",
                "    def select(self, context):",
                "        selected = context.forced_symbols",
                "        candidates = {",
                "            symbol.key: UniverseSelectionCandidate(",
                "                symbol=symbol,",
                "                score=None,",
                "                selected=True,",
                "                forced=True,",
                "                reasons=('operational_symbol',),",
                "            )",
                "            for symbol in selected",
                "        }",
                "        return build_universe_selection_result(",
                "            context, selected, selection_id=self.selection_id, candidates=candidates, rejected={}",
                "        )",
            ]
        ),
        encoding="utf-8",
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sleeves"][0]["universe"]["active"]["selection_models"] = [
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
        f"{selector_path}:OperationalSymbolsSelectionModel",
    ]
    payload["sleeves"][0]["alpha"]["input_selections"] = {
        "all-symbols": "leaps-operational-symbols",
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    held = Symbol("IBM", "US")
    provider = StaticPortfolioProvider(
        portfolios={
            "LEaps": Portfolio(
                cash=500,
                holdings={held.key: Holding(held, quantity=3, average_price=90.0)},
            )
        }
    )
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), held]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=AllSymbolsAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
            portfolio_provider=provider,
        ),
    )
    report = runtime.run_once()

    assert runtime.active_result.selection.forced_symbols == (held,)
    operational = runtime.active_result.selection.selections["leaps-operational-symbols"]
    assert operational.selected_symbols == (held,)
    assert report.framework is not None
    assert [insight.symbol for insight in report.framework.new_insight_batch.insights] == [held]


def test_bootstrap_warms_indicators_before_indicator_based_active_selection(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    selector_path = tmp_path / "selector.py"
    _write_universe(universe_path)
    universe_payload = json.loads(universe_path.read_text(encoding="utf-8"))
    universe_payload["indicators"] = [
        {"name": "close", "type": "close", "period": 1},
        {"name": "momentum_2_close", "type": "momentum", "period": 2, "field": "close"},
    ]
    universe_path.write_text(json.dumps(universe_payload), encoding="utf-8")
    selector_path.write_text(
        """
from leaps_quant_engine.universe.selection import UniverseSelectionCandidate, build_universe_selection_result


class WarmupReadySelectionModel:
    selection_id = "warmup-ready"

    def select(self, context):
        candidates = {}
        rejected = {}
        scored = []
        for symbol in context.universe.symbols:
            if context.indicator_snapshot is None:
                rejected[symbol.key] = ("missing_indicator_snapshot",)
                continue
            momentum = context.indicator_snapshot.value(symbol.key, "momentum_2_close")
            if momentum is None:
                rejected[symbol.key] = ("missing_momentum_2_close",)
                continue
            scored.append((momentum, symbol))
        selected = tuple(symbol for _, symbol in sorted(scored, key=lambda item: item[0], reverse=True)[:1])
        selected_keys = {symbol.key for symbol in selected}
        for score, symbol in scored:
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=score,
                selected=symbol.key in selected_keys,
                reasons=("warmup_ready",),
            )
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )
""",
        encoding="utf-8",
    )
    _write_runtime_config(config_path, universe_path, worker_min_success=1, min_order_notional=0)
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["sleeves"][0]["universe"]["fine"]["enabled"] = False
    payload["sleeves"][0]["universe"]["active"]["selection_models"] = [
        f"{selector_path}:WarmupReadySelectionModel"
    ]
    payload["sleeves"][0]["indicators"] = {
        "warmup_enabled": True,
        "extra_bars": 0,
        "min_ready_ratio": 1.0,
    }
    payload["sleeves"][0]["alpha"]["input_selections"] = {"all-symbols": "warmup-ready"}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    history = {
        symbols[0].key: [_daily_bar(symbols[0], 1, 100), _daily_bar(symbols[0], 2, 105), _daily_bar(symbols[0], 3, 115)],
        symbols[1].key: [_daily_bar(symbols[1], 1, 100), _daily_bar(symbols[1], 2, 103), _daily_bar(symbols[1], 3, 104)],
        symbols[2].key: [_daily_bar(symbols[2], 1, 100), _daily_bar(symbols[2], 2, 101), _daily_bar(symbols[2], 3, 102)],
    }
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 120 + index) for index, symbol in enumerate(symbols)})

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "us-live",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(history),
            alpha_loader=AllSymbolsAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
        ),
    )

    assert runtime.selection_warmup_report is not None
    assert runtime.selection_warmup_report.is_ready is True
    assert runtime.selection_indicator_snapshot is not None
    assert runtime.active_result.selection.selected_symbols == (Symbol("NVDA", "US"),)
    assert runtime.worker.universe.symbol_keys == ("US:NVDA",)
    assert [symbol.key for symbol in runtime.worker.indicator_engine.symbols_for_sleeve("us-live")] == ["US:NVDA"]
    assert runtime.worker.indicator_engine.value("us-live", Symbol("NVDA", "US"), "momentum_2_close") is not None
    report = runtime.run_once()
    assert report.framework is not None
    assert len(report.framework.order_intents) == 1
    assert report.framework.order_intents[0].reference_price == 120


def test_fallback_history_provider_uses_secondary_history_when_primary_fails():
    symbol = Symbol("NVDA", "US")
    fallback = FakeHistoryProvider(
        {
            symbol.key: [
                _daily_bar(symbol, 1, 100),
                _daily_bar(symbol, 2, 105),
                _daily_bar(symbol, 3, 110),
            ]
        }
    )
    provider = _FallbackHistoryProvider(primary=FailingHistoryProvider(), fallback=fallback)

    history = provider.get_history(symbol, start=datetime(2026, 5, 1), end=datetime(2026, 5, 3))

    assert [bar.close for bar in history] == [100, 105, 110]


def test_fallback_history_provider_uses_secondary_history_when_primary_range_is_too_short():
    symbol = Symbol("005930", "KRX")
    start = datetime(2025, 9, 1)
    end = datetime(2026, 5, 17)
    primary_bars = [
        Bar(symbol, start + timedelta(days=index), 100, 100, 100, 100 + index, 1000)
        for index in range(30)
    ]
    fallback_bars = [
        Bar(symbol, start + timedelta(days=index), 200, 200, 200, 200 + index, 1000)
        for index in range(120)
    ]
    provider = _FallbackHistoryProvider(
        primary=FakeHistoryProvider({symbol.key: primary_bars}),
        fallback=FakeHistoryProvider({symbol.key: fallback_bars}),
    )

    history = provider.get_history(symbol, start=start, end=end)

    assert len(history) == 120
    assert history[0].close == 200


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
            execution_model_loader=FakeExecutionModelLoader(),
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
            execution_model_loader=FakeExecutionModelLoader(),
        ),
        held_symbols=(symbol,),
    )
    report = runtime.run_once()

    assert isinstance(runtime.portfolio_provider, VirtualSleeveAccountStore)
    assert runtime.portfolio.cash == 300
    assert runtime.portfolio.quantity(symbol) == 2
    assert report.portfolio_state is not None
    assert report.portfolio_state.current.cash == 300


def test_runtime_uses_virtual_sleeve_account_store_from_broker_account_route(tmp_path):
    universe_path = tmp_path / "universe.json"
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "runtime" / "broker_accounts.json"
    _write_universe(universe_path)
    _write_runtime_config(
        config_path,
        universe_path,
        sleeve_id="LEaps",
        cash=1_000,
        min_order_notional=0,
        worker_min_success=1,
        broker_account_store_path=account_store_path,
    )
    symbol = Symbol("NVDA", "US")
    store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 1_000})
    store.initialize_sleeve("LEaps", cash=700)
    store.apply_fill(
        VirtualFillEvent(
            fill_id="fill-1",
            order_id="order-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=1,
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
            execution_model_loader=FakeExecutionModelLoader(),
        ),
        held_symbols=(symbol,),
    )
    report = runtime.run_once()

    assert isinstance(runtime.portfolio_provider, VirtualSleeveAccountStore)
    assert runtime.portfolio.cash == 600
    assert runtime.portfolio.quantity(symbol) == 1
    assert report.portfolio_state is not None
    assert report.portfolio_state.current.cash == 600


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
            execution_model_loader=FakeExecutionModelLoader(),
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
    execution_model_loader = FakeExecutionModelLoader()

    runtime = bootstrap_sleeve_runtime(
        snapshot,
        "LEaps",
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=alpha_loader,
            portfolio_model_loader=portfolio_model_loader,
            risk_model_loader=risk_model_loader,
            execution_model_loader=execution_model_loader,
        ),
    )

    assert runtime.sleeve_config.workspace_path == workspace_path
    assert alpha_loader.paths == [workspace_path / "alpha.py"]
    assert portfolio_model_loader.calls == [(str(workspace_path / "portfolio.py"), {"max_portfolio_pct": 0.5})]
    assert risk_model_loader.calls == [
        (str(workspace_path / "risk.py"), {"long_only": True, "max_position_pct": 0.4, "cash_buffer_pct": 0.05})
    ]
    assert execution_model_loader.calls == [(str(workspace_path / "execution.py"), {})]


def test_runtime_can_stage_dry_run_and_activate_sleeve_reload(tmp_path):
    universe_path = tmp_path / "universe.json"
    initial_config_path = tmp_path / "runtime_initial.json"
    updated_config_path = tmp_path / "runtime_updated.json"
    _write_universe(universe_path)
    _write_runtime_config(initial_config_path, universe_path, sleeve_id="LEaps", worker_min_success=1)
    _write_runtime_config(updated_config_path, universe_path, sleeve_id="LEaps", worker_min_success=1)
    payload = json.loads(updated_config_path.read_text(encoding="utf-8"))
    payload["runtime_id"] = "live-us-test-updated"
    payload["sleeves"][0]["risk"]["params"]["max_position_pct"] = 0.2
    updated_config_path.write_text(json.dumps(payload), encoding="utf-8")

    initial_snapshot = load_runtime_config_snapshot(initial_config_path)
    updated_snapshot = load_runtime_config_snapshot(updated_config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})
    dependencies = RuntimeBootstrapDependencies(
        live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
        history_provider_factory=lambda: FakeHistoryProvider(),
        alpha_loader=FakeAlphaLoader(),
        portfolio_model_loader=FakePortfolioModelLoader(),
        risk_model_loader=FakeRiskModelLoader(),
        execution_model_loader=FakeExecutionModelLoader(),
    )
    runtime = bootstrap_sleeve_runtime(initial_snapshot, "LEaps", dependencies=dependencies)
    runtime.run_once()

    stage_report = runtime.stage_reload(updated_snapshot, dependencies=dependencies)

    assert stage_report.previous_version == initial_snapshot.version
    assert stage_report.staged_version == updated_snapshot.version
    assert stage_report.dry_run_framework_ran is True
    assert runtime.config_version == initial_snapshot.version

    activate_report = runtime.activate_staged_reload()

    assert activate_report.activated is True
    assert activate_report.previous_version == initial_snapshot.version
    assert activate_report.staged_version == updated_snapshot.version
    assert runtime.config_version == updated_snapshot.version
    assert runtime.runtime_id == "live-us-test-updated"
    assert runtime.framework_runner.risk_model.limits.max_position_pct == 0.2

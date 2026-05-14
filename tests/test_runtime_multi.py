import json
from datetime import datetime
from types import SimpleNamespace

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.execution import ImmediateExecutionModel
from leaps_quant_engine.framework import BasicRiskManagementModel, EqualWeightPortfolioConstructionModel, RiskLimits
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_multi import run_multi_sleeve_once


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
                generated_at=context.as_of,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason="runtime_multi",
            )
        ]


class FakeAlphaLoader:
    def load(self, path):
        return SimpleNamespace(
            model=FakeAlphaModel(),
            alpha_id="fake-alpha",
            version="1.0",
            path=path,
            content_hash="abc",
        )


class FakePortfolioModelLoader:
    def load(self, ref, *, parameters=None):
        return SimpleNamespace(
            model=EqualWeightPortfolioConstructionModel(max_portfolio_pct=1.0),
            ref=ref,
            parameters=dict(parameters or {}),
            model_name="EqualWeightPortfolioConstructionModel",
        )


class FakeRiskModelLoader:
    def load(self, ref, *, parameters=None):
        return SimpleNamespace(
            model=BasicRiskManagementModel(limits=RiskLimits(long_only=True, max_position_pct=1.0)),
            ref=ref,
            parameters=dict(parameters or {}),
            model_name="BasicRiskManagementModel",
        )


class FakeExecutionModelLoader:
    def load(self, ref, *, parameters=None):
        return SimpleNamespace(
            model=ImmediateExecutionModel(),
            ref=ref,
            parameters=dict(parameters or {}),
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


def _write_universe(path, universe_id, tickers):
    path.write_text(
        json.dumps(
            {
                "id": universe_id,
                "market": "US",
                "symbols": [{"ticker": ticker, "exchange": "NAS"} for ticker in tickers],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )


def _write_multi_runtime_config(path, universe_a, universe_b):
    path.write_text(
        json.dumps(
            {
                "runtime_id": "multi-live-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "market_data": {
                    "provider": "market-data-engine",
                    "history_provider": "kis-cache",
                    "source": "fake-live",
                    "history_source": "fake-history",
                    "rate_limit_per_second": 20,
                },
                "sleeves": [
                    _sleeve_payload("sleeve-a", universe_a),
                    _sleeve_payload("sleeve-b", universe_b),
                ],
            }
        ),
        encoding="utf-8",
    )


def _sleeve_payload(sleeve_id, universe_path):
    return {
        "sleeve_id": sleeve_id,
        "cash": 100_000,
        "universe": {
            "coarse_path": str(universe_path),
            "fine": {"enabled": False},
            "active": {
                "max_symbols": 2,
                "selection_model": "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
            },
        },
        "indicators": {"warmup_enabled": False},
        "alpha": {"modules": [{"ref": "alpha.py"}]},
        "portfolio": {"module": "portfolio.py", "rebalance": {"min_order_notional": 0}},
        "risk": {"module": "risk.py"},
        "execution": {"module": "execution.py"},
        "worker": {"cycle_interval_seconds": 0, "min_success": 1},
    }


def test_multi_sleeve_runner_collects_union_market_snapshot_once(tmp_path):
    universe_a = tmp_path / "universe_a.json"
    universe_b = tmp_path / "universe_b.json"
    config_path = tmp_path / "runtime.json"
    _write_universe(universe_a, "universe-a", ["NVDA", "MSFT"])
    _write_universe(universe_b, "universe-b", ["NVDA", "IBM"])
    _write_multi_runtime_config(config_path, universe_a, universe_b)
    snapshot = load_runtime_config_snapshot(config_path)
    symbols = [Symbol("NVDA", "US"), Symbol("MSFT", "US"), Symbol("IBM", "US")]
    live_provider = FakeLiveProvider({symbol.key: _bar(symbol, 100 + index) for index, symbol in enumerate(symbols)})

    report = run_multi_sleeve_once(
        snapshot,
        ("sleeve-a", "sleeve-b"),
        dependencies=RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: live_provider,
            history_provider_factory=lambda: FakeHistoryProvider(),
            alpha_loader=FakeAlphaLoader(),
            portfolio_model_loader=FakePortfolioModelLoader(),
            risk_model_loader=FakeRiskModelLoader(),
            execution_model_loader=FakeExecutionModelLoader(),
        ),
        refresh_fine=False,
        warmup=False,
        framework_state_dir=tmp_path / "framework-state",
    )

    assert sorted(live_provider.calls) == ["US:IBM", "US:MSFT", "US:NVDA"]
    assert report.sleeve_ids == ("sleeve-a", "sleeve-b")
    assert report.requested_symbol_count == 3
    assert report.collected_symbol_count == 3
    assert report.order_count == 2
    assert [item.sleeve_id for item in report.reports] == ["sleeve-a", "sleeve-b"]
    assert {item.worker.cycles[0].market_snapshot_id for item in report.reports} == {report.market_snapshot_id}
    assert report.framework_state["sleeve-a"]["saved"] is True
    assert report.framework_state["sleeve-b"]["saved"] is True

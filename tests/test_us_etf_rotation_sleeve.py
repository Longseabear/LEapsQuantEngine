from __future__ import annotations

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys

from leaps_quant_engine.alpha import SnapshotContext
from leaps_quant_engine.execution_model_loader import PythonExecutionModelLoader
from leaps_quant_engine.framework import RiskDecisionStatus, RiskManagementContext
from leaps_quant_engine.framework.portfolio_model_loader import PythonPortfolioConstructionModelLoader
from leaps_quant_engine.framework.risk_model_loader import PythonRiskManagementModelLoader
from leaps_quant_engine.models import Bar, DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_config import parse_runtime_config
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.selection import UniverseSelectionContext


ROOT = Path(__file__).resolve().parents[1]
SLEEVE = ROOT / "sleeves" / "us_etf_rotation"


def test_us_etf_rotation_alpha_ranks_etfs_and_flattens_unselected():
    module = _load("sleeves/us_etf_rotation/alphas/etf_rotation.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "US:SPY": _values(close=600, fast=590, slow=560, momentum=0.05, momentum_5=0.01, vol=0.02),
            "US:QQQ": _values(close=500, fast=510, slow=480, momentum=0.12, momentum_5=0.04, vol=0.03),
            "US:TLT": _values(close=95, fast=96, slow=94, momentum=0.03, momentum_5=0.01, vol=0.01),
            "US:XLE": _values(close=90, fast=88, slow=92, momentum=0.08, momentum_5=0.02, vol=0.02),
        },
    )

    context = SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("US:QQQ", "US:TLT", "US:XLE"))
    insights = module.generate(context)
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert [insight.symbol.key for insight in insights[:2]] == ["US:QQQ", "US:TLT"]
    assert by_symbol["US:QQQ"].alpha_id == "us_etf_rotation"
    assert by_symbol["US:QQQ"].sleeve_id == "us_etf_rotation"
    assert by_symbol["US:QQQ"].direction.value == "up"
    assert by_symbol["US:XLE"].direction.value == "flat"
    assert by_symbol["US:XLE"].reason == "not_selected_by_etf_rotation"


def test_us_etf_rotation_alpha_uses_intermediate_dual_momentum():
    module = _load("sleeves/us_etf_rotation/alphas/etf_rotation.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "US:SPY": _values(close=600, fast=590, slow=560, momentum=0.05, momentum_5=0.01, vol=0.02),
            "US:QQQ": _values(
                close=500,
                fast=490,
                slow=470,
                momentum=0.02,
                momentum_5=0.01,
                vol=0.03,
                momentum_3m=0.12,
                momentum_6m=0.24,
                momentum_12m=0.30,
                long_trend=430,
            ),
            "US:XLP": _values(
                close=80,
                fast=79,
                slow=77,
                momentum=0.04,
                momentum_5=0.01,
                vol=0.01,
                momentum_3m=0.06,
                momentum_6m=0.08,
                momentum_12m=0.11,
                long_trend=74,
            ),
            "US:XLE": _values(
                close=90,
                fast=91,
                slow=92,
                momentum=0.25,
                momentum_5=0.03,
                vol=0.02,
                momentum_3m=0.18,
                momentum_6m=0.20,
                momentum_12m=0.22,
                long_trend=95,
            ),
        },
    )

    context = SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("US:QQQ", "US:XLP", "US:XLE"))
    insights = module.generate(context)
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["US:QQQ"].direction.value == "up"
    assert by_symbol["US:QQQ"].metadata["momentum_6m"] == 0.24
    assert by_symbol["US:QQQ"].metadata["momentum_12m"] == 0.30
    assert by_symbol["US:XLE"].direction.value == "flat"


def test_us_etf_rotation_alpha_treats_missing_spy_gate_as_risk_off():
    module = _load("sleeves/us_etf_rotation/alphas/etf_rotation.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "US:QQQ": _values(
                close=500,
                fast=490,
                slow=470,
                momentum=0.20,
                momentum_5=0.04,
                vol=0.03,
                momentum_3m=0.22,
                momentum_6m=0.24,
                momentum_12m=0.26,
                long_trend=430,
            ),
            "US:TLT": _values(
                close=95,
                fast=96,
                slow=92,
                momentum=0.04,
                momentum_5=0.01,
                vol=0.01,
                momentum_3m=0.04,
                momentum_6m=0.05,
                momentum_12m=0.06,
                long_trend=90,
            ),
        },
    )

    context = SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("US:QQQ", "US:TLT"))
    insights = module.generate(context)
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["US:QQQ"].direction.value == "flat"
    assert by_symbol["US:TLT"].direction.value == "up"
    assert by_symbol["US:TLT"].metadata["risk_on"] is False


def test_us_etf_rotation_daa_pullback_selects_four_etfs():
    module = _load("sleeves/us_etf_rotation/alphas/daa_pullback.py")
    now = datetime(2026, 5, 18)
    snapshot = _snapshot(
        now,
        {
            "US:SPY": _values(close=600, fast=590, slow=560, momentum=0.06, momentum_5=0.01, vol=0.02),
            "US:QQQ": _values(close=500, fast=490, slow=470, momentum=0.12, momentum_5=0.02, vol=0.03),
            "US:IWM": _values(close=220, fast=216, slow=205, momentum=0.09, momentum_5=0.01, vol=0.03),
            "US:XLK": _values(close=180, fast=176, slow=160, momentum=0.20, momentum_5=0.03, vol=0.02),
            "US:XLE": _values(close=95, fast=93, slow=88, momentum=0.16, momentum_5=0.02, vol=0.02),
        },
    )

    context = SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(tuple(snapshot.symbols))
    insights = module.generate(context)

    up_symbols = [insight.symbol.key for insight in insights if insight.direction.value == "up"]
    assert len(up_symbols) == 4
    assert up_symbols == ["US:XLK", "US:XLE", "US:QQQ", "US:IWM"]


def test_us_etf_rotation_selection_keeps_etf_universe_only():
    module = _load("sleeves/us_etf_rotation/selections/etf_rotation.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "mixed-us-test",
            "market": "US",
            "symbols": [
                {"ticker": "SPY", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "QQQ", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "SMH", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "AAPL", "market": "US", "asset_type": "stock"},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "US:SPY": _values(close=600, fast=590, slow=560, momentum=0.05, momentum_5=0.01, vol=0.02),
            "US:QQQ": _values(close=500, fast=510, slow=480, momentum=0.12, momentum_5=0.04, vol=0.03),
            "US:SMH": _values(close=250, fast=255, slow=240, momentum=0.18, momentum_5=0.07, vol=0.05),
            "US:AAPL": _values(close=210, fast=212, slow=200, momentum=0.50, momentum_5=0.08, vol=0.04),
        },
    )

    result = module.EtfRotationSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="us_etf_rotation", universe=universe, indicator_snapshot=snapshot)
    )

    assert result.selection_id == "us_etf_rotation"
    assert [symbol.key for symbol in result.selected_symbols] == ["US:SMH", "US:QQQ"]
    assert result.rejected["US:AAPL"] == ("not_etf",)


def test_us_etf_rotation_selection_prefers_defensive_etfs_when_market_risk_off():
    module = _load("sleeves/us_etf_rotation/selections/etf_rotation.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "risk-off-test",
            "market": "US",
            "symbols": [
                {"ticker": "SPY", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "QQQ", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "TLT", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "GLD", "market": "US", "asset_type": "etf", "is_etf": True},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "US:SPY": _values(close=420, fast=430, slow=440, momentum=-0.05, momentum_5=-0.01, vol=0.03, long_trend=450),
            "US:QQQ": _values(close=500, fast=510, slow=480, momentum=0.20, momentum_5=0.04, vol=0.03, long_trend=470),
            "US:TLT": _values(close=95, fast=96, slow=92, momentum=0.04, momentum_5=0.01, vol=0.01, long_trend=90),
            "US:GLD": _values(close=210, fast=211, slow=205, momentum=0.03, momentum_5=0.01, vol=0.01, long_trend=200),
        },
    )

    result = module.EtfRotationSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="us_etf_rotation", universe=universe, indicator_snapshot=snapshot)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["US:TLT", "US:GLD"]
    assert result.rejected["US:QQQ"] == ("market_risk_off",)


def test_us_etf_rotation_selection_treats_missing_spy_gate_as_risk_off():
    module = _load("sleeves/us_etf_rotation/selections/etf_rotation.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "missing-spy-gate-test",
            "market": "US",
            "symbols": [
                {"ticker": "QQQ", "market": "US", "asset_type": "etf", "is_etf": True},
                {"ticker": "TLT", "market": "US", "asset_type": "etf", "is_etf": True},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "US:QQQ": _values(close=500, fast=510, slow=480, momentum=0.20, momentum_5=0.04, vol=0.03, long_trend=470),
            "US:TLT": _values(close=95, fast=96, slow=92, momentum=0.04, momentum_5=0.01, vol=0.01, long_trend=90),
        },
    )

    result = module.EtfRotationSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="us_etf_rotation", universe=universe, indicator_snapshot=snapshot)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["US:TLT"]
    assert result.rejected["US:QQQ"] == ("market_risk_off",)


def test_us_etf_rotation_workspace_models_load_from_sleeve_folder():
    portfolio = PythonPortfolioConstructionModelLoader().load(
        SLEEVE / "portfolios" / "rl_ppo_constructor.py",
        parameters={
            "allocation_mode": "rl_weights",
            "fallback_gross_exposure": 0.70,
            "top_k": 8,
        },
    )
    risk = PythonRiskManagementModelLoader().load(
        SLEEVE / "risks" / "basic.py",
        parameters={"max_position_pct": 0.30},
    )
    execution = PythonExecutionModelLoader().load(
        SLEEVE / "executions" / "immediate.py",
        parameters={"tag_prefix": "us_etf_rotation"},
    )

    assert portfolio.model_name == "ReinforcementLearningPortfolioConstructionModel"
    assert portfolio.model.allocation_mode == "rl_weights"
    assert portfolio.model.top_k == 8
    assert risk.model_name == "BasicRiskManagementModel"
    assert execution.model_name == "UsEtfRotationExecutionModel"


def test_us_etf_rotation_live_cadences_match_etf_horizon():
    for config_path in (
        ROOT / "configs" / "runtime" / "live_multi_sleeve.json",
        ROOT / "configs" / "runtime" / "us_etf_rotation_sleeve.json",
    ):
        config = parse_runtime_config(json.loads(config_path.read_text(encoding="utf-8")))
        sleeve = config.sleeve("us_etf_rotation")

        assert sleeve.universe.active.cadence == "once_per_day"
        assert sleeve.worker.cycle_interval_seconds == 300
        assert sleeve.portfolio.rebalance.cadence == "every_5_minutes"

    pullback = _load("sleeves/us_etf_rotation/alphas/daa_pullback.py")
    trailing_stop = _load("sleeves/us_etf_rotation/alphas/volatility_trailing_stop.py")

    assert pullback.EVALUATION_CADENCE == "every_cycle"
    assert trailing_stop.EVALUATION_CADENCE == "every_cycle"


def test_us_etf_rotation_risk_clamps_cycle_buy_notional():
    first = Symbol("AAA", "US")
    second = Symbol("BBB", "US")
    now = datetime(2026, 5, 19, 9, 30)
    data = DataSlice(
        time=now,
        bars={
            first.key: Bar(first, now, 100, 100, 100, 100, 1000),
            second.key: Bar(second, now, 100, 100, 100, 100, 1000),
        },
    )
    risk = PythonRiskManagementModelLoader().load(
        SLEEVE / "risks" / "basic.py",
        parameters={
            "max_position_pct": 1.0,
            "max_total_exposure_pct": 1.0,
            "cash_buffer_pct": 0.0,
            "max_cycle_buy_notional": 500.0,
        },
    )

    batch = risk.model.manage_risk(
        RiskManagementContext(
            sleeve_id="us_etf_rotation",
            data=data,
            portfolio=Portfolio(cash=2_000, cash_by_currency={"USD": 2_000}),
            targets=(
                PortfolioTarget(first, quantity=5, tag="entry"),
                PortfolioTarget(second, quantity=5, tag="entry"),
            ),
        )
    )

    approved_by_symbol = {target.symbol.key: target.quantity for target in batch.approved_targets}
    approved_notional = sum(approved_by_symbol.values()) * 100

    assert risk.model_name == "CycleBuyNotionalRiskModel"
    assert approved_notional <= 500
    assert approved_by_symbol == {"US:AAA": 3, "US:BBB": 2}
    assert all(decision.status is RiskDecisionStatus.CLAMPED for decision in batch.decisions)
    assert {decision.reason for decision in batch.decisions} == {"cycle_buy_notional_clamped"}


def test_us_etf_rotation_risk_does_not_block_reductions_with_cycle_buy_cap():
    sell_symbol = Symbol("OLD", "US")
    buy_symbol = Symbol("NEW", "US")
    now = datetime(2026, 5, 19, 9, 30)
    data = DataSlice(
        time=now,
        bars={
            sell_symbol.key: Bar(sell_symbol, now, 100, 100, 100, 100, 1000),
            buy_symbol.key: Bar(buy_symbol, now, 100, 100, 100, 100, 1000),
        },
    )
    risk = PythonRiskManagementModelLoader().load(
        SLEEVE / "risks" / "basic.py",
        parameters={
            "max_position_pct": 1.0,
            "max_total_exposure_pct": 1.0,
            "cash_buffer_pct": 0.0,
            "max_cycle_buy_notional": 200.0,
        },
    )

    batch = risk.model.manage_risk(
        RiskManagementContext(
            sleeve_id="us_etf_rotation",
            data=data,
            portfolio=Portfolio(
                cash=1_000,
                cash_by_currency={"USD": 1_000},
                holdings={sell_symbol.key: Holding(sell_symbol, quantity=5, average_price=100.0)},
            ),
            targets=(
                PortfolioTarget(sell_symbol, quantity=2, tag="reduce"),
                PortfolioTarget(buy_symbol, quantity=5, tag="entry"),
            ),
        )
    )

    approved_by_symbol = {target.symbol.key: target.quantity for target in batch.approved_targets}

    assert approved_by_symbol["US:OLD"] == 2
    assert approved_by_symbol["US:NEW"] == 2
    assert batch.decisions[0].status is RiskDecisionStatus.APPROVED
    assert batch.decisions[1].status is RiskDecisionStatus.CLAMPED


def _snapshot(now: datetime, values: dict[str, dict[str, float]]) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="us_etf_rotation",
        universe_id="test",
        as_of=now,
        created_at=now,
        symbols=tuple(values),
        values={
            symbol: {
                name: IndicatorValue(name=name, value=value, is_ready=True, samples=30, time=now)
                for name, value in indicator_values.items()
            }
            for symbol, indicator_values in values.items()
        },
        source_snapshot_id="test",
    )


def _values(
    *,
    close: float,
    fast: float,
    slow: float,
    momentum: float,
    momentum_5: float,
    vol: float,
    momentum_3m: float | None = None,
    momentum_6m: float | None = None,
    momentum_12m: float | None = None,
    long_trend: float | None = None,
) -> dict[str, float]:
    return {
        "close": close,
        "identity_close": close,
        "ema_8_close": fast,
        "sma_20_close": slow,
        "sma_100_close": long_trend if long_trend is not None else slow,
        "sma_200_close": long_trend if long_trend is not None else slow,
        "roc_20_close": momentum,
        "roc_63_close": momentum_3m if momentum_3m is not None else momentum,
        "roc_126_close": momentum_6m if momentum_6m is not None else momentum,
        "roc_252_close": momentum_12m if momentum_12m is not None else momentum,
        "momentum_5_close": momentum_5,
        "stddev_20_close": close * vol,
        "stddev_63_close": close * vol,
        "atr_14": close * vol,
        "rolling_dollar_volume_20": 5_000_000_000,
        "volume": 1000,
    }


def _load(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

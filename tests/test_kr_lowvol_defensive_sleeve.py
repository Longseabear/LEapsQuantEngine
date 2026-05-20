from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import importlib.util
from pathlib import Path
import sys

from leaps_quant_engine.alpha import InsightDirection, SnapshotContext
from leaps_quant_engine.execution_model_loader import PythonExecutionModelLoader
from leaps_quant_engine.framework.portfolio_construction import PortfolioConstructionContext
from leaps_quant_engine.framework.portfolio_model_loader import PythonPortfolioConstructionModelLoader
from leaps_quant_engine.framework.risk_model_loader import PythonRiskManagementModelLoader
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.selection import UniverseSelectionContext


ROOT = Path(__file__).resolve().parents[1]
SLEEVE = ROOT / "sleeves" / "kr-lowvol-defensive"


def test_kr_lowvol_selection_filters_and_ranks_defensive_candidates():
    module = _load("sleeves/kr-lowvol-defensive/selections/lowvol_rank.py")
    now = datetime(2026, 5, 21)
    universe = parse_universe_definition(
        {
            "id": "lowvol-selection-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock"},
                {"ticker": "105560", "market": "KRX", "asset_type": "stock"},
                {"ticker": "042700", "market": "KRX", "asset_type": "stock"},
                {"ticker": "003550", "market": "KRX", "asset_type": "stock"},
                {"ticker": "005935", "market": "KRX", "asset_type": "stock", "preferred": True},
                {"ticker": "069500", "market": "KRX", "asset_type": "etf", "is_etf": True},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, vol=0.050, momentum_20=0.035, momentum_60=0.075, trend=0.025),
            "KRX:105560": _values(close=70000, vol=0.030, momentum_20=0.020, momentum_60=0.040, trend=0.010),
            "KRX:042700": _values(close=280000, vol=0.155, momentum_20=0.010, momentum_60=0.020, trend=0.020),
            "KRX:003550": _values(
                close=82000,
                vol=0.025,
                momentum_20=0.080,
                momentum_60=0.130,
                trend=0.050,
                bar_return=0.110,
                volume_ratio=2.60,
                high_low_range=0.080,
                zscore=2.40,
            ),
            "KRX:005935": _values(close=62000, vol=0.020, momentum_20=0.030, momentum_60=0.050, trend=0.010),
            "KRX:069500": _values(close=36000, vol=0.025, momentum_20=0.020, momentum_60=0.040, trend=0.010),
        },
    )

    result = module.LowVolDefensiveSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="kr-lowvol-defensive", universe=universe, indicator_snapshot=snapshot)
    )

    assert result.selection_id == "kr-lowvol-defensive-core"
    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:105560", "KRX:005930"]
    assert result.rejected["KRX:042700"] == ("extreme_volatility",)
    assert result.rejected["KRX:003550"] == ("lottery_like_spike",)
    assert result.rejected["KRX:005935"] == ("preferred_share",)
    assert result.rejected["KRX:069500"] == ("not_stock_candidate",)


def test_kr_lowvol_alpha_emits_up_insights_for_low_vol_not_falling_knives():
    module = _load("sleeves/kr-lowvol-defensive/alphas/lowvol_defensive.py")
    now = datetime(2026, 5, 21)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, vol=0.050, momentum_20=0.035, momentum_60=0.075, trend=0.025),
            "KRX:105560": _values(close=70000, vol=0.030, momentum_20=0.020, momentum_60=0.040, trend=0.010),
            "KRX:042700": _values(close=280000, vol=0.180, momentum_20=-0.070, momentum_60=-0.120, trend=-0.060, drawdown_60=-0.26),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930", "KRX:105560", "KRX:042700"))
    )

    assert [insight.symbol.key for insight in insights] == ["KRX:105560", "KRX:005930"]
    assert all(insight.direction is InsightDirection.UP for insight in insights)
    assert insights[0].alpha_id == "kr-lowvol-defensive-alpha"
    assert insights[0].alpha_version == "0.2.0"
    assert insights[0].group_id == "krw-lowvol-defensive"
    assert insights[0].reason == "anti_lottery_defensive_rank"
    assert insights[0].metadata["risk_bucket"] == "calm"
    assert insights[0].metadata["style"] == "kr_lowvol_defensive_v2"
    assert insights[0].metadata["factor_version"] == "0.2.0"
    assert "lottery_penalty" in insights[0].metadata
    assert "crowding_penalty" in insights[0].metadata
    assert "quality_score" in insights[0].metadata
    assert "value_score" in insights[0].metadata
    assert "dividend_score" in insights[0].metadata


def test_kr_lowvol_portfolio_weights_inverse_vol_and_zeroes_missing_held_symbol():
    alpha = _load("sleeves/kr-lowvol-defensive/alphas/lowvol_defensive.py")
    portfolio_module = _load("sleeves/kr-lowvol-defensive/portfolios/inverse_vol.py")
    now = datetime(2026, 5, 21)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, vol=0.050, momentum_20=0.035, momentum_60=0.075, trend=0.025),
            "KRX:105560": _values(close=70000, vol=0.030, momentum_20=0.020, momentum_60=0.040, trend=0.010),
        },
    )
    insights = tuple(alpha.generate(SnapshotContext.from_indicator_snapshot(snapshot)))
    samsung = Symbol("005930", "KRX")
    kb = Symbol("105560", "KRX")
    old = Symbol("042700", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(samsung, now, 80000, 80500, 79500, 80000),
            kb.key: Bar(kb, now, 70000, 70400, 69600, 70000),
            old.key: Bar(old, now, 280000, 281000, 279000, 280000),
        },
    )
    context = PortfolioConstructionContext(
        sleeve_id="kr-lowvol-defensive",
        data=data,
        portfolio=Portfolio(
            cash=5_000_000,
            cash_by_currency={"KRW": 5_000_000},
            holdings={old.key: Holding(old, quantity=3, average_price=260000)},
        ),
        active_insights=insights,
        managed_symbols=(samsung, kb, old),
    )

    targets = portfolio_module.LowVolInverseVolPortfolioConstructionModel(
        top_k=2,
        core_gross_exposure=0.88,
        max_position_pct=0.60,
        emit_zero_for_missing_held_targets=True,
    ).create_targets(context)
    target_by_symbol = {target.symbol.key: target for target in targets}

    assert set(target_by_symbol) == {"KRX:005930", "KRX:105560", "KRX:042700"}
    assert target_by_symbol["KRX:105560"].target_percent > target_by_symbol["KRX:005930"].target_percent
    assert round(target_by_symbol["KRX:042700"].target_percent, 6) == 0.0
    assert "missing_target_zero" in target_by_symbol["KRX:042700"].tag


def test_kr_lowvol_portfolio_haircuts_crowded_lottery_candidate():
    alpha = _load("sleeves/kr-lowvol-defensive/alphas/lowvol_defensive.py")
    portfolio_module = _load("sleeves/kr-lowvol-defensive/portfolios/inverse_vol.py")
    now = datetime(2026, 5, 21)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, vol=0.050, momentum_20=0.035, momentum_60=0.075, trend=0.025),
            "KRX:105560": _values(close=70000, vol=0.030, momentum_20=0.020, momentum_60=0.040, trend=0.010),
        },
    )
    raw_insights = alpha.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    insights = []
    for insight in raw_insights:
        metadata = dict(insight.metadata)
        if insight.symbol.key == "KRX:105560":
            metadata.update(
                {
                    "crowding_penalty": 0.80,
                    "lottery_penalty": 0.40,
                    "turnover_shock_penalty": 0.70,
                    "risk_bucket": "defensive",
                }
            )
        insights.append(replace(insight, metadata=metadata))

    samsung = Symbol("005930", "KRX")
    kb = Symbol("105560", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(samsung, now, 80000, 80500, 79500, 80000),
            kb.key: Bar(kb, now, 70000, 70400, 69600, 70000),
        },
    )
    context = PortfolioConstructionContext(
        sleeve_id="kr-lowvol-defensive",
        data=data,
        portfolio=Portfolio(cash=5_000_000, cash_by_currency={"KRW": 5_000_000}),
        active_insights=tuple(insights),
        managed_symbols=(samsung, kb),
    )

    targets = portfolio_module.LowVolInverseVolPortfolioConstructionModel(
        top_k=2,
        core_gross_exposure=0.88,
        max_position_pct=0.60,
        emit_zero_for_missing_held_targets=True,
    ).create_targets(context)
    target_by_symbol = {target.symbol.key: target for target in targets}

    assert target_by_symbol["KRX:005930"].target_percent > target_by_symbol["KRX:105560"].target_percent
    assert "crowd=0.80" in target_by_symbol["KRX:105560"].tag


def test_kr_lowvol_runtime_config_and_workspace_models_load():
    snapshot = load_runtime_config_snapshot(ROOT / "configs" / "runtime" / "kr_lowvol_defensive_sleeve.json")
    sleeve = snapshot.config.sleeve("kr-lowvol-defensive")

    portfolio = PythonPortfolioConstructionModelLoader().load(
        SLEEVE / "portfolios" / "inverse_vol.py",
        parameters={"top_k": 12},
    )
    risk = PythonRiskManagementModelLoader().load(
        SLEEVE / "risks" / "basic.py",
        parameters={"max_position_pct": 0.11},
    )
    execution = PythonExecutionModelLoader().load(
        SLEEVE / "executions" / "immediate.py",
        parameters={"tag_prefix": "kr-lowvol-defensive"},
    )

    assert snapshot.config.mode == "paper"
    assert sleeve.workspace_path == Path("sleeves/kr-lowvol-defensive")
    assert sleeve.universe.coarse_path == Path("configs/universes/kr_lowvol_defensive_core.json")
    assert [module.ref for module in sleeve.alpha.modules] == ["alphas/lowvol_defensive.py"]
    assert dict(sleeve.alpha.input_selections) == {
        "kr-lowvol-defensive-alpha": "kr-lowvol-defensive-core",
    }
    assert sleeve.portfolio.model.ref == "portfolios/inverse_vol.py"
    assert portfolio.model_name == "LowVolInverseVolPortfolioConstructionModel"
    assert risk.model_name == "BasicRiskManagementModel"
    assert execution.model_name == "LowVolDefensiveExecutionModel"


def _snapshot(
    now: datetime,
    values: dict[str, dict[str, float]],
    metadata: dict[str, dict[str, float | str]] | None = None,
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="kr-lowvol-defensive",
        universe_id="kr-lowvol-defensive-test",
        as_of=now,
        created_at=now,
        symbols=tuple(values),
        values={
            symbol: {
                name: IndicatorValue(name=name, value=value, is_ready=True, samples=260, time=now)
                for name, value in indicator_values.items()
            }
            for symbol, indicator_values in values.items()
        },
        source_snapshot_id="test",
        symbol_metadata=metadata or {},
    )


def _values(
    *,
    close: float,
    vol: float,
    momentum_20: float,
    momentum_60: float,
    trend: float,
    drawdown_60: float = -0.04,
    liquidity: float = 5_000_000_000.0,
    gap: float = 0.01,
    bar_return: float = 0.01,
    high_low_range: float = 0.02,
    rolling_range: float = 0.06,
    volume_ratio: float = 1.0,
    volume_momentum: float = 0.0,
    zscore: float = 0.4,
) -> dict[str, float]:
    sma60 = close / (1.0 + trend) if abs(1.0 + trend) > 1e-9 else close
    return {
        "close": close,
        "identity_close": close,
        "sma_20_close": sma60,
        "sma_60_close": sma60,
        "sma_120_close": sma60,
        "roc_20_close": momentum_20,
        "roc_60_close": momentum_60,
        "roc_120_close": momentum_60,
        "stddev_20_close": close * vol,
        "stddev_60_close": close * vol,
        "stddev_120_close": close * vol,
        "atr_14": close * vol * 0.80,
        "drawdown_20_close": max(drawdown_60 * 0.60, -0.01),
        "drawdown_60_close": drawdown_60,
        "gap_percent": gap,
        "bar_return_close": bar_return,
        "high_low_range_percent": high_low_range,
        "rolling_range_20_close": close * rolling_range,
        "volume_ratio_20": volume_ratio,
        "volume_momentum_20": volume_momentum,
        "zscore_20_close": zscore,
        "close_location_value": 0.25,
        "rolling_dollar_volume_20": liquidity,
        "rolling_dollar_volume_60": liquidity,
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

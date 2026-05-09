from datetime import datetime

import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import (
    EqualWeightPortfolioConstructionModel,
    PortfolioConstructionContext,
    PortfolioConstructionEngine,
    RebalancePolicy,
)
from leaps_quant_engine.models import Bar, DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio


class StaticTargetModel:
    def __init__(self, *targets: PortfolioTarget):
        self.targets = targets

    def create_targets(self, context):
        return self.targets


def _bar(symbol: Symbol, close: float, *, as_of: datetime = datetime(2026, 5, 9, 9, 30)) -> Bar:
    return Bar(symbol, as_of, close, close, close, close, 1000)


def _slice(*bars: Bar) -> DataSlice:
    return DataSlice(time=bars[0].time, bars={bar.symbol.key: bar for bar in bars})


def _insight(symbol: Symbol, *, as_of: datetime = datetime(2026, 5, 9, 9, 30)) -> Insight:
    return Insight(
        sleeve_id="test-sleeve",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=as_of,
        source_snapshot_id="snapshot-1",
        alpha_id="alpha-a",
        alpha_version="1.0",
    )


def test_portfolio_construction_engine_creates_target_batch_with_cash_reserve():
    first = Symbol("AAA", "US")
    second = Symbol("BBB", "US")
    data = _slice(_bar(first, 100.0), _bar(second, 50.0))
    insights = (_insight(first), _insight(second))
    engine = PortfolioConstructionEngine(
        model=EqualWeightPortfolioConstructionModel(),
        rebalance_policy=RebalancePolicy(cash_reserve_pct=0.20),
    )

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=1_000),
            active_insights=insights,
            managed_symbols=(),
        )
    )

    assert batch.sleeve_id == "test-sleeve"
    assert batch.model_name == "EqualWeightPortfolioConstructionModel"
    assert batch.source_insight_ids == tuple(insight.insight_id for insight in insights)
    assert {target.symbol.key: target.quantity for target in batch.targets} == {
        "US:AAA": 4,
        "US:BBB": 8,
    }
    assert {plan.target.symbol.key: plan.delta_quantity for plan in batch.plans} == {
        "US:AAA": 4,
        "US:BBB": 8,
    }
    assert batch.plans[0].source_insight_ids
    assert batch.metadata["portfolio_equity"] == pytest.approx(1_000)
    assert batch.metadata["target_portfolio_value"] == pytest.approx(800)
    assert batch.metadata["raw_plan_count"] == 2
    assert batch.metadata["filtered_plan_count"] == 2


def test_rebalance_policy_filters_small_target_delta():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=10, tag="static")
    portfolio = Portfolio(cash=100, holdings={symbol.key: Holding(symbol, quantity=9, average_price=100.0)})
    engine = PortfolioConstructionEngine(
        model=StaticTargetModel(target),
        rebalance_policy=RebalancePolicy(min_order_notional=200.0),
    )

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 100.0)),
            portfolio=portfolio,
            active_insights=(),
            managed_symbols=(symbol,),
        )
    )

    assert batch.targets == ()
    assert batch.plans == ()
    assert batch.metadata["raw_target_count"] == 1
    assert batch.metadata["filtered_target_count"] == 0
    assert batch.metadata["raw_plan_count"] == 1
    assert batch.metadata["filtered_plan_count"] == 0


def test_rebalance_policy_preserves_small_exit_targets_by_default():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=0, tag="exit")
    portfolio = Portfolio(cash=100, holdings={symbol.key: Holding(symbol, quantity=1, average_price=100.0)})
    engine = PortfolioConstructionEngine(
        model=StaticTargetModel(target),
        rebalance_policy=RebalancePolicy(min_order_notional=1_000.0),
    )

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 100.0)),
            portfolio=portfolio,
            active_insights=(),
            managed_symbols=(symbol,),
        )
    )

    assert batch.targets == (target,)
    assert len(batch.plans) == 1
    assert batch.plans[0].current_quantity == 1
    assert batch.plans[0].target_quantity == 0
    assert batch.plans[0].delta_quantity == -1
    assert batch.plans[0].is_exit is True


def test_equal_weight_model_flattens_current_holding_without_active_insight():
    held = Symbol("HELD", "US")
    portfolio = Portfolio(cash=100, holdings={held.key: Holding(held, quantity=3, average_price=25.0)})
    engine = PortfolioConstructionEngine(model=EqualWeightPortfolioConstructionModel())

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(held, 30.0)),
            portfolio=portfolio,
            active_insights=(),
            managed_symbols=(),
        )
    )

    assert batch.targets == (PortfolioTarget(symbol=held, quantity=0, tag="framework:insight_inactive"),)
    assert batch.plans[0].current_quantity == 3
    assert batch.plans[0].target_quantity == 0
    assert batch.plans[0].current_price == 30.0
    assert batch.plans[0].current_value == pytest.approx(90.0)
    assert batch.plans[0].target_value == pytest.approx(0.0)
    assert batch.plans[0].delta_value == pytest.approx(-90.0)


def test_portfolio_target_batch_serializes_target_plans():
    symbol = Symbol("AAA", "US")
    batch = PortfolioConstructionEngine(model=StaticTargetModel(PortfolioTarget(symbol, 2, "static"))).create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 50.0)),
            portfolio=Portfolio(cash=1_000),
            active_insights=(),
            managed_symbols=(),
        )
    )

    payload = batch.to_dict()

    assert payload["plan_count"] == 1
    assert payload["plans"][0]["symbol"] == "US:AAA"
    assert payload["plans"][0]["current_quantity"] == 0
    assert payload["plans"][0]["target_quantity"] == 2
    assert payload["plans"][0]["delta_quantity"] == 2
    assert payload["plans"][0]["target_value"] == pytest.approx(100.0)

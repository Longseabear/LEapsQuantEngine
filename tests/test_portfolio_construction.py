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
    assert batch.metadata["portfolio_equity"] == pytest.approx(1_000)
    assert batch.metadata["target_portfolio_value"] == pytest.approx(800)


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
    assert batch.metadata["raw_target_count"] == 1
    assert batch.metadata["filtered_target_count"] == 0


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

from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import (
    EqualWeightPortfolioConstructionModel,
    OrderSizingContext,
    OrderSizingEngine,
    PortfolioConstructionContext,
    PortfolioConstructionEngine,
    PortfolioAllocationTarget,
    PortfolioTargetBatch,
    PortfolioTargetPlan,
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
    assert {target.symbol.key: target.target_percent for target in batch.targets} == {
        "US:AAA": pytest.approx(0.5),
        "US:BBB": pytest.approx(0.5),
    }
    assert {plan.target.symbol.key: plan.desired_value for plan in batch.plans} == {
        "US:AAA": pytest.approx(400),
        "US:BBB": pytest.approx(400),
    }
    sized = OrderSizingEngine(rebalance_policy=engine.rebalance_policy).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=1_000),
            portfolio_targets=batch,
        )
    )
    assert {plan.allocation.symbol.key: plan.delta_quantity for plan in sized.plans} == {
        "US:AAA": 4,
        "US:BBB": 8,
    }
    assert batch.plans[0].source_insight_ids
    assert batch.metadata["portfolio_equity"] == pytest.approx(1_000)
    assert batch.metadata["target_portfolio_value"] == pytest.approx(800)
    assert batch.metadata["raw_plan_count"] == 2
    assert batch.metadata["filtered_plan_count"] == 2


def test_order_sizing_recomputes_reused_percent_target_from_current_portfolio_state():
    symbol = Symbol("AAA", "US")
    first_data = _slice(_bar(symbol, 100.0))
    second_data = _slice(_bar(symbol, 200.0))
    batch = PortfolioConstructionEngine(
        model=EqualWeightPortfolioConstructionModel(max_portfolio_pct=0.5),
    ).create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=first_data,
            portfolio=Portfolio(cash=1_000),
            active_insights=(_insight(symbol),),
            managed_symbols=(),
        )
    )
    first_sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=first_data,
            portfolio=Portfolio(cash=1_000),
            portfolio_targets=batch,
        )
    )

    updated_portfolio = Portfolio(
        cash=500,
        holdings={symbol.key: Holding(symbol, quantity=5, average_price=100.0)},
    )
    second_sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=second_data,
            portfolio=updated_portfolio,
            portfolio_targets=batch,
        )
    )

    assert batch.plans[0].desired_value == pytest.approx(500.0)
    assert batch.plans[0].current_price == pytest.approx(100.0)
    assert first_sized.targets == (PortfolioTarget(symbol=symbol, quantity=5, tag="framework:alpha-a:up"),)
    assert second_sized.plans[0].current_price == pytest.approx(200.0)
    assert second_sized.plans[0].desired_value == pytest.approx(750.0)
    assert second_sized.plans[0].target_quantity == 3
    assert second_sized.plans[0].delta_quantity == -2
    assert second_sized.metadata["recomputed_from_current_state"] is True


def test_order_sizing_lot_optimizer_recovers_meaningful_fractional_targets():
    expensive = Symbol("EXP", "US")
    cheaper = Symbol("CHP", "US")
    data = _slice(_bar(expensive, 160.0), _bar(cheaper, 90.0))
    batch = PortfolioConstructionEngine(
        model=EqualWeightPortfolioConstructionModel(max_portfolio_pct=0.6),
    ).create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=500),
            active_insights=(_insight(expensive), _insight(cheaper)),
            managed_symbols=(),
        )
    )

    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=500),
            portfolio_targets=batch,
        )
    )

    plans_by_symbol = {plan.allocation.symbol.key: plan for plan in sized.plans}
    assert plans_by_symbol["US:EXP"].target_quantity == 1
    assert plans_by_symbol["US:CHP"].target_quantity == 1
    assert sized.metadata["lot_optimizer_adjustment_count"] == 1
    assert sized.metadata["lot_optimizer_deployed_notional"] == pytest.approx(160.0)
    assert sized.metadata["raw_rounding_loss"] > sized.metadata["rounding_loss"]


def test_order_sizing_lot_optimizer_ignores_tiny_fractional_targets():
    symbol = Symbol("EXP", "US")
    data = _slice(_bar(symbol, 500.0))
    batch = PortfolioConstructionEngine(
        model=EqualWeightPortfolioConstructionModel(max_portfolio_pct=0.1),
    ).create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=1_000),
            active_insights=(_insight(symbol),),
            managed_symbols=(),
        )
    )

    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=1_000),
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["lot_optimizer_adjustment_count"] == 0


def test_order_sizing_can_suppress_reused_batch_adjacent_lot_sell_churn():
    symbol = Symbol("005380", "KRX")
    target = PortfolioAllocationTarget(
        symbol=symbol,
        target_percent=0.138,
        tag="rl:attention_ppo:leaps-kospi-conviction:weight=0.138",
    )
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 13, 26),
        targets=(target,),
        plans=(
            PortfolioTargetPlan(
                target=target,
                current_quantity=3,
                current_price=698_000,
                current_value=2_094_000,
                target_percent=0.138,
                desired_value=1_750_000,
                reason="target",
            ),
        ),
        metadata={"reused": True, "source_batch_id": "portfolio-targets-1"},
    )
    portfolio = Portfolio(
        cash=10_581_159.420289854,
        holdings={symbol.key: Holding(symbol, quantity=3, average_price=671_778)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            reused_target_churn_guard=True,
            reused_target_churn_lot_fraction=0.5,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 700_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["reused_target_churn_guard_enabled"] is True
    assert sized.metadata["reused_target_churn_suppressed_count"] == 1
    assert sized.metadata["reused_target_churn_suppressed_symbols"] == ["KRX:005380"]


def test_order_sizing_can_suppress_reused_batch_adjacent_lot_buy_back_churn():
    symbol = Symbol("005380", "KRX")
    stabilizer = Symbol("005930", "KRX")
    target = PortfolioAllocationTarget(
        symbol=symbol,
        target_percent=0.138,
        tag="rl:attention_ppo:leaps-kospi-conviction:weight=0.138",
    )
    stabilizer_target = PortfolioAllocationTarget(symbol=stabilizer, target_percent=0.08)
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 13, 28),
        targets=(target, stabilizer_target),
        plans=(
            PortfolioTargetPlan(
                target=target,
                current_quantity=3,
                current_price=698_000,
                current_value=2_094_000,
                target_percent=0.138,
                desired_value=1_750_000,
                reason="target",
            ),
            PortfolioTargetPlan(
                target=stabilizer_target,
                current_quantity=1,
                current_price=660_000,
                current_value=660_000,
                target_percent=0.08,
                desired_value=1_013_913.04,
                reason="target",
            ),
        ),
        metadata={"reused": True, "source_batch_id": "portfolio-targets-1"},
    )
    portfolio = Portfolio(
        cash=10_613_913.04347826,
        holdings={
            symbol.key: Holding(symbol, quantity=2, average_price=671_778),
            stabilizer.key: Holding(stabilizer, quantity=1, average_price=660_000),
        },
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            reused_target_churn_guard=True,
            reused_target_churn_lot_fraction=0.5,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 700_000), _bar(stabilizer, 660_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["lot_optimizer_adjustment_count"] == 1
    assert sized.metadata["reused_target_churn_suppressed_count"] == 1


def test_order_sizing_reused_batch_churn_guard_keeps_fresh_and_exit_targets():
    symbol = Symbol("005380", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.138)
    fresh_batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 13, 30),
        targets=(target,),
    )
    portfolio = Portfolio(
        cash=10_581_159.420289854,
        holdings={symbol.key: Holding(symbol, quantity=3, average_price=671_778)},
    )
    engine = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            reused_target_churn_guard=True,
            reused_target_churn_lot_fraction=0.5,
        )
    )

    fresh_sized = engine.size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 700_000)),
            portfolio=portfolio,
            portfolio_targets=fresh_batch,
        )
    )
    assert fresh_sized.targets == (PortfolioTarget(symbol=symbol, quantity=2),)

    exit_target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.0, tag="framework:stop:flat")
    reused_exit = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 13, 31),
        targets=(exit_target,),
        metadata={"reused": True},
    )
    exit_sized = engine.size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 700_000)),
            portfolio=portfolio,
            portfolio_targets=reused_exit,
        )
    )
    assert exit_sized.targets == (PortfolioTarget(symbol=symbol, quantity=0, tag="framework:stop:flat"),)


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

    assert batch.targets == (PortfolioAllocationTarget(symbol=symbol, target_percent=1.0, tag="static"),)
    assert len(batch.plans) == 1
    sized = OrderSizingEngine(rebalance_policy=RebalancePolicy(min_order_notional=200.0)).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 100.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )
    assert sized.targets == ()
    assert sized.plans == ()
    assert batch.metadata["raw_target_count"] == 1
    assert sized.metadata["filtered_target_count"] == 0
    assert batch.metadata["raw_plan_count"] == 1
    assert sized.metadata["filtered_plan_count"] == 0


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

    assert batch.targets == (PortfolioAllocationTarget(symbol=symbol, target_percent=0.0, tag="exit"),)
    assert len(batch.plans) == 1
    sized = OrderSizingEngine(rebalance_policy=RebalancePolicy(min_order_notional=1_000.0)).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 100.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )
    assert sized.targets == (target,)
    assert sized.plans[0].current_quantity == 1
    assert sized.plans[0].target_quantity == 0
    assert sized.plans[0].delta_quantity == -1
    assert sized.plans[0].is_exit is True


def test_equal_weight_model_keeps_current_holding_without_active_insight():
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

    assert batch.targets == ()
    assert batch.plans == ()
    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(held, 30.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )
    assert sized.targets == ()
    assert sized.plans == ()


def test_equal_weight_model_flattens_current_holding_with_explicit_flat_insight():
    held = Symbol("HELD", "US")
    now = datetime(2026, 5, 9, 9, 30)
    portfolio = Portfolio(cash=100, holdings={held.key: Holding(held, quantity=3, average_price=25.0)})
    engine = PortfolioConstructionEngine(model=EqualWeightPortfolioConstructionModel())

    flat = Insight(
        sleeve_id="test-sleeve",
        symbol=held,
        direction=InsightDirection.FLAT,
        generated_at=now,
        expires_at=now + timedelta(days=1),
        source_snapshot_id="snapshot-1",
        alpha_id="exit-alpha",
        alpha_version="1.0",
    )
    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(held, 30.0, as_of=now)),
            portfolio=portfolio,
            active_insights=(flat,),
            managed_symbols=(),
        )
    )

    assert batch.targets == (PortfolioAllocationTarget(symbol=held, target_percent=0.0, tag="framework:exit-alpha:flat"),)
    assert batch.plans[0].current_quantity == 3
    assert batch.plans[0].current_price == 30.0
    assert batch.plans[0].current_value == pytest.approx(90.0)
    assert batch.plans[0].desired_value == pytest.approx(0.0)
    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(held, 30.0, as_of=now)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )
    assert sized.targets == (PortfolioTarget(symbol=held, quantity=0, tag="framework:exit-alpha:flat"),)
    assert sized.plans[0].delta_quantity == -3


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
    assert payload["plans"][0]["target_percent"] == pytest.approx(0.1)
    assert payload["plans"][0]["desired_value"] == pytest.approx(100.0)
    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 50.0)),
            portfolio=Portfolio(cash=1_000),
            portfolio_targets=batch,
        )
    )
    sized_payload = sized.to_dict()
    assert sized_payload["plans"][0]["target_quantity"] == 2
    assert sized_payload["plans"][0]["delta_quantity"] == 2
    assert sized_payload["plans"][0]["rounded_value"] == pytest.approx(100.0)

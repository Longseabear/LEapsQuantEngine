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


def test_portfolio_construction_excludes_unheld_insight_missing_current_price_before_budgeting():
    priced = Symbol("AAA", "US")
    missing = Symbol("MISS", "US")
    data = _slice(_bar(priced, 100.0))
    priced_insight = _insight(priced)
    missing_insight = _insight(missing)
    engine = PortfolioConstructionEngine(model=EqualWeightPortfolioConstructionModel())

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=Portfolio(cash=1_000),
            active_insights=(priced_insight, missing_insight),
            managed_symbols=(),
        )
    )

    assert batch.targets == (
        PortfolioAllocationTarget(symbol=priced, target_percent=1.0, tag="framework:alpha-a:up"),
    )
    assert batch.plans[0].desired_value == pytest.approx(1_000.0)
    assert batch.source_insight_ids == (priced_insight.insight_id,)
    assert batch.metadata["raw_active_insight_count"] == 2
    assert batch.metadata["targetable_active_insight_count"] == 1
    assert batch.metadata["excluded_universe_mismatch_insight_count"] == 1
    assert batch.metadata["excluded_universe_mismatch_symbols"] == ["US:MISS"]
    assert batch.metadata["excluded_universe_mismatch_insight_ids"] == [missing_insight.insight_id]


def test_portfolio_construction_parks_reused_unheld_target_missing_current_price():
    priced = Symbol("AAA", "US")
    missing = Symbol("MISS", "US")
    source_batch = PortfolioTargetBatch(
        sleeve_id="test-sleeve",
        generated_at=datetime(2026, 5, 9, 9, 0),
        targets=(
            PortfolioAllocationTarget(symbol=priced, target_percent=0.5),
            PortfolioAllocationTarget(symbol=missing, target_percent=0.5),
        ),
    )
    engine = PortfolioConstructionEngine(model=EqualWeightPortfolioConstructionModel())

    batch = engine.build_target_batch_from_targets(
        PortfolioConstructionContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(priced, 100.0)),
            portfolio=Portfolio(cash=1_000),
            active_insights=(),
            managed_symbols=(),
        ),
        source_batch,
        source_batch.targets,
        metadata={"reused": True},
    )

    assert batch.targets == (PortfolioAllocationTarget(symbol=priced, target_percent=0.5),)
    assert batch.plans[0].desired_value == pytest.approx(500.0)
    assert batch.metadata["raw_target_count"] == 2
    assert batch.metadata["filtered_target_count"] == 1
    assert batch.metadata["parked_unpriced_target_count"] == 1
    assert batch.metadata["parked_unpriced_target_symbols"] == ["US:MISS"]


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


def test_order_sizing_keeps_meaningful_reused_batch_one_share_rebalance():
    symbol = Symbol("036930", "KRX")
    expensive = Symbol("009150", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.2375)
    expensive_target = PortfolioAllocationTarget(symbol=expensive, target_percent=0.12)
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 28, 14, 30),
        targets=(target, expensive_target),
        metadata={"reused": True, "source_batch_id": "portfolio-targets-1"},
    )
    portfolio = Portfolio(
        cash=6_800_000,
        holdings={symbol.key: Holding(symbol, quantity=10, average_price=217_000)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            min_order_notional=50_000,
            reused_target_churn_guard=True,
            reused_target_churn_lot_fraction=0.25,
            reused_target_churn_equity_bps=5.0,
            whole_share_entry_floor_min_fraction=0.75,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 200_000), _bar(expensive, 1_500_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert PortfolioTarget(symbol=symbol, quantity=11) in sized.targets
    assert sized.metadata["reused_target_churn_suppressed_count"] == 0
    assert sized.metadata["below_min_notional_suppressed_count"] == 0


def test_order_sizing_metadata_explains_min_notional_and_zero_delta_filters():
    zero_delta_symbol = Symbol("005930", "KRX")
    small_delta_symbol = Symbol("000660", "KRX")
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 28, 14, 30),
        targets=(
            PortfolioAllocationTarget(symbol=zero_delta_symbol, target_percent=5 / 12),
            PortfolioAllocationTarget(symbol=small_delta_symbol, target_percent=0.011),
        ),
    )
    portfolio = Portfolio(
        cash=700_000,
        holdings={zero_delta_symbol.key: Holding(zero_delta_symbol, quantity=5, average_price=100_000)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(min_order_notional=50_000)
    ).size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(zero_delta_symbol, 100_000), _bar(small_delta_symbol, 10_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["zero_delta_symbols"] == ["KRX:005930"]
    assert sized.metadata["below_min_notional_suppressed_symbols"] == ["KRX:000660"]


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


def test_order_sizing_reused_churn_guard_does_not_use_min_order_notional_as_noise_threshold():
    symbol = Symbol("069500", "KRX")
    price = 129_375.0
    current_quantity = 22
    current_value = current_quantity * price
    target_value = 15_000_000.0
    desired_value = current_value - 80_000.0
    target = PortfolioAllocationTarget(
        symbol=symbol,
        target_percent=desired_value / target_value,
        tag="agent_narrative_target:id=test-reused",
    )
    batch = PortfolioTargetBatch(
        sleeve_id="semiconduct-kor",
        generated_at=datetime(2026, 5, 28, 14, 30),
        targets=(target,),
        metadata={"reused": True},
    )
    portfolio = Portfolio(
        cash=target_value - current_value,
        holdings={symbol.key: Holding(symbol, quantity=current_quantity, average_price=129_290.0)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            min_order_notional=100_000.0,
            reused_target_churn_guard=True,
            reused_target_churn_max_quantity_delta=1,
            reused_target_churn_lot_fraction=0.25,
            reused_target_churn_equity_bps=5.0,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="semiconduct-kor",
            data=_slice(_bar(symbol, price)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == (
        PortfolioTarget(symbol=symbol, quantity=21, tag="agent_narrative_target:id=test-reused"),
    )
    assert sized.metadata["below_min_notional_count"] == 0
    assert sized.metadata["reused_target_churn_suppressed_count"] == 0


def test_order_sizing_reused_churn_guard_still_suppresses_tiny_adjacent_lot_noise():
    symbol = Symbol("069500", "KRX")
    price = 129_375.0
    current_quantity = 22
    current_value = current_quantity * price
    target_value = 15_000_000.0
    desired_value = current_value - 20_000.0
    target = PortfolioAllocationTarget(
        symbol=symbol,
        target_percent=desired_value / target_value,
        tag="agent_narrative_target:id=test-reused",
    )
    batch = PortfolioTargetBatch(
        sleeve_id="semiconduct-kor",
        generated_at=datetime(2026, 5, 28, 14, 31),
        targets=(target,),
        metadata={"reused": True},
    )
    portfolio = Portfolio(
        cash=target_value - current_value,
        holdings={symbol.key: Holding(symbol, quantity=current_quantity, average_price=129_290.0)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            min_order_notional=100_000.0,
            reused_target_churn_guard=True,
            reused_target_churn_max_quantity_delta=1,
            reused_target_churn_lot_fraction=0.25,
            reused_target_churn_equity_bps=5.0,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="semiconduct-kor",
            data=_slice(_bar(symbol, price)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["below_min_notional_count"] == 0
    assert sized.metadata["reused_target_churn_suppressed_count"] == 1
    assert sized.metadata["reused_target_churn_suppressed_symbols"] == ["KRX:069500"]
    detail = sized.metadata["reused_target_churn_suppressed_details"][0]
    assert detail["drift_notional"] == 20_000.0
    assert detail["threshold_notional"] == price * 0.25


def test_order_sizing_records_zero_delta_and_below_min_notional_metadata():
    zero_symbol = Symbol("009150", "KRX")
    small_symbol = Symbol("091160", "KRX")
    zero_target = PortfolioAllocationTarget(symbol=zero_symbol, target_percent=1_500_000.0 / 10_500_000.0)
    small_target = PortfolioAllocationTarget(symbol=small_symbol, target_percent=50.0 / 10_500_000.0)
    batch = PortfolioTargetBatch(
        sleeve_id="semiconduct-kor",
        generated_at=datetime(2026, 5, 28, 14, 32),
        targets=(zero_target, small_target),
    )
    portfolio = Portfolio(
        cash=9_000_000.0,
        holdings={zero_symbol.key: Holding(zero_symbol, quantity=1, average_price=1_500_000.0)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(min_order_notional=200_000.0)
    ).size(
        OrderSizingContext(
            sleeve_id="semiconduct-kor",
            data=_slice(_bar(zero_symbol, 1_500_000.0), _bar(small_symbol, 50.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["zero_delta_symbols"] == ["KRX:009150"]
    assert sized.metadata["below_min_notional_symbols"] == ["KRX:091160"]
    assert sized.metadata["below_min_notional_details"][0]["symbol"] == "KRX:091160"
    assert sized.metadata["below_min_notional_details"][0]["delta_notional"] == 50.0
    assert sized.metadata["below_min_notional_details"][0]["threshold_notional"] == 200_000.0


def test_order_sizing_keeps_meaningful_entry_but_suppresses_small_lowvol_rebalance_noise():
    add_symbol = Symbol("050890", "KRX")
    small_add = Symbol("005290", "KRX")
    tiny_add = Symbol("055550", "KRX")
    targets = (
        PortfolioAllocationTarget(symbol=add_symbol, target_percent=0.03454251577095184),
        PortfolioAllocationTarget(symbol=small_add, target_percent=0.055446442618563636),
        PortfolioAllocationTarget(symbol=tiny_add, target_percent=0.0955425416751486),
    )
    batch = PortfolioTargetBatch(
        sleeve_id="kr-lowvol-defensive",
        generated_at=datetime(2026, 5, 28, 14, 45),
        targets=targets,
        metadata={"reused": True},
    )
    portfolio = Portfolio(
        cash=11_135_262.0,
        holdings={
            small_add.key: Holding(small_add, quantity=10, average_price=61_960.0),
            tiny_add.key: Holding(tiny_add, quantity=11, average_price=96_661.11),
        },
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            cash_reserve_pct=0.02,
            min_order_notional=150_000.0,
            min_order_notional_equity_bps=200.0,
            reused_target_churn_guard=True,
            reused_target_churn_max_quantity_delta=2,
            reused_target_churn_lot_fraction=1.0,
            reused_target_churn_equity_bps=50.0,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="kr-lowvol-defensive",
            data=_slice(
                _bar(add_symbol, 15_570.0),
                _bar(small_add, 56_900.0),
                _bar(tiny_add, 92_800.0),
            ),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert [target.symbol.key for target in sized.targets] == ["KRX:050890"]
    assert sized.targets[0].quantity >= 27
    assert sized.metadata["below_min_notional_symbols"] == ["KRX:005290", "KRX:055550"]
    assert sized.metadata["below_min_notional_details"][0]["threshold_notional"] == pytest.approx(249_411.20)


def test_order_sizing_target_churn_guard_suppresses_fresh_small_retargeting_noise():
    symbol = Symbol("005380", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.138)
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 13, 30),
        targets=(target,),
    )
    portfolio = Portfolio(
        cash=10_581_159.420289854,
        holdings={symbol.key: Holding(symbol, quantity=3, average_price=671_778)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            target_churn_guard=True,
            target_churn_lot_fraction=0.5,
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
    assert sized.metadata["target_churn_guard_enabled"] is True
    assert sized.metadata["target_churn_suppressed_count"] == 1
    assert sized.metadata["target_churn_suppressed_symbols"] == ["KRX:005380"]


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


def test_order_sizing_filters_small_delta_by_equity_bps_threshold():
    symbol = Symbol("AAA", "US")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=1.0)
    batch = PortfolioTargetBatch(
        sleeve_id="test-sleeve",
        generated_at=datetime(2026, 5, 13, 13, 30),
        targets=(target,),
    )
    portfolio = Portfolio(cash=50.0, holdings={symbol.key: Holding(symbol, quantity=19, average_price=50.0)})

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(min_order_notional=0.0, min_order_notional_equity_bps=1_000.0)
    ).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 50.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["min_order_notional"] == 0.0
    assert sized.metadata["min_order_notional_equity_bps"] == 1_000.0


def test_order_sizing_reports_min_quantity_delta_suppressed_targets():
    symbol = Symbol("AAA", "US")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=1.0)
    batch = PortfolioTargetBatch(
        sleeve_id="test-sleeve",
        generated_at=datetime(2026, 5, 13, 13, 30),
        targets=(target,),
    )
    portfolio = Portfolio(cash=100.0, holdings={symbol.key: Holding(symbol, quantity=9, average_price=100.0)})

    sized = OrderSizingEngine(rebalance_policy=RebalancePolicy(min_quantity_delta=2)).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 100.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["min_quantity_delta_suppressed_count"] == 1
    assert sized.metadata["min_quantity_delta_suppressed_symbols"] == ["US:AAA"]
    detail = sized.metadata["min_quantity_delta_suppressed_details"][0]
    assert detail["delta_quantity"] == 1
    assert detail["delta_notional"] == 100.0
    assert detail["threshold_notional"] == 2.0


def test_order_sizing_allows_meaningful_one_share_rebalance_when_min_quantity_is_one():
    symbol = Symbol("005930", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=1.0)
    batch = PortfolioTargetBatch(
        sleeve_id="kr-domestic-4401",
        generated_at=datetime(2026, 5, 28, 9, 5),
        targets=(target,),
    )
    portfolio = Portfolio(
        cash=500_000.0,
        holdings={symbol.key: Holding(symbol, quantity=2, average_price=293_750.0)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            min_quantity_delta=1,
            min_order_notional=50_000.0,
            min_order_notional_equity_bps=50.0,
            target_churn_guard=True,
            target_churn_equity_bps=50.0,
            target_churn_lot_fraction=1.0,
            reused_target_churn_guard=True,
            reused_target_churn_equity_bps=50.0,
            reused_target_churn_lot_fraction=1.0,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="kr-domestic-4401",
            data=_slice(_bar(symbol, 304_500.0)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == (PortfolioTarget(symbol=symbol, quantity=3),)
    assert sized.metadata["min_quantity_delta_suppressed_count"] == 0


def test_order_sizing_can_floor_fractional_entry_to_one_whole_share():
    symbol = Symbol("009150", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.035)
    batch = PortfolioTargetBatch(
        sleeve_id="test-sleeve",
        generated_at=datetime(2026, 5, 13, 13, 30),
        targets=(target,),
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(whole_share_entry_floor_min_fraction=0.35)
    ).size(
        OrderSizingContext(
            sleeve_id="test-sleeve",
            data=_slice(_bar(symbol, 1_300_000.0)),
            portfolio=Portfolio(cash=18_000_000),
            portfolio_targets=batch,
        )
    )

    assert sized.targets == (PortfolioTarget(symbol=symbol, quantity=1),)
    assert round(sized.plans[0].desired_value, 2) == 630_000.0
    assert sized.plans[0].rounded_value == 1_300_000.0


def test_order_sizing_suppresses_one_share_rounding_exit_for_positive_target():
    symbol = Symbol("000660", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.10)
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 4, 21, 12, 0),
        targets=(target,),
    )
    portfolio = Portfolio(
        cash=7_200_000,
        holdings={symbol.key: Holding(symbol, quantity=1, average_price=1_217_500)},
    )

    sized = OrderSizingEngine(
        rebalance_policy=RebalancePolicy(
            whole_share_rounding_churn_guard=True,
            whole_share_rounding_churn_min_fraction=0.25,
        )
    ).size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 1_217_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == ()
    assert sized.metadata["whole_share_rounding_churn_suppressed_count"] == 1
    assert sized.metadata["whole_share_rounding_churn_suppressed_symbols"] == ["KRX:000660"]


def test_order_sizing_keeps_explicit_zero_exit_despite_rounding_guard():
    symbol = Symbol("000660", "KRX")
    target = PortfolioAllocationTarget(symbol=symbol, target_percent=0.0, tag="agent_daily_target:missing_from_daily_artifact")
    batch = PortfolioTargetBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 4, 22, 9, 0),
        targets=(target,),
    )
    portfolio = Portfolio(
        cash=7_200_000,
        holdings={symbol.key: Holding(symbol, quantity=1, average_price=1_217_500)},
    )

    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=_slice(_bar(symbol, 1_232_000)),
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert sized.targets == (PortfolioTarget(symbol=symbol, quantity=0, tag="agent_daily_target:missing_from_daily_artifact"),)
    assert sized.metadata["whole_share_rounding_churn_suppressed_count"] == 0


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

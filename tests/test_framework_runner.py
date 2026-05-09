from datetime import datetime

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.framework import (
    EqualWeightPortfolioConstructionModel,
    FrameworkRunner,
    PassThroughRiskManagementModel,
)
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue


class OneShotAlpha:
    alpha_id = "one-shot"
    version = "1.0"

    def __init__(self, insight: Insight | None):
        self.insight = insight

    def generate(self, context):
        return [self.insight] if self.insight is not None else []


def _snapshot(as_of: datetime, symbol: Symbol) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id=f"indicator-{as_of:%H%M}",
        sleeve_id="us-live",
        universe_id="us-active",
        as_of=as_of,
        created_at=as_of,
        symbols=(symbol.key,),
        source_snapshot_id=f"market-{as_of:%H%M}",
        values={
            symbol.key: {
                "close": IndicatorValue("close", 100.0, True, 1, as_of),
            }
        },
    )


def _slice(as_of: datetime, symbol: Symbol, close: float = 100.0) -> DataSlice:
    return DataSlice(
        time=as_of,
        bars={symbol.key: Bar(symbol, as_of, close, close, close, close, 1000)},
    )


def test_framework_runner_turns_active_insight_into_order_intent():
    symbol = Symbol("NVDA", "US")
    as_of = datetime(2026, 5, 9, 9, 30)
    insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=as_of,
        expires_at=datetime(2026, 5, 9, 10, 0),
        source_snapshot_id="market-0930",
        alpha_id="one-shot",
        alpha_version="1.0",
        weight=0.5,
    )
    runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(insight),)),
        portfolio_model=EqualWeightPortfolioConstructionModel(),
        risk_model=PassThroughRiskManagementModel(),
    )

    result = runner.run_once(
        indicator_snapshot=_snapshot(as_of, symbol),
        data=_slice(as_of, symbol, close=100.0),
        portfolio=Portfolio(cash=1_000),
    )

    assert result.new_insight_batch.insight_count == 1
    assert result.active_insight_count == 1
    assert result.portfolio_targets[0].quantity == 5
    assert result.risk_decisions.approved_targets == result.portfolio_targets
    assert len(result.order_intents) == 1
    assert result.order_intents[0].side is OrderSide.BUY
    assert result.order_intents[0].quantity == 5
    summary = result.to_dict(include_details=False)
    assert summary["new_insights"]["insight_count"] == 1
    assert "insights" not in summary["new_insights"]
    assert summary["active_insights"] == []


def test_framework_runner_expires_insight_and_flattens_managed_symbol():
    symbol = Symbol("NVDA", "US")
    first_time = datetime(2026, 5, 9, 9, 30)
    second_time = datetime(2026, 5, 9, 9, 36)
    insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=first_time,
        expires_at=datetime(2026, 5, 9, 9, 35),
        source_snapshot_id="market-0930",
        alpha_id="one-shot",
        alpha_version="1.0",
        weight=0.5,
    )
    runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(insight),)),
        portfolio_model=EqualWeightPortfolioConstructionModel(),
        risk_model=PassThroughRiskManagementModel(),
    )
    portfolio = Portfolio(cash=1_000)

    first = runner.run_once(
        indicator_snapshot=_snapshot(first_time, symbol),
        data=_slice(first_time, symbol),
        portfolio=portfolio,
    )
    for order in first.order_intents:
        portfolio.apply_fill(order)
    runner.alpha_runtime.replace_active([OneShotAlpha(None)])

    second = runner.run_once(
        indicator_snapshot=_snapshot(second_time, symbol),
        data=_slice(second_time, symbol),
        portfolio=portfolio,
    )

    assert second.active_insight_count == 0
    assert second.insight_manager_update.expired_count == 1
    assert second.portfolio_targets[0].quantity == 0
    assert second.order_intents[0].side is OrderSide.SELL
    assert second.order_intents[0].quantity == 5

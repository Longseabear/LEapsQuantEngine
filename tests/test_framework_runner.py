from datetime import datetime

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.framework import (
    EqualWeightPortfolioConstructionModel,
    FileFrameworkRunnerStateStore,
    FrameworkRunner,
    PassThroughRiskManagementModel,
    PortfolioConstructionEngine,
    RebalancePolicy,
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


class SelectedSymbolsAlpha:
    alpha_id = "selected-symbols"
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
                weight=0.5,
                reason="selected_symbol",
            )
            for symbol_key in context.symbol_keys
        ]


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


def _multi_snapshot(as_of: datetime, symbols: tuple[Symbol, ...]) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id=f"indicator-{as_of:%H%M}",
        sleeve_id="us-live",
        universe_id="us-active",
        as_of=as_of,
        created_at=as_of,
        symbols=tuple(symbol.key for symbol in symbols),
        source_snapshot_id=f"market-{as_of:%H%M}",
        values={
            symbol.key: {
                "close": IndicatorValue("close", 100.0, True, 1, as_of),
            }
            for symbol in symbols
        },
    )


def _multi_slice(as_of: datetime, symbols: tuple[Symbol, ...], close: float = 100.0) -> DataSlice:
    return DataSlice(
        time=as_of,
        bars={
            symbol.key: Bar(symbol, as_of, close, close, close, close, 1000)
            for symbol in symbols
        },
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
    assert result.portfolio_target_batch.target_count == 1
    assert result.portfolio_target_batch.source_insight_ids == (insight.insight_id,)
    assert result.portfolio_target_batch.targets[0].target_percent == 1.0
    assert result.order_sizing_batch.targets[0].quantity == 10
    assert result.portfolio_targets[0].quantity == 10
    assert result.risk_decisions.approved_targets == result.portfolio_targets
    assert len(result.order_intents) == 1
    assert result.order_intents[0].side is OrderSide.BUY
    assert result.order_intents[0].quantity == 10
    summary = result.to_dict(include_details=False)
    assert summary["new_insights"]["insight_count"] == 1
    assert "insights" not in summary["new_insights"]
    assert summary["portfolio_target_batch"]["target_count"] == 1
    assert summary["order_sizing"]["target_count"] == 1
    assert summary["active_insights"] == []


def test_framework_runner_scopes_alpha_with_selection_symbols():
    symbols = (Symbol("NVDA", "US"), Symbol("MSFT", "US"))
    selected = (symbols[1],)
    as_of = datetime(2026, 5, 9, 9, 30)
    runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(SelectedSymbolsAlpha(),)),
        portfolio_model=EqualWeightPortfolioConstructionModel(),
        risk_model=PassThroughRiskManagementModel(),
    )

    result = runner.run_once(
        indicator_snapshot=_multi_snapshot(as_of, symbols),
        data=_multi_slice(as_of, symbols, close=100.0),
        portfolio=Portfolio(cash=1_000),
        alpha_symbols_by_model={"selected-symbols": selected},
    )

    assert [insight.symbol for insight in result.new_insight_batch.insights] == [symbols[1]]
    assert result.portfolio_target_batch.target_count == 1
    assert result.order_intents[0].symbol == symbols[1]


def test_framework_runner_expires_insight_and_keeps_current_holding_without_exit_signal():
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
    assert second.portfolio_targets == ()
    assert second.order_intents == ()


def test_framework_runner_reuses_portfolio_targets_until_rebalance_cadence_due():
    symbol = Symbol("NVDA", "US")
    first_time = datetime(2026, 5, 9, 9, 30)
    second_time = datetime(2026, 5, 9, 9, 31)
    insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=first_time,
        expires_at=datetime(2026, 5, 10, 9, 30),
        source_snapshot_id="market-0930",
        alpha_id="daily-alpha",
        alpha_version="1.0",
    )
    alpha = OneShotAlpha(insight)
    alpha.alpha_id = "daily-alpha"
    alpha.evaluation_cadence = "once_per_day"
    runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(alpha,)),
        portfolio_engine=PortfolioConstructionEngine(
            model=EqualWeightPortfolioConstructionModel(),
            rebalance_policy=RebalancePolicy(cadence="once_per_day"),
        ),
        risk_model=PassThroughRiskManagementModel(),
    )
    portfolio = Portfolio(cash=1_000)

    first = runner.run_once(
        indicator_snapshot=_snapshot(first_time, symbol),
        data=_slice(first_time, symbol, close=100.0),
        portfolio=portfolio,
    )
    for order in first.order_intents:
        portfolio.apply_fill(order)

    second = runner.run_once(
        indicator_snapshot=_snapshot(second_time, symbol),
        data=_slice(second_time, symbol, close=100.0),
        portfolio=portfolio,
    )

    assert second.new_insight_batch.insight_count == 0
    assert second.active_insight_count == 1
    assert second.portfolio_target_batch.metadata["reused"] is True
    assert second.stage_decisions["portfolio"]["ran"] is False
    assert second.order_intents == ()


def test_framework_runner_restores_rebalance_state_across_run_once_processes(tmp_path):
    symbol = Symbol("NVDA", "US")
    first_time = datetime(2026, 5, 9, 9, 30)
    second_time = datetime(2026, 5, 9, 9, 31)
    first_insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=first_time,
        expires_at=datetime(2026, 5, 10, 9, 30),
        source_snapshot_id="market-0930",
        alpha_id="fast-alpha",
        alpha_version="1.0",
    )
    second_insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=second_time,
        expires_at=datetime(2026, 5, 10, 9, 30),
        source_snapshot_id="market-0931",
        alpha_id="fast-alpha",
        alpha_version="1.0",
    )
    store = FileFrameworkRunnerStateStore(tmp_path / "framework-state.json")
    portfolio = Portfolio(cash=1_000)
    first_runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(first_insight),)),
        portfolio_engine=PortfolioConstructionEngine(
            model=EqualWeightPortfolioConstructionModel(),
            rebalance_policy=RebalancePolicy(cadence="every_5m"),
        ),
        risk_model=PassThroughRiskManagementModel(),
    )

    first = first_runner.run_once(
        indicator_snapshot=_snapshot(first_time, symbol),
        data=_slice(first_time, symbol, close=100.0),
        portfolio=portfolio,
    )
    store.save(first_runner.export_state(as_of=first_time))

    second_runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(second_insight),)),
        portfolio_engine=PortfolioConstructionEngine(
            model=EqualWeightPortfolioConstructionModel(),
            rebalance_policy=RebalancePolicy(cadence="every_5m"),
        ),
        risk_model=PassThroughRiskManagementModel(),
    )
    second_runner.restore_state(store.load())

    second = second_runner.run_once(
        indicator_snapshot=_snapshot(second_time, symbol),
        data=_slice(second_time, symbol, close=100.0),
        portfolio=portfolio,
    )

    assert first.stage_decisions["portfolio"]["ran"] is True
    assert second.new_insight_batch.insight_count == 1
    assert second.active_insight_count == 1
    assert second.portfolio_target_batch.batch_id == first.portfolio_target_batch.batch_id
    assert second.portfolio_target_batch.metadata["reused"] is True
    assert second.stage_decisions["portfolio"]["ran"] is False


def test_framework_runner_runs_portfolio_after_minute_rebalance_cadence():
    symbol = Symbol("NVDA", "US")
    first_time = datetime(2026, 5, 9, 9, 30)
    four_minutes_later = datetime(2026, 5, 9, 9, 34)
    five_minutes_later = datetime(2026, 5, 9, 9, 35)
    insight = Insight(
        sleeve_id="us-live",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=first_time,
        expires_at=datetime(2026, 5, 10, 9, 30),
        source_snapshot_id="market-0930",
        alpha_id="daily-alpha",
        alpha_version="1.0",
    )
    alpha = OneShotAlpha(insight)
    alpha.alpha_id = "daily-alpha"
    alpha.evaluation_cadence = "once_per_day"
    runner = FrameworkRunner(
        sleeve_id="us-live",
        alpha_runtime=AlphaRuntime(active_models=(alpha,)),
        portfolio_engine=PortfolioConstructionEngine(
            model=EqualWeightPortfolioConstructionModel(),
            rebalance_policy=RebalancePolicy(cadence="every_5_minutes"),
        ),
        risk_model=PassThroughRiskManagementModel(),
    )
    portfolio = Portfolio(cash=1_000)

    first = runner.run_once(
        indicator_snapshot=_snapshot(first_time, symbol),
        data=_slice(first_time, symbol, close=100.0),
        portfolio=portfolio,
    )
    for order in first.order_intents:
        portfolio.apply_fill(order)

    reused = runner.run_once(
        indicator_snapshot=_snapshot(four_minutes_later, symbol),
        data=_slice(four_minutes_later, symbol, close=100.0),
        portfolio=portfolio,
    )
    refreshed = runner.run_once(
        indicator_snapshot=_snapshot(five_minutes_later, symbol),
        data=_slice(five_minutes_later, symbol, close=100.0),
        portfolio=portfolio,
    )

    assert reused.stage_decisions["portfolio"]["cadence"] == "every_5_minutes"
    assert reused.stage_decisions["portfolio"]["ran"] is False
    assert reused.portfolio_target_batch.metadata["reused"] is True
    assert refreshed.stage_decisions["portfolio"]["ran"] is True
    assert refreshed.portfolio_target_batch.metadata.get("reused") is None

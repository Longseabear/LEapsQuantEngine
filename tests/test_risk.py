from datetime import datetime

import pytest

from leaps_quant_engine.framework import (
    BasicRiskManagementModel,
    DailyLossLimitRiskModel,
    MaxDrawdownRiskModel,
    PassThroughRiskManagementModel,
    RiskDecisionStatus,
    RiskLimits,
    RiskManagementContext,
)
from leaps_quant_engine.models import Bar, DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, RuntimeModelStateView
from leaps_quant_engine.snapshots import SnapshotQualityReport, SnapshotQualityStatus


def _slice(symbol: Symbol, close: float = 100.0) -> DataSlice:
    as_of = datetime(2026, 5, 9, 9, 30)
    return DataSlice(
        time=as_of,
        bars={symbol.key: Bar(symbol, as_of, close, close, close, close, 1000)},
    )


def _context(
    *,
    portfolio: Portfolio,
    target: PortfolioTarget,
    close: float = 100.0,
    snapshot_quality: SnapshotQualityReport | None = None,
) -> RiskManagementContext:
    return RiskManagementContext(
        sleeve_id="test-sleeve",
        data=_slice(target.symbol, close),
        portfolio=portfolio,
        targets=(target,),
        snapshot_quality=snapshot_quality,
    )


def _quality(status: SnapshotQualityStatus) -> SnapshotQualityReport:
    return SnapshotQualityReport(
        status=status,
        complete_ratio=1.0 if status is SnapshotQualityStatus.FRESH else 0.8,
        age_seconds=1.0,
        collection_seconds=1.0,
        requested_symbol_count=1,
        collected_symbol_count=1 if status is not SnapshotQualityStatus.INVALID else 0,
        failed_symbol_count=0 if status is not SnapshotQualityStatus.INVALID else 1,
        reasons=(),
    )


def test_pass_through_risk_model_approves_targets():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=2, tag="target")

    batch = PassThroughRiskManagementModel().manage_risk(
        _context(portfolio=Portfolio(cash=1_000), target=target)
    )

    assert batch.approved_targets == (target,)
    assert batch.decisions[0].status is RiskDecisionStatus.APPROVED
    assert batch.to_dict()["approved_count"] == 1


def test_basic_risk_rejects_short_targets_when_long_only():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=-3, tag="short")

    batch = BasicRiskManagementModel().manage_risk(
        _context(portfolio=Portfolio(cash=1_000), target=target)
    )

    assert batch.approved_targets == ()
    assert batch.decisions[0].status is RiskDecisionStatus.REJECTED
    assert batch.decisions[0].reason == "short_target_rejected"


def test_basic_risk_clamps_target_to_max_position_pct():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=10, tag="target")
    model = BasicRiskManagementModel(limits=RiskLimits(max_position_pct=0.25))

    batch = model.manage_risk(_context(portfolio=Portfolio(cash=1_000), target=target))

    assert batch.approved_targets == (PortfolioTarget(symbol=symbol, quantity=2, tag="target"),)
    assert batch.decisions[0].status is RiskDecisionStatus.CLAMPED
    assert batch.decisions[0].metadata["max_position_pct"] == pytest.approx(0.25)


def test_basic_risk_clamps_buy_to_available_cash_after_buffer():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=10, tag="target")
    model = BasicRiskManagementModel(limits=RiskLimits(cash_buffer_pct=0.25))

    batch = model.manage_risk(_context(portfolio=Portfolio(cash=450), target=target))

    assert batch.approved_targets == (PortfolioTarget(symbol=symbol, quantity=3, tag="target"),)
    assert batch.decisions[0].status is RiskDecisionStatus.CLAMPED
    assert batch.decisions[0].metadata["cash_buffer_pct"] == pytest.approx(0.25)


def test_basic_risk_allows_reducing_positions_without_cash():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=1, tag="reduce")
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=5, average_price=80.0)})

    batch = BasicRiskManagementModel().manage_risk(_context(portfolio=portfolio, target=target))

    assert batch.approved_targets == (target,)
    assert batch.decisions[0].status is RiskDecisionStatus.APPROVED


def test_basic_risk_blocks_new_entries_when_snapshot_is_not_fresh():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=1, tag="entry")

    batch = BasicRiskManagementModel().manage_risk(
        _context(
            portfolio=Portfolio(cash=1_000),
            target=target,
            snapshot_quality=_quality(SnapshotQualityStatus.STALE),
        )
    )

    assert batch.approved_targets == ()
    assert batch.decisions[0].status is RiskDecisionStatus.REJECTED
    assert batch.decisions[0].reason == "snapshot_quality_blocks_entry"


def test_basic_risk_allows_exits_when_snapshot_is_stale():
    symbol = Symbol("AAA", "US")
    target = PortfolioTarget(symbol=symbol, quantity=0, tag="exit")
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=3, average_price=100.0)})

    batch = BasicRiskManagementModel().manage_risk(
        _context(
            portfolio=portfolio,
            target=target,
            snapshot_quality=_quality(SnapshotQualityStatus.STALE),
        )
    )

    assert batch.approved_targets == (target,)
    assert batch.decisions[0].status is RiskDecisionStatus.APPROVED


def test_basic_risk_clamps_to_total_exposure_limit():
    first = Symbol("AAA", "US")
    second = Symbol("BBB", "US")
    as_of = datetime(2026, 5, 9, 9, 30)
    data = DataSlice(
        time=as_of,
        bars={
            first.key: Bar(first, as_of, 100, 100, 100, 100, 1000),
            second.key: Bar(second, as_of, 100, 100, 100, 100, 1000),
        },
    )
    portfolio = Portfolio(cash=1_000, holdings={first.key: Holding(first, quantity=4, average_price=100.0)})
    model = BasicRiskManagementModel(limits=RiskLimits(max_total_exposure_pct=0.5))

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="test-sleeve",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(second, quantity=10, tag="entry"),),
        )
    )

    assert batch.approved_targets == (PortfolioTarget(second, quantity=3, tag="entry"),)
    assert batch.decisions[0].status is RiskDecisionStatus.CLAMPED


def test_daily_loss_limit_model_rejects_new_entries_after_loss_limit():
    symbol = Symbol("AAA", "US")
    store = InMemoryRuntimeStateStore()
    state = RuntimeModelStateView(store=store, default_sleeve_id="test-sleeve")
    model = DailyLossLimitRiskModel(max_daily_loss_pct=0.05)
    context = RiskManagementContext(
        sleeve_id="test-sleeve",
        data=_slice(symbol, 100.0),
        portfolio=Portfolio(cash=940),
        targets=(PortfolioTarget(symbol, quantity=1, tag="entry"),),
        model_state=state,
    )
    store.apply_patches(
        (
            state.object_set(
                {"start_equity": 1000.0},
                model_id="daily-loss-limit",
                namespace="daily_equity",
                symbol_key=context.data.time.date().isoformat(),
            ),
        )
    )

    batch = model.manage_risk(context)

    assert batch.approved_targets == ()
    assert batch.decisions[0].status is RiskDecisionStatus.REJECTED
    assert batch.decisions[0].reason == "daily_loss_limit_exceeded"
    assert batch.state_patches[0].value["blocked"] is True


def test_daily_loss_limit_model_allows_reductions_when_blocked():
    symbol = Symbol("AAA", "US")
    store = InMemoryRuntimeStateStore()
    state = RuntimeModelStateView(store=store, default_sleeve_id="test-sleeve")
    model = DailyLossLimitRiskModel(max_daily_loss_pct=0.05)
    context = RiskManagementContext(
        sleeve_id="test-sleeve",
        data=_slice(symbol, 100.0),
        portfolio=Portfolio(cash=440, holdings={symbol.key: Holding(symbol, quantity=5, average_price=100.0)}),
        targets=(PortfolioTarget(symbol, quantity=2, tag="reduce"),),
        model_state=state,
    )
    store.apply_patches(
        (
            state.object_set(
                {"start_equity": 1000.0},
                model_id="daily-loss-limit",
                namespace="daily_equity",
                symbol_key=context.data.time.date().isoformat(),
            ),
        )
    )

    batch = model.manage_risk(context)

    assert batch.approved_targets == (PortfolioTarget(symbol, quantity=2, tag="reduce"),)
    assert batch.decisions[0].reason == "risk_reduction_allowed"


def test_max_drawdown_model_rejects_new_entries_below_peak_limit():
    symbol = Symbol("AAA", "US")
    store = InMemoryRuntimeStateStore()
    state = RuntimeModelStateView(store=store, default_sleeve_id="test-sleeve")
    model = MaxDrawdownRiskModel(max_drawdown_pct=0.10)
    store.apply_patches(
        (
            state.object_set(
                {"peak_equity": 1000.0},
                model_id="max-drawdown",
                namespace="drawdown",
            ),
        )
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="test-sleeve",
            data=_slice(symbol, 100.0),
            portfolio=Portfolio(cash=890),
            targets=(PortfolioTarget(symbol, quantity=1, tag="entry"),),
            model_state=state,
        )
    )

    assert batch.approved_targets == ()
    assert batch.decisions[0].reason == "max_drawdown_limit_exceeded"
    assert batch.state_patches[0].value["peak_equity"] == 1000.0

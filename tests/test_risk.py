from datetime import datetime

import pytest

from leaps_quant_engine.framework import (
    BasicRiskManagementModel,
    PassThroughRiskManagementModel,
    RiskDecisionStatus,
    RiskLimits,
    RiskManagementContext,
)
from leaps_quant_engine.models import Bar, DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio


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
) -> RiskManagementContext:
    return RiskManagementContext(
        sleeve_id="test-sleeve",
        data=_slice(target.symbol, close),
        portfolio=portfolio,
        targets=(target,),
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

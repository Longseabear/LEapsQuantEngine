from datetime import datetime

import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import (
    BasicRiskManagementModel,
    EqualWeightPortfolioConstructionModel,
    OrderSizingContext,
    OrderSizingEngine,
    PortfolioConstructionContext,
    PortfolioConstructionEngine,
    RiskDecisionStatus,
    RiskManagementContext,
)
from leaps_quant_engine.models import Bar, DataSlice, OrderIntent, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio


def _bar(symbol: Symbol, close: float, *, as_of: datetime = datetime(2026, 5, 9, 9, 30)) -> Bar:
    return Bar(symbol, as_of, close, close, close, close, 1000)


def _slice(*bars: Bar) -> DataSlice:
    return DataSlice(time=bars[0].time, bars={bar.symbol.key: bar for bar in bars})


def _insight(symbol: Symbol, *, as_of: datetime = datetime(2026, 5, 9, 9, 30)) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=as_of,
        source_snapshot_id="snapshot-1",
        alpha_id="alpha-a",
        alpha_version="1.0",
    )


def test_cash_book_keeps_krw_and_usd_equity_separate():
    samsung = Symbol("005930", "KRX")
    nvda = Symbol("NVDA", "NAS")
    data = _slice(_bar(samsung, 70_000.0), _bar(nvda, 100.0))
    portfolio = Portfolio(
        cash=0.0,
        cash_by_currency={"KRW": 1_000_000.0, "USD": 500.0},
        holdings={
            samsung.key: Holding(samsung, quantity=2, average_price=65_000.0),
            nvda.key: Holding(nvda, quantity=3, average_price=90.0),
        },
    )

    assert portfolio.cash_by_currency == {"KRW": 1_000_000.0, "USD": 500.0}
    assert portfolio.equity_by_currency(data) == {
        "KRW": pytest.approx(1_140_000.0),
        "USD": pytest.approx(800.0),
    }
    with pytest.raises(ValueError, match="requires a currency"):
        portfolio.equity(data)


def test_cash_book_applies_fills_to_the_symbol_currency_only():
    samsung = Symbol("005930", "KRX")
    nvda = Symbol("NVDA", "NAS")
    portfolio = Portfolio(cash=0.0, cash_by_currency={"KRW": 1_000_000.0, "USD": 500.0})

    portfolio.apply_fill(OrderIntent("LEaps", samsung, OrderSide.BUY, 2, 70_000.0))
    portfolio.apply_fill(OrderIntent("LEaps", nvda, OrderSide.BUY, 3, 100.0))

    assert portfolio.cash_by_currency == {"KRW": 860_000.0, "USD": 200.0}
    assert portfolio.quantity(samsung) == 2
    assert portfolio.quantity(nvda) == 3


def test_equal_weight_portfolio_construction_allocates_inside_each_currency_bucket():
    samsung = Symbol("005930", "KRX")
    nvda = Symbol("NVDA", "NAS")
    msft = Symbol("MSFT", "NAS")
    data = _slice(_bar(samsung, 70_000.0), _bar(nvda, 100.0), _bar(msft, 50.0))
    portfolio = Portfolio(cash=0.0, cash_by_currency={"KRW": 1_000_000.0, "USD": 1_000.0})
    engine = PortfolioConstructionEngine(model=EqualWeightPortfolioConstructionModel())

    batch = engine.create_targets(
        PortfolioConstructionContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            active_insights=(_insight(samsung), _insight(nvda), _insight(msft)),
            managed_symbols=(),
        )
    )

    assert {target.symbol.key: target.target_percent for target in batch.targets} == {
        "KRX:005930": pytest.approx(1.0),
        "NAS:NVDA": pytest.approx(0.5),
        "NAS:MSFT": pytest.approx(0.5),
    }
    assert {plan.target.symbol.key: plan.desired_value for plan in batch.plans} == {
        "KRX:005930": pytest.approx(1_000_000.0),
        "NAS:NVDA": pytest.approx(500.0),
        "NAS:MSFT": pytest.approx(500.0),
    }
    assert batch.metadata["portfolio_equity"] == 0.0
    assert batch.metadata["portfolio_equity_by_currency"] == {
        "KRW": pytest.approx(1_000_000.0),
        "USD": pytest.approx(1_000.0),
    }

    sized = OrderSizingEngine().size(
        OrderSizingContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            portfolio_targets=batch,
        )
    )

    assert {plan.allocation.symbol.key: plan.target_quantity for plan in sized.plans} == {
        "KRX:005930": 14,
        "NAS:NVDA": 5,
        "NAS:MSFT": 10,
    }


def test_basic_risk_uses_the_target_currency_cash_bucket():
    samsung = Symbol("005930", "KRX")
    nvda = Symbol("NVDA", "NAS")
    as_of = datetime(2026, 5, 9, 9, 30)
    data = DataSlice(
        time=as_of,
        bars={
            samsung.key: Bar(samsung, as_of, 70_000.0, 70_000.0, 70_000.0, 70_000.0, 1000),
            nvda.key: Bar(nvda, as_of, 100.0, 100.0, 100.0, 100.0, 1000),
        },
    )
    portfolio = Portfolio(cash=0.0, cash_by_currency={"KRW": 1_000_000.0, "USD": 250.0})

    batch = BasicRiskManagementModel().manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(nvda, quantity=3, tag="entry"),),
        )
    )

    assert batch.approved_targets == (PortfolioTarget(nvda, quantity=2, tag="entry"),)
    assert batch.decisions[0].status is RiskDecisionStatus.CLAMPED
    assert batch.decisions[0].metadata["currency"] == "USD"
    assert batch.decisions[0].metadata["available_cash_after"] == pytest.approx(50.0)

from datetime import datetime

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel, FrameworkRunner
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.portfolio_state import PortfolioEngineState, PortfolioSnapshot
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue


class OneShotAlpha:
    alpha_id = "one-shot"
    version = "1.0"

    def __init__(self, insight: Insight | None):
        self.insight = insight

    def generate(self, context):
        return [self.insight] if self.insight is not None else []


def test_portfolio_snapshot_marks_holdings_and_exposure():
    symbol = Symbol("NVDA", "US")
    as_of = datetime(2026, 5, 9, 9, 30)
    portfolio = Portfolio(
        cash=100.0,
        holdings={symbol.key: Holding(symbol=symbol, quantity=2, average_price=80.0)},
    )

    snapshot = PortfolioSnapshot.from_portfolio(
        sleeve_id="LEaps",
        portfolio=portfolio,
        data=_slice(as_of, symbol, close=100.0),
    )

    assert snapshot.sleeve_id == "LEaps"
    assert snapshot.cash == 100.0
    assert snapshot.equity == 300.0
    assert snapshot.gross_exposure == 200.0
    assert snapshot.gross_exposure_pct == 200.0 / 300.0
    assert snapshot.holdings[0].market_value == 200.0
    assert snapshot.holdings[0].cost_basis == 160.0
    assert snapshot.holdings[0].unrealized_pnl == 40.0
    assert snapshot.to_dict()["holdings"][0]["symbol"] == "US:NVDA"


def test_portfolio_engine_state_tracks_target_risk_and_pending_buy_order():
    symbol = Symbol("NVDA", "US")
    as_of = datetime(2026, 5, 9, 9, 30)
    portfolio = Portfolio(cash=1_000.0)
    cycle = _run_framework_cycle(
        sleeve_id="LEaps",
        symbol=symbol,
        as_of=as_of,
        portfolio=portfolio,
        insight_weight=0.5,
    )

    state = PortfolioEngineState.from_cycle(
        cycle=cycle,
        portfolio=portfolio,
        data=_slice(as_of, symbol, close=100.0),
    )

    assert state.current.equity == 1_000.0
    assert state.target_batch.target_count == 1
    assert state.target_batch.plans[0].target_quantity == 5
    assert state.risk_decisions.approved_targets == state.target_batch.targets
    assert state.pending.order_intent_count == 1
    assert state.pending.reserved_cash == 500.0
    assert state.pending.reserved_sell_quantities == {}
    payload = state.to_dict()
    assert payload["target"]["plans"][0]["delta_quantity"] == 5
    assert payload["pending"]["order_intents"][0]["side"] == "buy"


def test_portfolio_engine_state_tracks_pending_sell_quantity():
    symbol = Symbol("NVDA", "US")
    as_of = datetime(2026, 5, 9, 9, 30)
    portfolio = Portfolio(
        cash=100.0,
        holdings={symbol.key: Holding(symbol=symbol, quantity=2, average_price=80.0)},
    )
    cycle = _run_framework_cycle(
        sleeve_id="LEaps",
        symbol=symbol,
        as_of=as_of,
        portfolio=portfolio,
        insight_weight=0.5,
    )

    state = PortfolioEngineState.from_cycle(
        cycle=cycle,
        portfolio=portfolio,
        data=_slice(as_of, symbol, close=100.0),
    )

    assert state.current.equity == 300.0
    assert state.target_batch.plans[0].current_quantity == 2
    assert state.target_batch.plans[0].target_quantity == 1
    assert cycle.order_intents[0].side is OrderSide.SELL
    assert state.pending.reserved_cash == 0.0
    assert state.pending.reserved_sell_quantities == {"US:NVDA": 1}


def _run_framework_cycle(
    *,
    sleeve_id: str,
    symbol: Symbol,
    as_of: datetime,
    portfolio: Portfolio,
    insight_weight: float,
):
    insight = Insight(
        sleeve_id=sleeve_id,
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=as_of,
        expires_at=datetime(2026, 5, 9, 10, 0),
        source_snapshot_id="market-0930",
        alpha_id="one-shot",
        alpha_version="1.0",
        weight=insight_weight,
    )
    runner = FrameworkRunner(
        sleeve_id=sleeve_id,
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(insight),)),
        portfolio_model=EqualWeightPortfolioConstructionModel(),
    )
    return runner.run_once(
        indicator_snapshot=_snapshot(as_of, sleeve_id, symbol),
        data=_slice(as_of, symbol, close=100.0),
        portfolio=portfolio,
    )


def _snapshot(as_of: datetime, sleeve_id: str, symbol: Symbol) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id=f"indicator-{as_of:%H%M}",
        sleeve_id=sleeve_id,
        universe_id="active",
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


def _slice(as_of: datetime, symbol: Symbol, close: float) -> DataSlice:
    return DataSlice(
        time=as_of,
        bars={symbol.key: Bar(symbol, as_of, close, close, close, close, 1000)},
    )

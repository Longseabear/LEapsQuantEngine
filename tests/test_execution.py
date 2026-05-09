from datetime import datetime

from leaps_quant_engine.execution import ExecutionContext, ExecutionEngine, ImmediateExecutionModel
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio


def _slice(symbol: Symbol, close: float = 100.0) -> DataSlice:
    as_of = datetime(2026, 5, 9, 9, 30)
    return DataSlice(
        time=as_of,
        bars={symbol.key: Bar(symbol, as_of, close, close, close, close, 1000)},
    )


def test_execution_engine_creates_order_intent_batch_from_approved_targets():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)

    batch = ExecutionEngine(model=ImmediateExecutionModel()).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
        )
    )

    assert batch.sleeve_id == "test-sleeve"
    assert batch.model_name == "ImmediateExecutionModel"
    assert batch.order_count == 1
    assert batch.order_intents[0].side is OrderSide.BUY
    assert batch.order_intents[0].quantity == 3
    assert batch.metadata["approved_target_count"] == 1


def test_execution_engine_creates_sell_for_reduced_target():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=5, average_price=90.0)})

    batch = ExecutionEngine().execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=portfolio,
            data=data,
            approved_targets=(PortfolioTarget(symbol, 2, "reduce"),),
        )
    )

    assert batch.order_intents[0].side is OrderSide.SELL
    assert batch.order_intents[0].quantity == 3
    assert batch.to_dict()["orders"][0]["notional"] == 300.0

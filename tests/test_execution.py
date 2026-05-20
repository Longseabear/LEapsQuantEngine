from datetime import datetime

from leaps_quant_engine.execution import (
    ExecutionContext,
    ExecutionEngine,
    ImmediateExecutionModel,
    LimitExecutionModel,
    MarketExecutionModel,
    SlicedExecutionModel,
)
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import Bar, DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, Symbol, TimeInForce
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
    assert batch.order_intents[0].order_type is OrderType.LIMIT
    assert batch.order_intents[0].limit_price == 100.0
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


def test_limit_execution_model_applies_side_aware_limit_offset():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=5, average_price=90.0)})

    buy_batch = ExecutionEngine(model=LimitExecutionModel(limit_offset_bps=100)).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
        )
    )
    sell_batch = ExecutionEngine(model=LimitExecutionModel(limit_offset_bps=100)).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=portfolio,
            data=data,
            approved_targets=(PortfolioTarget(symbol, 2, "reduce"),),
        )
    )

    assert buy_batch.order_intents[0].order_type is OrderType.LIMIT
    assert buy_batch.order_intents[0].limit_price == 101.0
    assert sell_batch.order_intents[0].side is OrderSide.SELL
    assert sell_batch.order_intents[0].limit_price == 99.0


def test_market_execution_model_uses_market_order_without_limit_price():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)

    batch = ExecutionEngine(model=MarketExecutionModel(time_in_force=TimeInForce.IOC)).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
        )
    )

    order = batch.order_intents[0]
    assert order.order_type is OrderType.MARKET
    assert order.limit_price is None
    assert order.time_in_force is TimeInForce.IOC


def test_execution_model_buy_window_blocks_new_buys_but_allows_sells():
    symbol = Symbol("AAA", "US")
    outside_window = datetime(2026, 5, 18, 15, 0)
    data = DataSlice(
        time=outside_window,
        bars={symbol.key: Bar(symbol, outside_window, 100, 100, 100, 100, 1000)},
    )
    model = ImmediateExecutionModel(buy_window="09:05-14:50 America/New_York")

    buy_batch = ExecutionEngine(model=model).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=outside_window,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
        )
    )
    sell_batch = ExecutionEngine(model=model).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=outside_window,
            portfolio=Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=3, average_price=90.0)}),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 0, "exit"),),
        )
    )

    assert buy_batch.order_intents == ()
    assert buy_batch.metadata["approved_target_count"] == 1
    assert sell_batch.order_intents[0].side is OrderSide.SELL


def test_sliced_execution_model_splits_large_delta_into_child_orders():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)

    batch = ExecutionEngine(
        model=SlicedExecutionModel(
            order_type="market",
            max_slice_quantity=3,
            max_slice_notional=250,
            max_slices=3,
        )
    ).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 8, "entry"),),
        )
    )

    assert [order.quantity for order in batch.order_intents] == [2, 2, 4]
    assert [order.metadata["slice_index"] for order in batch.order_intents] == [1, 2, 3]
    assert all(order.metadata["slice_count"] == 3 for order in batch.order_intents)
    assert all(order.order_type is OrderType.MARKET for order in batch.order_intents)


def test_execution_model_can_emit_order_lifecycle_policy_metadata():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)

    batch = ExecutionEngine(
        model=LimitExecutionModel(
            urgency="exit",
            max_order_age_seconds=120,
            price_drift_bps=50,
            min_replace_interval_seconds=30,
            max_replacements=2,
        )
    ).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
        )
    )

    assert batch.order_intents[0].metadata["execution_policy"] == {
        "urgency": "exit",
        "max_order_age_seconds": 120.0,
        "price_drift_bps": 50.0,
        "min_replace_interval_seconds": 30.0,
        "max_replacements": 2,
    }


class _ContextAwareExecutionModel:
    def create_orders(self, sleeve_id, portfolio, data, targets, execution_context=None):
        assert execution_context is not None
        target = targets[0]
        session = execution_context.session_for_symbol(target.symbol)
        return [
            OrderIntent(
                sleeve_id,
                target.symbol,
                OrderSide.BUY,
                target.quantity,
                data.get(target.symbol).close,
                metadata={"model_seen_session": session.session_phase if session else ""},
            )
        ]


def test_execution_engine_passes_market_session_context_and_stamps_orders():
    symbol = Symbol("AAA", "US")
    data = _slice(symbol, 100.0)
    session = MarketSession(
        market_scope="overseas",
        session_phase="pre_market",
        is_orderable=True,
        is_regular_market_open=False,
        source="test",
    )

    batch = ExecutionEngine(model=_ContextAwareExecutionModel()).execute(
        ExecutionContext(
            sleeve_id="test-sleeve",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 3, "entry"),),
            market_session=session,
        )
    )

    order = batch.order_intents[0]
    assert order.metadata["model_seen_session"] == "pre_market"
    assert order.metadata["order_session"] == "pre_market"
    assert order.metadata["market_session_scope"] == "overseas"
    assert batch.metadata["market_sessions"]["overseas"]["session_phase"] == "pre_market"


class _MarketSessionAwareExecutionModel:
    def create_orders(self, sleeve_id, portfolio, data, targets, market_session=None):
        target = targets[0]
        return [
            OrderIntent(
                sleeve_id,
                target.symbol,
                OrderSide.SELL,
                target.quantity,
                data.get(target.symbol).close,
                metadata={"model_seen_primary_session": market_session.session_phase if market_session else ""},
            )
        ]


def test_execution_engine_supports_market_session_keyword_for_new_models():
    symbol = Symbol("005930", "KRX")
    data = _slice(symbol, 70_000.0)
    session = MarketSession(
        market_scope="domestic",
        session_phase="after_hours_close",
        is_orderable=True,
        is_regular_market_open=False,
        source="test",
    )

    batch = ExecutionEngine(model=_MarketSessionAwareExecutionModel()).execute(
        ExecutionContext(
            sleeve_id="LEaps",
            generated_at=data.time,
            portfolio=Portfolio(cash=1_000),
            data=data,
            approved_targets=(PortfolioTarget(symbol, 1, "reduce"),),
            market_session=session,
        )
    )

    assert batch.order_intents[0].metadata["model_seen_primary_session"] == "after_hours_close"
    assert batch.order_intents[0].metadata["order_session"] == "after_hours_close"

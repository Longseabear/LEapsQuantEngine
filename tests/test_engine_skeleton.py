from datetime import datetime

from leaps_quant_engine.engine import Engine
from leaps_quant_engine.examples.buy_and_hold import BuyAndHoldAlgorithm
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.sleeve import Sleeve, SleevePolicy


def test_engine_routes_algorithm_target_through_sleeve_to_order_intent():
    symbol = Symbol("005930", "KRX")
    data = DataSlice(
        time=datetime(2026, 5, 7, 9, 0),
        bars={symbol.key: Bar(symbol, datetime(2026, 5, 7, 9, 0), 70000, 71000, 69000, 70000, 1000)},
    )
    sleeve = Sleeve(
        id="swing-kor",
        algorithm=BuyAndHoldAlgorithm(symbol, quantity=10),
        portfolio=Portfolio(cash=1_000_000),
        policy=SleevePolicy(max_position_pct=1.0),
    )

    result = Engine([sleeve]).run([data])

    assert len(result.orders) == 1
    assert result.orders[0].sleeve_id == "swing-kor"
    assert result.orders[0].side is OrderSide.BUY
    assert result.orders[0].quantity == 10


def test_sleeve_policy_caps_target_quantity_by_cash_budget():
    symbol = Symbol("005930", "KRX")
    data = DataSlice(
        time=datetime(2026, 5, 7, 9, 0),
        bars={symbol.key: Bar(symbol, datetime(2026, 5, 7, 9, 0), 100, 100, 100, 100, 1000)},
    )
    sleeve = Sleeve(
        id="micro-kor",
        algorithm=BuyAndHoldAlgorithm(symbol, quantity=100),
        portfolio=Portfolio(cash=1_000),
        policy=SleevePolicy(max_position_pct=0.25),
    )

    result = Engine([sleeve]).run([data])

    assert result.orders[0].quantity == 2

from datetime import datetime

import pytest

from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.orders import (
    OrderCoordinator,
    OrderEventType,
    OrderTicketStatus,
    SimulatedFillModel,
)
from leaps_quant_engine.portfolio import Portfolio


def _batch(sleeve_id: str, intent: OrderIntent, batch_id: str) -> OrderIntentBatch:
    return OrderIntentBatch(
        sleeve_id=sleeve_id,
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(intent,),
        batch_id=batch_id,
    )


def test_order_coordinator_records_same_symbol_opposing_sleeve_collision_without_rejecting_tickets():
    symbol = Symbol("005930", "KRX")
    buy = OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 100.0, "entry")
    sell = OrderIntent("default sleeve", symbol, OrderSide.SELL, 1, 100.0, "exit")

    result = OrderCoordinator().coordinate(
        (
            _batch("LEaps", buy, "batch-buy"),
            _batch("default sleeve", sell, "batch-sell"),
        ),
        generated_at=datetime(2026, 5, 9, 9, 31),
    )

    assert result.has_collisions is True
    assert len(result.tickets) == 2
    assert len(result.events) == 2
    assert result.events[0].event_type is OrderEventType.CREATED
    collision = result.collisions[0]
    assert collision.symbol == symbol
    assert collision.buy_sleeve_ids == ("LEaps",)
    assert collision.sell_sleeve_ids == ("default sleeve",)


def test_order_ticket_applies_only_matching_events_and_syncs_broker_identity():
    symbol = Symbol("005930", "KRX")
    intent = OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 100.0)
    result = OrderCoordinator().coordinate((_batch("LEaps", intent, "batch-1"),))
    ticket = result.tickets[0]

    submitted = ticket.event(
        OrderEventType.SUBMITTED,
        broker_order_id="broker-1",
        occurred_at=datetime(2026, 5, 9, 9, 32),
    )
    submitted_ticket = ticket.apply_event(submitted)

    assert submitted_ticket.status is OrderTicketStatus.SUBMITTED
    assert submitted_ticket.broker_order_id == "broker-1"
    assert submitted_ticket.filled_quantity == 0

    fill = submitted_ticket.event(
        OrderEventType.FILLED,
        quantity=2,
        fill_price=101.0,
        occurred_at=datetime(2026, 5, 9, 9, 33),
    )
    filled_ticket = submitted_ticket.apply_event(fill)

    assert filled_ticket.status is OrderTicketStatus.FILLED
    assert filled_ticket.filled_quantity == 2
    assert filled_ticket.remaining_quantity == 0

    other_ticket = result.tickets[0].__class__(
        ticket_id="other",
        order_intent_id="other-intent",
        batch_id="other-batch",
        sleeve_id="LEaps",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=1,
        reference_price=100.0,
    )
    with pytest.raises(ValueError):
        other_ticket.apply_event(fill)


def test_order_ticket_and_event_round_trip_through_dict_payload():
    symbol = Symbol("005930", "KRX")
    intent = OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 100.0, "entry")
    ticket = OrderCoordinator().coordinate((_batch("LEaps", intent, "batch-1"),)).tickets[0]
    event = ticket.event(
        OrderEventType.SUBMITTED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        broker_order_id="broker-1",
        metadata={"source": "test"},
    )

    restored_ticket = ticket.__class__.from_dict(ticket.to_dict())
    restored_event = event.__class__.from_dict(event.to_dict())

    assert restored_ticket == ticket
    assert restored_event == event
    assert restored_event.metadata["source"] == "test"


def test_portfolio_changes_from_fill_event_not_created_or_submitted_events():
    symbol = Symbol("005930", "KRX")
    intent = OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 100.0)
    ticket = OrderCoordinator().coordinate((_batch("LEaps", intent, "batch-1"),)).tickets[0]
    portfolio = Portfolio(cash=1_000)

    portfolio.apply_order_event(ticket.event(OrderEventType.CREATED))
    portfolio.apply_order_event(ticket.event(OrderEventType.SUBMITTED, broker_order_id="broker-1"))

    assert portfolio.cash == 1_000
    assert portfolio.quantity(symbol) == 0

    fill_event = SimulatedFillModel().fill((ticket,), occurred_at=datetime(2026, 5, 9, 9, 34))[0]
    portfolio.apply_order_event(fill_event)

    assert portfolio.cash == 800
    assert portfolio.quantity(symbol) == 2

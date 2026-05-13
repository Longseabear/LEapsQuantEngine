from datetime import datetime

from leaps_quant_engine.brokerage import BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.orders import OrderEventType, OrderTicketStatus
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def _batch(
    sleeve_id: str,
    *,
    side: OrderSide,
    quantity: int,
    price: float = 100.0,
    batch_id: str = "batch-1",
) -> OrderIntentBatch:
    return OrderIntentBatch(
        sleeve_id=sleeve_id,
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id=sleeve_id,
                symbol=Symbol("005930", "KRX"),
                side=side,
                quantity=quantity,
                reference_price=price,
                tag="test",
            ),
        ),
        batch_id=batch_id,
    )


def test_multi_sleeve_order_orchestrator_submits_polls_and_applies_paper_fills(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000.0, "default sleeve": 500.0},
    )
    symbol = Symbol("005930", "KRX")
    store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-default-holding",
            order_id="seed-default-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=5,
            fill_price=100.0,
            filled_at=datetime(2026, 5, 9, 9, 0),
            sleeve_id="default sleeve",
        )
    )
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        account_store=store,
    )

    result = orchestrator.run_batches(
        (
            _batch("LEaps", side=OrderSide.BUY, quantity=2, batch_id="buy-batch"),
            _batch("default sleeve", side=OrderSide.SELL, quantity=1, batch_id="sell-batch"),
        ),
        generated_at=datetime(2026, 5, 9, 9, 31),
    )

    assert result.has_collisions is True
    assert len(result.coordination.tickets) == 2
    assert [event.event_type for event in result.submission.events] == [
        OrderEventType.SUBMITTED,
        OrderEventType.SUBMITTED,
    ]
    assert [event.event_type for event in result.polling.events] == [
        OrderEventType.FILLED,
        OrderEventType.FILLED,
    ]
    assert len(result.applied_event_ids) == 4
    assert all(ticket.status is OrderTicketStatus.FILLED for ticket in result.final_tickets)

    leaps = store.current_portfolio("LEaps")
    default = store.current_portfolio("default sleeve")
    assert leaps.cash == 800.0
    assert leaps.quantity(symbol) == 2
    assert default.cash == 100.0
    assert default.quantity(symbol) == 4

    for ticket in result.final_tickets:
        ownership = store.ownership_for_order(ticket.order_intent_id)
        assert ownership is not None
        assert ownership.sleeve_id == ticket.sleeve_id
        assert ownership.broker_order_id.startswith("paper:")


def test_order_orchestrator_can_submit_without_polling_or_fill_application(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000.0},
    )
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        account_store=store,
    )

    result = orchestrator.run_batches(
        (_batch("LEaps", side=OrderSide.BUY, quantity=3),),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=False,
    )

    assert len(result.submission.events) == 1
    assert len(result.polling.events) == 0
    assert result.fill_events == ()
    assert result.final_tickets[0].status is OrderTicketStatus.SUBMITTED
    portfolio = store.current_portfolio("LEaps")
    assert portfolio.cash == 1_000.0
    assert portfolio.holdings == {}


class _FailingBrokerGateway:
    def submit(self, ticket, *, occurred_at=None):
        raise RuntimeError("boom")

    def cancel(self, ticket, *, reason="", occurred_at=None):
        raise RuntimeError("boom")

    def poll(self, ticket, *, occurred_at=None):
        return ()


def test_order_orchestrator_records_rejected_event_when_broker_submit_fails(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000.0},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(_FailingBrokerGateway()),
        account_store=store,
        order_state_store=order_store,
    )

    result = orchestrator.run_batches(
        (_batch("LEaps", side=OrderSide.BUY, quantity=3),),
        generated_at=datetime(2026, 5, 9, 9, 31),
    )

    assert [event.event_type for event in result.submission.events] == [OrderEventType.REJECTED]
    assert result.final_tickets[0].status is OrderTicketStatus.REJECTED
    assert "broker_submit_failed" in result.submission.events[0].reason

    snapshot = order_store.snapshot()
    assert snapshot.open_tickets == ()
    assert snapshot.ticket(result.final_tickets[0].ticket_id).status is OrderTicketStatus.REJECTED

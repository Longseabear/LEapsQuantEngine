from datetime import datetime

from leaps_quant_engine.brokerage import BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType, OrderTicketStatus
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def _batch(*, quantity: int = 2, batch_id: str = "batch-1") -> OrderIntentBatch:
    return OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=quantity,
                reference_price=70_000,
                tag="state-test",
            ),
        ),
        batch_id=batch_id,
    )


def test_file_order_runtime_state_store_reconstructs_open_ticket_after_restart(tmp_path):
    store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    coordination = OrderCoordinator().coordinate((_batch(),), generated_at=datetime(2026, 5, 9, 9, 31))
    ticket = coordination.tickets[0]
    submitted = ticket.event(
        OrderEventType.SUBMITTED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        broker_order_id="broker-1",
    )

    store.record_tickets(coordination.tickets, recorded_at=datetime(2026, 5, 9, 9, 31))
    store.record_events(coordination.events, recorded_at=datetime(2026, 5, 9, 9, 31))
    store.record_event(submitted, recorded_at=datetime(2026, 5, 9, 9, 32))

    reloaded = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    snapshot = reloaded.snapshot(captured_at=datetime(2026, 5, 9, 9, 33))

    assert snapshot.record_count == 3
    assert len(snapshot.tickets) == 1
    assert len(snapshot.events) == 2
    assert len(snapshot.open_tickets) == 1
    assert snapshot.open_tickets[0].status is OrderTicketStatus.SUBMITTED
    assert snapshot.open_tickets[0].broker_order_id == "broker-1"
    assert snapshot.open_tickets_for_sleeve("LEaps") == snapshot.open_tickets


def test_file_order_runtime_state_store_deduplicates_event_ids_when_replaying(tmp_path):
    store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    coordination = OrderCoordinator().coordinate((_batch(),), generated_at=datetime(2026, 5, 9, 9, 31))
    ticket = coordination.tickets[0]
    fill = ticket.event(
        OrderEventType.FILLED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        quantity=2,
        fill_price=70_100,
        broker_order_id="broker-1",
    )

    store.record_tickets(coordination.tickets)
    store.record_event(fill)
    store.record_event(fill)

    snapshot = store.snapshot()

    assert len(snapshot.events) == 1
    assert snapshot.tickets[0].status is OrderTicketStatus.FILLED
    assert snapshot.tickets[0].filled_quantity == 2
    assert snapshot.open_tickets == ()
    assert len(snapshot.terminal_tickets) == 1


def test_order_orchestrator_records_ticket_and_events_to_runtime_state_store(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        account_store=account_store,
        order_state_store=order_store,
    )

    result = orchestrator.run_batches(
        (_batch(quantity=3),),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=False,
    )
    snapshot = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl").snapshot()

    assert result.final_tickets[0].status is OrderTicketStatus.SUBMITTED
    assert snapshot.record_count == 3
    assert len(snapshot.events) == 2
    assert [event.event_type for event in snapshot.events] == [
        OrderEventType.CREATED,
        OrderEventType.SUBMITTED,
    ]
    assert snapshot.open_tickets[0].status is OrderTicketStatus.SUBMITTED
    assert snapshot.open_tickets[0].broker_order_id.startswith("paper:")
    assert account_store.current_portfolio("LEaps").holdings == {}


def test_order_orchestrator_reports_fill_driven_portfolio_mutations(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=True)),
        account_store=account_store,
        order_state_store=order_store,
    )

    result = orchestrator.run_batches(
        (_batch(quantity=3),),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=True,
    )

    assert result.final_tickets[0].status is OrderTicketStatus.FILLED
    assert result.applied_event_ids == tuple(event.event_id for event in result.submission.events + result.polling.events)
    assert len(result.fill_application_reports) == 2
    assert len(result.portfolio_mutations) == 1
    mutation = result.portfolio_mutations[0]
    assert mutation.sleeve_id == "LEaps"
    assert mutation.before_cash == 1_000_000
    assert mutation.after_cash == 790_000
    assert mutation.before_quantity == 0
    assert mutation.after_quantity == 3
    assert mutation.order_intent_id == result.final_tickets[0].order_intent_id
    assert mutation.ticket_id == result.final_tickets[0].ticket_id
    assert account_store.current_portfolio("LEaps").quantity(Symbol("005930", "KRX")) == 3

    status = result.to_dict()
    assert status["portfolio_mutation_count"] == 1
    assert status["portfolio_mutations"][0]["after_quantity"] == 3

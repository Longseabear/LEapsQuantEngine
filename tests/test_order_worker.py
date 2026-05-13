from datetime import datetime

from leaps_quant_engine.brokerage import BrokerEngineExecutionGateway, BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_worker import ExecutionHistoryReconcileWorker, OpenTicketPollWorker
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType, OrderTicketStatus
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def _batch(
    sleeve_id: str = "LEaps",
    *,
    quantity: int = 2,
    batch_id: str = "batch-1",
) -> OrderIntentBatch:
    return OrderIntentBatch(
        sleeve_id=sleeve_id,
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id=sleeve_id,
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=quantity,
                reference_price=70_000,
                tag="worker-test",
            ),
        ),
        batch_id=batch_id,
    )


def test_open_ticket_poll_worker_restores_open_ticket_and_applies_paper_fill(tmp_path):
    account_path = tmp_path / "accounts.json"
    order_path = tmp_path / "orders.jsonl"
    account_store = VirtualSleeveAccountStore(
        account_path,
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(order_path)
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        account_store=account_store,
        order_state_store=order_store,
    )
    orchestrator.run_batches(
        (_batch(quantity=2),),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=False,
    )

    reloaded_account_store = VirtualSleeveAccountStore(account_path)
    reloaded_order_store = FileOrderRuntimeStateStore(order_path)
    worker = OpenTicketPollWorker(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        order_state_store=reloaded_order_store,
        account_store=reloaded_account_store,
    )

    report = worker.poll_once(polled_at=datetime(2026, 5, 9, 9, 32))

    assert report.open_ticket_count_before == 1
    assert report.polled_ticket_count == 1
    assert report.fill_event_count == 1
    assert report.open_ticket_count_after == 0
    assert report.after.terminal_tickets[0].status is OrderTicketStatus.FILLED
    portfolio = reloaded_account_store.current_portfolio("LEaps")
    assert portfolio.cash == 860_000
    assert portfolio.quantity(Symbol("005930", "KRX")) == 2


def test_open_ticket_poll_worker_noops_when_no_open_tickets(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    worker = OpenTicketPollWorker(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        order_state_store=order_store,
        account_store=account_store,
    )

    report = worker.poll_once(polled_at=datetime(2026, 5, 9, 9, 32))

    assert report.open_ticket_count_before == 0
    assert report.polled_ticket_count == 0
    assert report.event_count == 0
    assert report.applied_event_ids == ()


def test_open_ticket_poll_worker_can_filter_by_sleeve(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000, "ETF": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        account_store=account_store,
        order_state_store=order_store,
    )
    orchestrator.run_batches(
        (
            _batch("LEaps", quantity=1, batch_id="batch-leaps"),
            _batch("ETF", quantity=1, batch_id="batch-etf"),
        ),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=False,
    )
    worker = OpenTicketPollWorker(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        order_state_store=order_store,
        account_store=account_store,
    )

    report = worker.poll_once(
        polled_at=datetime(2026, 5, 9, 9, 32),
        sleeve_id="LEaps",
    )

    assert report.open_ticket_count_before == 2
    assert report.polled_ticket_count == 1
    assert report.touched_sleeve_ids == ("LEaps",)
    after = order_store.snapshot()
    assert len(after.open_tickets) == 1
    assert after.open_tickets[0].sleeve_id == "ETF"
    assert account_store.current_portfolio("LEaps").quantity(Symbol("005930", "KRX")) == 1
    assert account_store.current_portfolio("ETF").quantity(Symbol("005930", "KRX")) == 0


class FakeExecutionHistoryClient:
    def __init__(self, *, executions, holdings=None, holdings_error=None):
        self.executions = executions
        self.holdings = holdings if holdings is not None else {"holdings": []}
        self.holdings_error = holdings_error
        self.calls = []

    def get_execution_history(self, *, start_date, end_date, market="domestic", side="all", symbol=""):
        self.calls.append(("history", start_date, end_date, market, side, symbol))
        return {"executions": list(self.executions)}

    def get_holdings(self, *, market="domestic"):
        self.calls.append(("holdings", market))
        if self.holdings_error is not None:
            raise self.holdings_error
        return self.holdings


class FakeBrokerEngineQueueClient:
    def __init__(self):
        self.enqueued = []

    def call_operation(self, operation, arguments=None):
        raise AssertionError("queue mode should not call operation directly")

    def enqueue_command(self, operation, *, arguments=None, metadata=None):
        self.enqueued.append(
            {
                "operation": operation,
                "arguments": dict(arguments or {}),
                "metadata": dict(metadata or {}),
            }
        )
        return {"command_id": "cmd-00000001", "sequence": 1, "status": "queued"}

    def get_snapshots(self, *, consumer_id, snapshot_type="", resource_id="", limit=200):
        return {
            "consumer_id": consumer_id,
            "snapshot_count": 1,
            "snapshots": [
                {
                    "snapshot_id": f"{snapshot_type}:{resource_id}",
                    "snapshot_type": snapshot_type,
                    "resource_id": resource_id,
                    "payload": {
                        "status": "completed",
                        "result": {
                            "branch_no": "001",
                            "order_no": "00012345",
                        },
                        "error": "",
                    },
                }
            ],
        }


def test_broker_engine_accepted_order_alias_later_owns_execution_history_fill(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    broker_client = FakeBrokerEngineQueueClient()
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(
            BrokerEngineExecutionGateway(client=broker_client, consumer_id="test-consumer")
        ),
        account_store=account_store,
        order_state_store=order_store,
    )

    submitted = orchestrator.run_batches(
        (_batch(quantity=2),),
        generated_at=datetime(2026, 5, 9, 9, 31),
        poll_after_submit=True,
    )
    ticket = submitted.final_tickets[0]

    assert submitted.submission.events[0].broker_order_id == "cmd-00000001"
    assert submitted.polling.events[0].broker_order_id == "001:00012345"
    assert account_store.current_portfolio("LEaps").holdings == {}
    ownership = account_store.ownership_for_order(ticket.order_intent_id)
    assert ownership is not None
    assert ownership.sleeve_id == "LEaps"
    assert ownership.broker_order_id == "001:00012345"
    assert account_store.ownership_for_order("00012345").sleeve_id == "LEaps"

    worker = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(
            executions=[
                {
                    "order_id": "00012345",
                    "symbol": "005930",
                    "side": "buy",
                    "execution_quantity": "2",
                    "execution_price": "70000",
                    "execution_timestamp": "20260509T093500",
                    "source_granularity": "order_execution_summary",
                }
            ],
            holdings={
                "holdings": [
                    {
                        "symbol": "005930",
                        "holding_quantity": 2,
                        "average_purchase_price": 70_000,
                    }
                ]
            },
        ),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = worker.reconcile_once(
        start_date="20260509",
        end_date="20260509",
        reconciled_at=datetime(2026, 5, 9, 9, 40),
    )

    assert report.status == "ok"
    assert report.imported_fill_count == 1
    assert report.touched_sleeve_ids == ("LEaps",)
    assert report.reconciliation["status"] == "matched"
    portfolio = account_store.current_portfolio("LEaps")
    assert portfolio.cash == 860_000
    assert portfolio.quantity(Symbol("005930", "KRX")) == 2


def test_execution_history_reconcile_worker_imports_owned_fill_by_broker_alias(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    account_store.register_order_intent(
        OrderIntent(
            sleeve_id="LEaps",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=2,
            reference_price=70_000,
        ),
        order_id="intent-1",
        broker_order_id="001:00012345",
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    coordination = OrderCoordinator().coordinate(
        (_batch(quantity=2),),
        generated_at=datetime(2026, 5, 8, 9, 29),
    )
    accepted = coordination.tickets[0].event(
        OrderEventType.ACCEPTED,
        occurred_at=datetime(2026, 5, 8, 9, 30),
        broker_order_id="001:00012345",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events((*coordination.events, accepted))
    worker = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(
            executions=[
                {
                    "order_id": "00012345",
                    "symbol": "005930",
                    "side": "buy",
                    "execution_quantity": "2",
                    "execution_price": "70000",
                    "execution_timestamp": "20260508T093000",
                }
            ],
            holdings={
                "holdings": [
                    {
                        "symbol": "005930",
                        "holding_quantity": 2,
                        "average_purchase_price": 70_000,
                    }
                ]
            },
        ),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = worker.reconcile_once(
        start_date="20260508",
        end_date="20260508",
        reconciled_at=datetime(2026, 5, 9, 9, 40),
    )

    assert report.status == "ok"
    assert report.imported_fill_count == 1
    assert report.duplicate_fill_count == 0
    assert report.touched_sleeve_ids == ("LEaps",)
    assert account_store.current_portfolio("LEaps").quantity(Symbol("005930", "KRX")) == 2
    assert report.reconciliation["status"] == "matched"
    assert order_store.snapshot().open_tickets == ()
    assert order_store.snapshot().fill_events[-1].reason == "execution_history_reconcile_fill"


def test_execution_history_reconcile_worker_closes_ticket_when_fill_was_already_imported(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    account_store.register_order_intent(
        OrderIntent(
            sleeve_id="LEaps",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=2,
            reference_price=70_000,
        ),
        order_id="intent-1",
        broker_order_id="001:00012345",
    )
    fill_row = {
        "order_id": "00012345",
        "symbol": "005930",
        "side": "buy",
        "execution_quantity": "2",
        "execution_price": "70000",
        "execution_timestamp": "20260508T093000",
    }
    worker_without_order_store = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(executions=[fill_row]),
        account_store=account_store,
    )
    worker_without_order_store.reconcile_once(
        start_date="20260508",
        end_date="20260508",
        reconcile_holdings=False,
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    coordination = OrderCoordinator().coordinate(
        (_batch(quantity=2),),
        generated_at=datetime(2026, 5, 8, 9, 29),
    )
    accepted = coordination.tickets[0].event(
        OrderEventType.ACCEPTED,
        occurred_at=datetime(2026, 5, 8, 9, 30),
        broker_order_id="001:00012345",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events((*coordination.events, accepted))
    worker = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(executions=[fill_row]),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = worker.reconcile_once(
        start_date="20260508",
        end_date="20260508",
        reconcile_holdings=False,
    )

    assert report.duplicate_fill_count == 1
    assert report.imported_fill_count == 0
    assert order_store.snapshot().open_tickets == ()
    assert order_store.snapshot().fill_events[-1].reason == "execution_history_reconcile_fill"


def test_execution_history_reconcile_worker_records_unknown_and_continues_past_bad_rows(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    worker = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(
            executions=[
                {
                    "order_id": "bad-row",
                    "symbol": "005930",
                    "side": "buy",
                    "execution_quantity": "1",
                    "execution_timestamp": "20260508T093000",
                },
                {
                    "order_id": "manual-order",
                    "symbol": "000660",
                    "side": "buy",
                    "execution_quantity": "1",
                    "execution_price": "120000",
                    "execution_timestamp": "20260508T100000",
                },
            ],
            holdings_error=RuntimeError("holdings temporarily unavailable"),
        ),
        account_store=account_store,
    )

    report = worker.reconcile_once(start_date="20260508", end_date="20260508")

    assert report.status == "warnings"
    assert report.execution_count == 2
    assert report.skipped_fill_count == 1
    assert report.unallocated_fill_count == 1
    assert report.imported_fill_count == 0
    assert report.errors == ("holdings_reconciliation_failed: holdings temporarily unavailable",)
    assert report.rejected_executions[0]["execution"]["order_id"] == "bad-row"
    assert account_store.broker_fill("kis:domestic:manual-order:20260508T100000:1:120000") is not None
    assert account_store.current_portfolio("LEaps").holdings == {}


def test_execution_history_reconcile_worker_skips_fill_already_applied_from_order_event(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    coordination = OrderCoordinator().coordinate(
        (_batch(quantity=2),),
        generated_at=datetime(2026, 5, 9, 9, 31),
    )
    ticket = coordination.tickets[0]
    account_store.register_order_ticket(ticket)
    order_store.record_tickets((ticket,))
    fill_event = ticket.event(
        OrderEventType.FILLED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        quantity=2,
        fill_price=70_000,
        broker_order_id="001:00012345",
    )
    order_store.record_event(fill_event)
    account_store.apply_order_event(fill_event)
    worker = ExecutionHistoryReconcileWorker(
        account_client=FakeExecutionHistoryClient(
            executions=[
                {
                    "order_id": "00012345",
                    "symbol": "005930",
                    "side": "buy",
                    "execution_quantity": "2",
                    "execution_price": "70000",
                    "execution_timestamp": "20260508T093000",
                }
            ],
        ),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = worker.reconcile_once(
        start_date="20260508",
        end_date="20260508",
        reconcile_holdings=False,
    )

    assert report.existing_order_event_fill_count == 1
    assert report.imported_fill_count == 0
    portfolio = account_store.current_portfolio("LEaps")
    assert portfolio.cash == 860_000
    assert portfolio.quantity(Symbol("005930", "KRX")) == 2

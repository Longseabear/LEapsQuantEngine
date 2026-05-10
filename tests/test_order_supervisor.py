from datetime import datetime

from leaps_quant_engine.brokerage import BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_supervisor import OrderMaintenancePolicy, OrderRuntimeSupervisor
from leaps_quant_engine.order_worker import OpenTicketPollWorker
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


class FailingPollWorker:
    def poll_once(self, **kwargs):
        raise RuntimeError("boom")


def test_order_runtime_supervisor_reports_poll_errors_without_stopping(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")

    report = OrderRuntimeSupervisor(
        runtime_id="test-runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
        poll_worker=FailingPollWorker(),
    ).run_once(
        poll=True,
        reconcile=False,
        run_at=datetime(2026, 5, 10, 9, 0),
    )

    assert report.status == "warnings"
    assert report.errors == ("poll_failed:LEaps: boom",)
    assert report.final_status.sleeves[0].portfolio.cash == 1000


def test_order_runtime_supervisor_cancels_stale_open_tickets(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 10, 9, 0),
        order_intents=(OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 1, 100),),
        batch_id="batch-1",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 10, 9, 0)).tickets[0]
    order_store.record_tickets((ticket,), recorded_at=datetime(2026, 5, 10, 9, 0))
    account_store.register_order_ticket(ticket)

    poll_worker = OpenTicketPollWorker(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway()),
        order_state_store=order_store,
        account_store=account_store,
    )
    report = OrderRuntimeSupervisor(
        runtime_id="test-runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
        poll_worker=poll_worker,
        maintenance_policy=OrderMaintenancePolicy(stale_after_seconds=60, cancel_stale=True),
    ).run_once(
        poll=False,
        reconcile=False,
        run_at=datetime(2026, 5, 10, 9, 2),
    )

    assert report.maintenance_report is not None
    assert report.maintenance_report.stale_ticket_count == 1
    assert report.maintenance_report.cancel_events[0].event_type is OrderEventType.CANCELLED
    assert report.final_status.order_snapshot.open_tickets == ()

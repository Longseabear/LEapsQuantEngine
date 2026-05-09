from datetime import datetime

from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_smoke import OrderRuntimePaperSmokeRunner
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def test_order_runtime_paper_smoke_submits_then_supervises_to_fill(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 10, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=2,
                reference_price=100,
                tag="paper-smoke",
            ),
        ),
        batch_id="batch-1",
    )

    report = OrderRuntimePaperSmokeRunner(
        runtime_id="test-runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
    ).run_batches((batch,), started_at=datetime(2026, 5, 10, 9, 31))

    assert report.status == "ok"
    assert report.submit.status == "submitted"
    assert report.submit.final_status.order_snapshot.open_tickets[0].status.value == "submitted"
    assert report.supervisor is not None
    assert report.supervisor.poll_fill_event_count == 1
    assert len(report.final_status.order_snapshot.open_tickets) == 0
    assert account_store.current_portfolio("LEaps").cash == 800
    assert account_store.current_portfolio("LEaps").quantity(Symbol("005930", "KRX")) == 2

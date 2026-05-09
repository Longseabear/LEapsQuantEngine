from datetime import datetime

from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_supervisor import OrderRuntimeSupervisor
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

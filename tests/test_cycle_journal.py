from datetime import datetime, timedelta

from leaps_quant_engine.cycle_journal import CycleJournalEntry, FileCycleJournalStore
from leaps_quant_engine.runtime_health import build_runtime_health_report
from leaps_quant_engine.runtime_recovery import build_recovery_account_report, build_recovery_report
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def test_cycle_journal_jsonl_filters_latest_by_sleeve_and_route(tmp_path):
    path = tmp_path / "cycle-journal.jsonl"
    store = FileCycleJournalStore(path)
    first = CycleJournalEntry(
        runtime_id="runtime",
        config_version="sha256:1",
        sleeve_id="LEaps",
        account_id="kis-domestic",
        route_id="kis-domestic",
        market_scope="domestic",
        generated_at=datetime(2026, 5, 10, 9, 0),
        recorded_at=datetime(2026, 5, 10, 9, 0),
        source="runtime-run-once",
        status="ok",
        counts={"order_intent_count": 0},
    )
    second = CycleJournalEntry(
        runtime_id="runtime",
        config_version="sha256:1",
        sleeve_id="LEaps",
        account_id="kis-overseas",
        route_id="kis-overseas",
        market_scope="overseas",
        generated_at=datetime(2026, 5, 10, 9, 1),
        recorded_at=datetime(2026, 5, 10, 9, 1),
        source="runtime-run-once",
        status="warnings",
        snapshot_status="stale",
        counts={"order_intent_count": 1},
        warnings=("stale_snapshot",),
    )

    store.append(first)
    store.append(second)

    assert store.latest(sleeve_id="LEaps").entry_id == second.entry_id
    assert store.latest(sleeve_id="LEaps", account_id="kis-domestic").entry_id == first.entry_id
    assert store.entries(sleeve_id="LEaps", market_scope="overseas") == (second,)
    assert FileCycleJournalStore(path).latest(sleeve_id="LEaps").warnings == ("stale_snapshot",)


def test_recovery_and_health_reports_combine_journal_order_store_and_virtual_account(tmp_path):
    journal_store = FileCycleJournalStore(tmp_path / "journal.jsonl")
    journal_store.append(
        CycleJournalEntry(
            runtime_id="runtime",
            config_version="sha256:1",
            sleeve_id="LEaps",
            account_id="kis-domestic",
            route_id="kis-domestic",
            market_scope="domestic",
            generated_at=datetime(2026, 5, 10, 8, 0),
            recorded_at=datetime(2026, 5, 10, 8, 0),
            source="runtime-run-once",
            status="warnings",
            snapshot_status="stale",
        )
    )
    account_store_path = tmp_path / "accounts.json"
    order_store_path = tmp_path / "orders.jsonl"
    account_store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 1_000})
    account_store.record_broker_fill(
        VirtualFillEvent(
            fill_id="raw-fill",
            order_id="broker-order",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=1,
            fill_price=100,
            filled_at=datetime(2026, 5, 10, 8, 30),
        )
    )
    order_store = FileOrderRuntimeStateStore(order_store_path)
    coordination = OrderCoordinator().coordinate(
        (
            OrderIntentBatch(
                sleeve_id="LEaps",
                generated_at=datetime(2026, 5, 10, 8, 31),
                order_intents=(
                    OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 1, 100),
                ),
                batch_id="batch-1",
            ),
        ),
        generated_at=datetime(2026, 5, 10, 8, 31),
    )
    submitted = coordination.tickets[0].event(
        OrderEventType.SUBMITTED,
        occurred_at=datetime(2026, 5, 10, 8, 32),
        broker_order_id="broker-1",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events(coordination.events)
    order_store.record_event(submitted)
    status = build_order_runtime_status(
        runtime_id="runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
        order_store_path=order_store_path,
        account_store_path=account_store_path,
        broker_account_id="kis-domestic",
        market_scope="domestic",
        generated_at=datetime(2026, 5, 10, 8, 33),
    )

    account_recovery = build_recovery_account_report(
        order_status=status,
        journal_store=journal_store,
        sleeve_ids=("LEaps",),
    )
    recovery = build_recovery_report(
        runtime_id="runtime",
        config_version="sha256:1",
        sleeve_ids=("LEaps",),
        accounts=(account_recovery,),
        generated_at=datetime(2026, 5, 10, 8, 34),
    )
    health = build_runtime_health_report(
        runtime_id="runtime",
        sleeve_ids=("LEaps",),
        journal_store=journal_store,
        order_status=status,
        max_cycle_age_seconds=60,
        generated_at=datetime(2026, 5, 10, 8, 34),
    )

    assert recovery.status == "needs_attention"
    assert "poll_open_tickets" in recovery.recommended_next_actions
    assert "allocate_unassigned_fills" in recovery.recommended_next_actions
    assert health.status == "needs_attention"
    assert "run_order_runtime_supervise" in health.recommended_next_actions
    assert "refresh_snapshot_worker" in health.recommended_next_actions

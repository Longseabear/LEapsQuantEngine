from datetime import datetime, timedelta

from leaps_quant_engine.cycle_journal import CycleJournalEntry, FileCycleJournalStore
from leaps_quant_engine.framework.portfolio_blend import ACTIVE_TRANSITION_NAMESPACE, DEFAULT_PORTFOLIO_BLEND_MODEL_ID
from leaps_quant_engine.runtime_health import build_runtime_health_report
from leaps_quant_engine.runtime_recovery import build_recovery_account_report, build_recovery_report
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, ModelStateKey, StatePatch, StatePatchOperation
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


def test_cycle_journal_limited_entries_return_latest_matches_in_chronological_order(tmp_path):
    path = tmp_path / "cycle-journal.jsonl"
    store = FileCycleJournalStore(path)
    entries = [
        CycleJournalEntry(
            runtime_id="runtime",
            config_version="sha256:1",
            sleeve_id="LEaps" if index % 2 == 0 else "other",
            generated_at=datetime(2026, 5, 10, 9, index),
            recorded_at=datetime(2026, 5, 10, 9, index),
            source="runtime-run-once",
            status="ok",
            counts={"index": index},
        )
        for index in range(6)
    ]
    store.append_many(entries)

    latest_two = store.entries(sleeve_id="LEaps", limit=2)

    assert latest_two == (entries[2], entries[4])
    assert store.latest(sleeve_id="LEaps") == entries[4]


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


def test_runtime_health_reports_overdue_portfolio_blend_transition():
    store = InMemoryRuntimeStateStore()
    transition_key = ModelStateKey(
        sleeve_id="LEaps",
        model_id=DEFAULT_PORTFOLIO_BLEND_MODEL_ID,
        namespace=ACTIVE_TRANSITION_NAMESPACE,
    )
    store.apply_patches(
        (
            StatePatch(
                key=transition_key,
                operation=StatePatchOperation.SET,
                value={
                    "transition_id": "blend-1",
                    "sleeve_id": "LEaps",
                    "from_weights": {"KRX:005930": 0.4},
                    "to_weights": {"KRX:005930": 0.1},
                    "started_at": "2026-05-15T09:00:00",
                    "deadline_at": "2026-05-15T10:00:00",
                    "duration_minutes": 60,
                    "elapsed_minutes": 45,
                    "from_elapsed_minutes": 20,
                },
                reason="test",
                generated_at=datetime(2026, 5, 15, 9, 45),
            ),
        ),
        applied_at=datetime(2026, 5, 15, 9, 45),
    )

    health = build_runtime_health_report(
        runtime_id="runtime",
        sleeve_ids=("LEaps",),
        journal_store=None,
        runtime_state_store=store,
        generated_at=datetime(2026, 5, 15, 10, 10),
        portfolio_blend_overdue_grace_seconds=60,
    )

    checks = {check.name: check for check in health.checks}
    assert health.status == "needs_attention"
    assert checks["portfolio_blend_deadline_overdue"].metadata["transition_id"] == "blend-1"
    assert checks["portfolio_blend_deadline_overdue"].metadata["overdue_seconds"] == 600.0
    assert "run_runtime_once_or_check_worker" in health.recommended_next_actions


def test_runtime_health_reports_kis_gateway_liveness(monkeypatch):
    monkeypatch.setattr(
        "leaps_quant_engine.runtime_health.fetch_kis_gateway_health",
        lambda base_url, timeout_seconds: {"status": "ok", "server": "leaps-kis-gateway", "lane": {"mock": False}},
    )

    health = build_runtime_health_report(
        runtime_id="runtime",
        sleeve_ids=("LEaps",),
        journal_store=None,
        kis_gateway_base_url="http://127.0.0.1:8766",
    )

    checks = {check.name: check for check in health.checks}
    assert checks["kis_gateway_liveness"].status == "ok"
    assert checks["kis_gateway_liveness"].metadata["base_url"] == "http://127.0.0.1:8766"

from datetime import datetime

from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def _batch() -> OrderIntentBatch:
    return OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=2,
                reference_price=70_000,
                tag="status-test",
            ),
        ),
        batch_id="batch-1",
    )


def test_order_runtime_status_combines_tickets_portfolio_and_unallocated_fills(tmp_path):
    account_store_path = tmp_path / "accounts.json"
    order_store_path = tmp_path / "orders.jsonl"
    account_store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 1_000_000})
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-fill",
            order_id="seed-order",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=1,
            fill_price=70_000,
            filled_at=datetime(2026, 5, 9, 9, 0),
            sleeve_id="LEaps",
        )
    )
    account_store.record_broker_fill(
        VirtualFillEvent(
            fill_id="raw-broker-fill",
            order_id="broker-order",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=4,
            fill_price=70_100,
            filled_at=datetime(2026, 5, 9, 9, 1),
        )
    )

    order_store = FileOrderRuntimeStateStore(order_store_path)
    coordination = OrderCoordinator().coordinate((_batch(),), generated_at=datetime(2026, 5, 9, 9, 31))
    submitted = coordination.tickets[0].event(
        OrderEventType.SUBMITTED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        broker_order_id="broker-1",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events(coordination.events)
    order_store.record_event(submitted)

    report = build_order_runtime_status(
        runtime_id="test-runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
        order_store_path=order_store_path,
        account_store_path=account_store_path,
        generated_at=datetime(2026, 5, 9, 9, 33),
    )
    payload = report.to_dict(include_details=False)

    assert payload["needs_attention"] is True
    assert payload["order_runtime"]["open_ticket_count"] == 1
    assert payload["order_runtime"]["ticket_status_counts"] == {"submitted": 1}
    assert payload["virtual_account"]["unallocated_fill_count"] == 1
    assert payload["sleeves"][0]["portfolio"]["cash"] == 930_000
    assert payload["sleeves"][0]["portfolio"]["holdings"][0]["quantity"] == 1
    assert payload["sleeves"][0]["pending_buy_notional"] == 140_000


def test_order_runtime_status_does_not_need_attention_for_ignored_broker_fill(tmp_path):
    account_store_path = tmp_path / "accounts.json"
    order_store_path = tmp_path / "orders.jsonl"
    account_store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 1_000_000})
    fill = VirtualFillEvent(
        fill_id="manual-fill",
        order_id="manual-order",
        symbol=Symbol("005930", "KRX"),
        side=OrderSide.SELL,
        quantity=2,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 14, 15, 10),
    )
    account_store.record_broker_fill(fill)
    account_store.ignore_broker_fill(fill.fill_id, reason="operator owned manual exit")
    order_store_path.write_text("", encoding="utf-8")
    order_store = FileOrderRuntimeStateStore(order_store_path)

    report = build_order_runtime_status(
        runtime_id="test-runtime",
        sleeve_ids=("LEaps",),
        order_state_store=order_store,
        account_store=account_store,
        order_store_path=order_store_path,
        account_store_path=account_store_path,
        generated_at=datetime(2026, 5, 14, 15, 11),
    )
    payload = report.to_dict(include_details=True)

    assert payload["needs_attention"] is False
    assert payload["virtual_account"]["unallocated_fill_count"] == 0
    assert payload["virtual_account"]["ignored_fill_count"] == 1
    assert payload["virtual_account"]["allocation_status_counts"] == {"ignored": 1}

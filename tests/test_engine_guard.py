from datetime import datetime

from leaps_quant_engine.engine_guard import EngineGuard
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def _batch(*orders):
    return OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 10, 9, 0),
        order_intents=tuple(orders),
        batch_id="batch-1",
    )


def test_engine_guard_blocks_reserved_cash_oversell_and_route_mismatch(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 100})
    store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-fill",
            order_id="seed-order",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=1,
            fill_price=10,
            filled_at=datetime(2026, 5, 10, 8, 59),
            sleeve_id="LEaps",
        )
    )

    report = EngineGuard().evaluate(
        batches=(
            _batch(
                OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.SELL, 2, 10),
                OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 2, 100),
            ),
        ),
        account_store=store,
        account_id="kis-overseas",
        market_scope="overseas",
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert report.blocked is True
    assert "account_route_mismatch" in report.errors
    assert "reserved_sell_quantity_exceeded" in report.errors
    assert "reserved_cash_exceeded" in report.errors


def test_engine_guard_uses_route_currency_cash(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
        default_currency="KRW",
    )
    store.sync_account_cash(
        {"cash_balance": 500, "currency": "USD"},
        account_id="kis-overseas",
        currency="USD",
        residual_sleeve_id="default sleeve",
    )
    store.transfer_cash(
        from_sleeve_id="default sleeve",
        to_sleeve_id="LEaps",
        amount=300,
        account_id="kis-overseas",
        currency="USD",
    )

    report = EngineGuard().evaluate(
        batches=(
            _batch(
                OrderIntent("LEaps", Symbol("NVDA", "NAS"), OrderSide.BUY, 2, 200),
            ),
        ),
        account_store=store,
        account_id="kis-overseas",
        market_scope="overseas",
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert report.blocked is True
    assert report.errors == ("reserved_cash_exceeded",)
    assert report.decisions[0].metadata["currency"] == "USD"
    assert report.decisions[0].metadata["cash"] == 300


def test_engine_guard_blocks_duplicate_committed_order_intents(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    batch = _batch(OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 1, 70_000))
    coordination = OrderCoordinator().coordinate(
        (batch,),
        generated_at=datetime(2026, 5, 10, 9, 0),
    )
    order_state_store.record_tickets(coordination.tickets)

    report = EngineGuard().evaluate(
        batches=(batch,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=True,
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert report.blocked is True
    assert "duplicate_order_intent_already_recorded" in report.errors
    decision = next(decision for decision in report.decisions if decision.reason == "duplicate_order_intent_already_recorded")
    assert decision.metadata["order_intent_id"] == "batch-1:1"
    assert decision.metadata["ticket_id"] == "ticket:batch-1:1"


def test_engine_guard_warns_duplicate_order_intents_on_dry_run(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    batch = _batch(OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 1, 70_000))
    coordination = OrderCoordinator().coordinate(
        (batch,),
        generated_at=datetime(2026, 5, 10, 9, 0),
    )
    order_state_store.record_tickets(coordination.tickets)

    report = EngineGuard().evaluate(
        batches=(batch,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=False,
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert report.blocked is False
    assert report.warnings == ("duplicate_order_intent_already_recorded",)


def test_engine_guard_blocks_order_when_open_ticket_already_covers_target_quantity(tmp_path):
    symbol = Symbol("005380", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 10_000_000},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-hyundai",
            order_id="seed-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=4,
            fill_price=670_000,
            filled_at=datetime(2026, 5, 12, 8, 50),
            sleeve_id="LEaps",
        )
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_sell = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.SELL,
            1,
            678_000,
            metadata={"current_quantity": 4, "target_quantity": 3, "delta_quantity": -1},
        )
    )
    coordination = OrderCoordinator().coordinate((existing_sell,), generated_at=datetime(2026, 5, 12, 10, 7))
    order_state_store.record_tickets(coordination.tickets)
    order_state_store.record_events(coordination.events)
    repeated_sell = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.SELL,
            1,
            674_000,
            metadata={"current_quantity": 4, "target_quantity": 3, "delta_quantity": -1},
        )
    )

    report = EngineGuard().evaluate(
        batches=(repeated_sell,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=True,
        generated_at=datetime(2026, 5, 12, 10, 9),
    )

    assert report.blocked is True
    assert "target_quantity_already_covered_by_pending_orders" in report.errors
    decision = next(decision for decision in report.decisions if decision.reason == "target_quantity_already_covered_by_pending_orders")
    assert decision.metadata["held_quantity"] == 4
    assert decision.metadata["open_sell_quantity"] == 1
    assert decision.metadata["projected_quantity"] == 3
    assert decision.metadata["target_quantity"] == 3


def test_engine_guard_blocks_order_quantity_that_exceeds_unreserved_target_delta(tmp_path):
    symbol = Symbol("005930", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 10_000_000},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-samsung",
            order_id="seed-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=2,
            fill_price=70_000,
            filled_at=datetime(2026, 5, 12, 8, 50),
            sleeve_id="LEaps",
        )
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_buy = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.BUY,
            1,
            70_000,
            metadata={"current_quantity": 2, "target_quantity": 4, "delta_quantity": 2},
        )
    )
    coordination = OrderCoordinator().coordinate((existing_buy,), generated_at=datetime(2026, 5, 12, 10, 7))
    order_state_store.record_tickets(coordination.tickets)
    order_state_store.record_events(coordination.events)
    stale_buy = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.BUY,
            2,
            70_000,
            metadata={"current_quantity": 2, "target_quantity": 4, "delta_quantity": 2},
        )
    )

    report = EngineGuard().evaluate(
        batches=(stale_buy,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=True,
        generated_at=datetime(2026, 5, 12, 10, 9),
    )

    assert report.blocked is True
    assert "order_quantity_exceeds_unreserved_target_delta" in report.errors
    decision = next(decision for decision in report.decisions if decision.reason == "order_quantity_exceeds_unreserved_target_delta")
    assert decision.metadata["projected_quantity"] == 3
    assert decision.metadata["unreserved_delta"] == 1
    assert decision.metadata["requested_delta"] == 2


def test_engine_guard_allows_incremental_order_when_target_quantity_increases(tmp_path):
    symbol = Symbol("005930", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 10_000_000},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-samsung",
            order_id="seed-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=2,
            fill_price=70_000,
            filled_at=datetime(2026, 5, 12, 8, 50),
            sleeve_id="LEaps",
        )
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_buy = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.BUY,
            1,
            70_000,
            metadata={"current_quantity": 2, "target_quantity": 4, "delta_quantity": 2},
        )
    )
    coordination = OrderCoordinator().coordinate((existing_buy,), generated_at=datetime(2026, 5, 12, 10, 7))
    order_state_store.record_tickets(coordination.tickets)
    order_state_store.record_events(coordination.events)
    increased_target_buy = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 12, 10, 9),
        batch_id="batch-2",
        order_intents=(
            OrderIntent(
                "LEaps",
                symbol,
                OrderSide.BUY,
                2,
                70_000,
                metadata={"current_quantity": 2, "target_quantity": 5, "delta_quantity": 3},
            ),
        ),
    )

    report = EngineGuard().evaluate(
        batches=(increased_target_buy,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=True,
        generated_at=datetime(2026, 5, 12, 10, 9),
    )

    assert report.blocked is False
    assert "target_quantity_already_covered_by_pending_orders" not in report.errors
    assert "order_quantity_exceeds_unreserved_target_delta" not in report.errors


def test_engine_guard_does_not_double_count_execution_history_fill_already_applied_to_virtual_account(tmp_path):
    symbol = Symbol("005380", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 10_000_000},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-hyundai",
            order_id="seed-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=4,
            fill_price=670_000,
            filled_at=datetime(2026, 5, 12, 8, 50),
            sleeve_id="LEaps",
        )
    )
    kis_fill_id = "kis:domestic:order-summary:0023103700:20260512T100755"
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id=kis_fill_id,
            order_id="sell-intent",
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=1,
            fill_price=678_000,
            filled_at=datetime(2026, 5, 12, 10, 7),
            sleeve_id="LEaps",
        )
    )
    order_state_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_sell = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.SELL,
            1,
            678_000,
            metadata={"current_quantity": 4, "target_quantity": 3, "delta_quantity": -1},
        )
    )
    coordination = OrderCoordinator().coordinate((existing_sell,), generated_at=datetime(2026, 5, 12, 10, 7))
    ticket = coordination.tickets[0]
    fill_event = ticket.event(
        OrderEventType.FILLED,
        occurred_at=datetime(2026, 5, 12, 10, 7),
        quantity=1,
        fill_price=678_000,
        broker_order_id="91255:0023103700",
        reason="execution_history_reconcile_fill",
        metadata={"fill_id": kis_fill_id},
    )
    order_state_store.record_tickets(coordination.tickets)
    order_state_store.record_events((*coordination.events, fill_event))
    stale_buy = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.BUY,
            1,
            674_000,
            metadata={"current_quantity": 2, "target_quantity": 3, "delta_quantity": 1},
        )
    )

    report = EngineGuard().evaluate(
        batches=(stale_buy,),
        account_store=account_store,
        order_state_store=order_state_store,
        account_id="kis-domestic",
        market_scope="domestic",
        commit=True,
        generated_at=datetime(2026, 5, 12, 10, 10),
    )

    assert report.blocked is True
    assert "target_quantity_already_covered_by_pending_orders" in report.errors
    decision = next(decision for decision in report.decisions if decision.reason == "target_quantity_already_covered_by_pending_orders")
    assert decision.metadata["held_quantity"] == 3
    assert decision.metadata["unapplied_fill_delta"] == 0
    assert decision.metadata["projected_quantity"] == 3


def test_engine_guard_rejects_missing_orderable_session_for_confirmed_live_submit(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )

    report = EngineGuard().evaluate(
        batches=(_batch(OrderIntent("LEaps", Symbol("005930", "KRX"), OrderSide.BUY, 1, 70_000)),),
        account_store=account_store,
        account_id="kis-domestic",
        market_scope="domestic",
        broker="broker-engine",
        commit=True,
        require_orderable_session=True,
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert "missing_market_session" in report.errors


def test_engine_guard_allows_orderable_session_and_warns_invalid_krx_tick(tmp_path):
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )

    report = EngineGuard().evaluate(
        batches=(
            _batch(
                OrderIntent(
                    "LEaps",
                    Symbol("005930", "KRX"),
                    OrderSide.BUY,
                    1,
                    70_000,
                    order_type=OrderType.LIMIT,
                    limit_price=70_150,
                )
            ),
        ),
        account_store=account_store,
        account_id="kis-domestic",
        market_scope="domestic",
        require_orderable_session=True,
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="regular_continuous",
            is_orderable=True,
            is_regular_market_open=True,
        ),
        generated_at=datetime(2026, 5, 10, 9, 1),
    )

    assert report.blocked is False
    assert "limit_price_not_on_krx_tick" in report.warnings

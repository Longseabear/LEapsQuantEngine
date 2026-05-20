import json
from datetime import datetime

from leaps_quant_engine.brokerage import BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_submit import OrderRuntimeSubmitter, load_order_intent_batches, write_order_intent_batches
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol, TimeInForce
from leaps_quant_engine.orders import OrderCoordinator
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def _batch(*orders, batch_id="batch-1"):
    return OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 10, 9, 0),
        order_intents=tuple(orders),
        batch_id=batch_id,
    )


def test_load_order_intent_batches_accepts_framework_execution_json(tmp_path):
    batch_path = tmp_path / "orders.json"
    batch_path.write_text(
        json.dumps(
            {
                "batches": [
                    {
                        "batch_id": "batch-1",
                        "sleeve_id": "LEaps",
                        "generated_at": "2026-05-10T09:30:00",
                        "orders": [
                            {
                                "symbol": "KRX:005930",
                                "side": "buy",
                                "quantity": 2,
                                "reference_price": 70000,
                                "order_type": "market",
                                "time_in_force": "ioc",
                                "tag": "rebalance",
                                "metadata": {"execution": "market_ioc"},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    batches = load_order_intent_batches(batch_path)

    assert len(batches) == 1
    assert batches[0].batch_id == "batch-1"
    assert batches[0].order_intents[0].symbol.key == "KRX:005930"
    assert batches[0].order_intents[0].notional == 140000
    assert batches[0].order_intents[0].order_type is OrderType.MARKET
    assert batches[0].order_intents[0].limit_price is None
    assert batches[0].order_intents[0].time_in_force is TimeInForce.IOC
    assert batches[0].order_intents[0].metadata["execution"] == "market_ioc"


def test_order_runtime_submitter_blocks_live_submit_without_confirmation(tmp_path):
    batch_path = tmp_path / "orders.json"
    batch_path.write_text(
        json.dumps(
            {
                "batch_id": "batch-1",
                "sleeve_id": "LEaps",
                "orders": [
                    {
                        "symbol": "KRX:005930",
                        "side": "buy",
                        "quantity": 1,
                        "reference_price": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")

    report = OrderRuntimeSubmitter(
        runtime_id="test-runtime",
        order_state_store=order_store,
        account_store=account_store,
    ).submit_batches(
        load_order_intent_batches(batch_path),
        allowed_sleeve_ids=("LEaps",),
        broker="broker-engine",
        commit=True,
        generated_at=datetime(2026, 5, 10, 9, 31),
    )

    assert report.status == "blocked"
    assert report.errors == ("broker_engine_submit_requires_confirm_live_submit",)
    assert len(report.final_status.order_snapshot.tickets) == 0


def test_write_order_intent_batches_round_trips_submit_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "orders.json"
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
                tag="artifact",
                limit_price=101,
                time_in_force=TimeInForce.FOK,
            ),
        ),
        batch_id="batch-1",
    )

    summary = write_order_intent_batches(
        artifact_path,
        (batch,),
        runtime_id="test-runtime",
        config_version="sha256:test",
        source="test",
        generated_at=datetime(2026, 5, 10, 9, 31),
    )
    reloaded = load_order_intent_batches(artifact_path)

    assert summary["batch_count"] == 1
    assert summary["order_count"] == 1
    assert reloaded[0].batch_id == "batch-1"
    assert reloaded[0].order_intents[0].symbol.key == "KRX:005930"
    assert reloaded[0].order_intents[0].limit_price == 101
    assert reloaded[0].order_intents[0].time_in_force is TimeInForce.FOK


def test_order_runtime_submitter_blocks_duplicate_commit_from_same_artifact(tmp_path):
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 10, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=1,
                reference_price=70_000,
            ),
        ),
        batch_id="batch-1",
    )
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 10, 9, 30))
    order_store.record_tickets(existing.tickets)

    report = OrderRuntimeSubmitter(
        runtime_id="test-runtime",
        order_state_store=order_store,
        account_store=account_store,
        broker_account_id="kis-domestic",
        market_scope="domestic",
    ).submit_batches(
        (batch,),
        allowed_sleeve_ids=("LEaps",),
        broker="paper",
        commit=True,
        generated_at=datetime(2026, 5, 10, 9, 31),
    )

    assert report.status == "blocked"
    assert report.errors == ("duplicate_order_intent_already_recorded",)
    assert len(report.final_status.order_snapshot.tickets) == 1


def test_order_runtime_submitter_drops_guard_rejected_orders_and_submits_rest(tmp_path):
    covered_symbol = Symbol("131970", "KRX")
    retry_symbol = Symbol("036930", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 0},
    )
    for fill_id, symbol in (("seed-covered", covered_symbol), ("seed-retry", retry_symbol)):
        account_store.apply_fill(
            VirtualFillEvent(
                fill_id=fill_id,
                order_id=fill_id,
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=2,
                fill_price=100_000,
                filled_at=datetime(2026, 5, 18, 8, 50),
                sleeve_id="LEaps",
            )
        )

    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_sell = _batch(
        OrderIntent(
            "LEaps",
            covered_symbol,
            OrderSide.SELL,
            2,
            100_000,
            metadata={"current_quantity": 2, "target_quantity": 0, "delta_quantity": -2},
        )
    )
    existing_coordination = OrderCoordinator().coordinate((existing_sell,), generated_at=datetime(2026, 5, 18, 9, 0))
    order_store.record_tickets(existing_coordination.tickets)
    order_store.record_events(existing_coordination.events)

    candidate = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 18, 9, 1),
        batch_id="batch-2",
        order_intents=(
            OrderIntent(
                "LEaps",
                covered_symbol,
                OrderSide.SELL,
                2,
                100_000,
                metadata={"current_quantity": 2, "target_quantity": 0, "delta_quantity": -2},
            ),
            OrderIntent(
                "LEaps",
                retry_symbol,
                OrderSide.SELL,
                2,
                100_000,
                metadata={"current_quantity": 2, "target_quantity": 0, "delta_quantity": -2},
            ),
        ),
    )
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=False)),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = OrderRuntimeSubmitter(
        runtime_id="test-runtime",
        order_state_store=order_store,
        account_store=account_store,
        broker_account_id="kis-domestic",
        market_scope="domestic",
        orchestrator=orchestrator,
    ).submit_batches(
        (candidate,),
        allowed_sleeve_ids=("LEaps",),
        broker="paper",
        commit=True,
        generated_at=datetime(2026, 5, 18, 9, 2),
    )

    assert report.status == "submitted_with_warnings"
    assert report.order_count == 1
    assert report.coordination.tickets[0].symbol == retry_symbol
    assert report.guard.blocked is False
    assert any(
        warning.startswith("dropped_guard_rejected_order_intent:LEaps:KRX:131970:sell")
        for warning in report.warnings
    )


def test_order_runtime_submitter_clamps_order_to_unreserved_target_delta(tmp_path):
    symbol = Symbol("417840", "KRX")
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 0},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed-jusung",
            order_id="seed-order",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=105,
            fill_price=17_380,
            filled_at=datetime(2026, 5, 19, 8, 50),
            sleeve_id="LEaps",
        )
    )

    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")
    existing_sell = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.SELL,
            1,
            17_250,
            metadata={"current_quantity": 105, "target_quantity": 104, "delta_quantity": -1},
        )
    )
    existing_coordination = OrderCoordinator().coordinate((existing_sell,), generated_at=datetime(2026, 5, 19, 10, 37))
    order_store.record_tickets(existing_coordination.tickets)
    order_store.record_events(existing_coordination.events)

    candidate = _batch(
        OrderIntent(
            "LEaps",
            symbol,
            OrderSide.SELL,
            53,
            16_910,
            metadata={"current_quantity": 105, "target_quantity": 52, "delta_quantity": -53},
        ),
        batch_id="batch-2",
    )
    orchestrator = MultiSleeveOrderOrchestrator(
        broker=BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=False)),
        account_store=account_store,
        order_state_store=order_store,
    )

    report = OrderRuntimeSubmitter(
        runtime_id="test-runtime",
        order_state_store=order_store,
        account_store=account_store,
        broker_account_id="kis-domestic",
        market_scope="domestic",
        orchestrator=orchestrator,
    ).submit_batches(
        (candidate,),
        allowed_sleeve_ids=("LEaps",),
        broker="paper",
        commit=True,
        generated_at=datetime(2026, 5, 19, 11, 2),
    )

    assert report.status == "submitted_with_warnings"
    assert report.order_count == 1
    assert report.coordination.tickets[0].quantity == 52
    assert report.coordination.tickets[0].metadata["engine_guard_adjusted_quantity"] == 52
    assert report.guard is not None
    assert report.guard.blocked is False
    assert any(
        warning.startswith(
            "adjusted_guard_rejected_order_intent:LEaps:KRX:417840:sell:order_quantity_exceeds_unreserved_target_delta:53->52"
        )
        for warning in report.warnings
    )


def test_order_runtime_submitter_stamps_order_session_on_tickets(tmp_path):
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 15, 41),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.SELL,
                quantity=1,
                reference_price=70_000,
                order_type=OrderType.LIMIT,
                limit_price=70_000,
                time_in_force=TimeInForce.DAY,
            ),
        ),
        batch_id="batch-after-hours",
    )
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 0},
    )
    account_store.apply_fill(
        VirtualFillEvent(
            fill_id="seed",
            order_id="seed",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=1,
            fill_price=69_000,
            filled_at=datetime(2026, 5, 13, 9, 0),
            sleeve_id="LEaps",
        )
    )
    order_store = FileOrderRuntimeStateStore(tmp_path / "orders.jsonl")

    report = OrderRuntimeSubmitter(
        runtime_id="test-runtime",
        order_state_store=order_store,
        account_store=account_store,
        broker_account_id="kis-domestic",
        market_scope="domestic",
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="after_hours_close",
            is_orderable=True,
            is_regular_market_open=False,
            source="test",
        ),
        require_orderable_session=True,
    ).submit_batches(
        (batch,),
        allowed_sleeve_ids=("LEaps",),
        broker="broker-engine",
        commit=False,
        confirm_live_submit=True,
        generated_at=datetime(2026, 5, 13, 15, 42),
    )

    ticket = report.coordination.tickets[0]
    assert ticket.metadata["order_session"] == "after_hours_close"
    assert ticket.metadata["market_session_phase"] == "after_hours_close"
    assert ticket.metadata["market_session_scope"] == "domestic"

import json
from datetime import datetime

from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_submit import OrderRuntimeSubmitter, load_order_intent_batches, write_order_intent_batches
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol, TimeInForce
from leaps_quant_engine.orders import OrderCoordinator
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


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

import json
from datetime import datetime

from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_submit import OrderRuntimeSubmitter, load_order_intent_batches, write_order_intent_batches
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
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
                                "tag": "rebalance",
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

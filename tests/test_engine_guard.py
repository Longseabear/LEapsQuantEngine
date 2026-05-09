from datetime import datetime

from leaps_quant_engine.engine_guard import EngineGuard
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
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

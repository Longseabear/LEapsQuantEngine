from datetime import datetime

from leaps_quant_engine.account_sync import KISAccountClient, KISVirtualAccountSync, execution_to_virtual_fill
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


class FakeBroker:
    def __init__(self, result_by_operation):
        self.result_by_operation = result_by_operation
        self.calls = []

    def call_operation(self, operation, arguments=None):
        self.calls.append((operation, arguments or {}))
        return self.result_by_operation[operation]


def test_execution_to_virtual_fill_normalizes_kis_execution():
    fill = execution_to_virtual_fill(
        {
            "order_id": "12345",
            "symbol": "005930",
            "side": "buy",
            "execution_quantity": "3",
            "execution_price": "70000",
            "execution_timestamp": "20260508T093000",
        },
        market="domestic",
    )

    assert fill.fill_id == "kis:domestic:12345:20260508T093000:3:70000"
    assert fill.symbol == Symbol("005930", "KRX")
    assert fill.side is OrderSide.BUY
    assert fill.quantity == 3
    assert fill.fill_price == 70000.0
    assert fill.filled_at == datetime(2026, 5, 8, 9, 30)


def test_kis_account_sync_imports_owned_and_unassigned_fills(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    store.register_order_intent(
        OrderIntent(
            sleeve_id="LEaps",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=2,
            reference_price=70_000,
        ),
        order_id="owned-order",
        broker_order_id="owned-order",
    )
    fake_broker = FakeBroker(
        {
            "get_account_balance_summary": {"cash_balance": 500_000, "holdings_count": 2},
            "get_account_holdings": {
                "holdings_count": 99,
                "holdings": [
                    {"symbol": "005930", "holding_quantity": 100},
                ],
            },
            "get_account_execution_history": {
                "executions": [
                    {
                        "order_id": "owned-order",
                        "symbol": "005930",
                        "side": "buy",
                        "execution_quantity": "2",
                        "execution_price": "70000",
                        "execution_timestamp": "20260508T093000",
                    },
                    {
                        "order_id": "external-order",
                        "symbol": "000660",
                        "side": "buy",
                        "execution_quantity": "1",
                        "execution_price": "120000",
                        "execution_timestamp": "20260508T100000",
                    },
                ],
            },
        }
    )
    sync = KISVirtualAccountSync(KISAccountClient(fake_broker))

    report = sync.sync(
        store,
        start_date="20260508",
        end_date="20260508",
        report_sleeve_ids=("LEaps",),
    )

    assert report.imported_fill_count == 1
    assert report.unassigned_fill_count == 0
    assert report.unallocated_fill_count == 1
    assert report.holdings["holdings_count"] == 99
    assert store.current_portfolio("LEaps").holdings["KRX:005930"].quantity == 2
    assert store.current_portfolio("unassigned").holdings == {}
    assert store.broker_fill("kis:domestic:external-order:20260508T100000:1:120000") is not None

    repeated = sync.sync(
        store,
        start_date="20260508",
        end_date="20260508",
        report_sleeve_ids=("LEaps",),
    )
    assert repeated.imported_fill_count == 0
    assert repeated.duplicate_fill_count == 2
    assert repeated.unallocated_fill_count == 0


def test_kis_account_sync_can_assign_unknown_fills_to_requested_sleeve(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    fake_broker = FakeBroker(
        {
            "get_account_balance_summary": {},
            "get_account_holdings": {"holdings": []},
            "get_account_execution_history": {
                "executions": [
                    {
                        "order_id": "manual-order",
                        "symbol": "005930",
                        "side": "buy",
                        "execution_quantity": "1",
                        "execution_price": "70000",
                        "execution_timestamp": "20260508T093000",
                    },
                ],
            },
        }
    )
    sync = KISVirtualAccountSync(KISAccountClient(fake_broker))

    report = sync.sync(
        store,
        start_date="20260508",
        end_date="20260508",
        assign_unknown_to_sleeve_id="LEaps",
        report_sleeve_ids=("LEaps",),
    )

    assert report.imported_fill_count == 1
    assert report.unassigned_fill_count == 0
    assert store.current_portfolio("LEaps").holdings["KRX:005930"].quantity == 1


def test_kis_account_sync_can_sync_cash_to_default_sleeve(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 100_000, "default sleeve": 0},
    )
    store.current_portfolio("LEaps")
    fake_broker = FakeBroker(
        {
            "get_account_balance_summary": {"cash_balance": 1_000_000},
            "get_account_holdings": {"holdings": []},
            "get_account_execution_history": {"executions": []},
        }
    )
    sync = KISVirtualAccountSync(KISAccountClient(fake_broker))

    report = sync.sync(
        store,
        start_date="20260508",
        end_date="20260508",
        sync_cash=True,
        report_sleeve_ids=("LEaps",),
    )

    assert report.cash_reconciliation["status"] == "matched"
    assert store.current_portfolio("default sleeve").cash == 900_000

from datetime import datetime
import json
import os

import pytest

from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.virtual_account import (
    FillAllocation,
    UNKNOWN_SLEEVE_ID,
    VirtualFillEvent,
    VirtualSleeveAccountStore,
)


def test_virtual_sleeve_account_reads_default_current_portfolio(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )

    portfolio = store.current_portfolio("LEaps")

    assert portfolio.cash == 1_000_000
    assert portfolio.holdings == {}


def test_virtual_sleeve_account_retries_transient_windows_replace_lock(monkeypatch, tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
    )
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError("transient lock")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)

    portfolio = store.current_portfolio("LEaps")

    assert portfolio.cash == 1_000_000
    assert calls["count"] == 2


def test_virtual_sleeve_account_reads_multi_currency_default_current_portfolio(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_currency_by_sleeve={"LEaps": {"KRW": 10_000_000, "USD": 3434.25}},
        default_currency="USD",
    )

    portfolio = store.current_portfolio("LEaps")

    assert portfolio.cash_by_currency == {"KRW": 10_000_000.0, "USD": 3434.25}


def test_virtual_sleeve_account_repairs_missing_configured_cash_bucket_before_activity(tmp_path):
    path = tmp_path / "accounts.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sleeves": {"LEaps": {"cash": 3434.25, "cash_by_currency": {"USD": 3434.25}, "holdings": {}}},
                "order_ownership": {},
                "broker_order_index": {},
                "fills": {},
                "broker_fills": {},
                "fill_allocations": {},
                "account_cash_snapshots": {},
                "cash_transfers": {},
            }
        ),
        encoding="utf-8",
    )
    store = VirtualSleeveAccountStore(
        path,
        default_cash_by_currency_by_sleeve={"LEaps": {"KRW": 10_000_000, "USD": 3434.25}},
        default_currency="USD",
    )

    portfolio = store.current_portfolio("LEaps")

    assert portfolio.cash_by_currency == {"USD": 3434.25, "KRW": 10_000_000.0}


def test_virtual_sleeve_account_routes_fill_by_order_ownership(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    store.initialize_sleeve("LEaps", cash=1_000_000)
    symbol = Symbol("005930", "KRX")
    order = OrderIntent(
        sleeve_id="LEaps",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=3,
        reference_price=70_000,
        tag="framework:demo",
    )

    ownership = store.register_order_intent(
        order,
        order_id="local-1",
        broker_order_id="kis-1",
        created_at=datetime(2026, 5, 9, 9, 0),
    )
    portfolio = store.apply_fill(
        VirtualFillEvent(
            fill_id="fill-1",
            order_id="local-1",
            broker_order_id="kis-1",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=3,
            fill_price=70_100,
            fee=100,
            filled_at=datetime(2026, 5, 9, 9, 1),
        )
    )

    assert ownership.sleeve_id == "LEaps"
    assert portfolio.cash == 789_600
    assert portfolio.quantity(symbol) == 3
    assert portfolio.holdings[symbol.key].average_price == 70_100
    reloaded = VirtualSleeveAccountStore(tmp_path / "accounts.json").current_portfolio("LEaps")
    assert reloaded.cash == 789_600
    assert reloaded.quantity(symbol) == 3


def test_virtual_sleeve_account_tracks_position_state_from_fills_and_marks(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    symbol = Symbol("005930", "KRX")
    first_fill_at = datetime(2026, 5, 9, 9, 1)
    second_fill_at = datetime(2026, 5, 9, 9, 5)
    mark_at = datetime(2026, 5, 9, 10, 0)
    store.initialize_sleeve("LEaps", cash=1_000_000)

    store.apply_fill(
        VirtualFillEvent(
            fill_id="buy-fill-1",
            order_id="buy-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=2,
            fill_price=70_000,
            filled_at=first_fill_at,
        )
    )
    store.apply_fill(
        VirtualFillEvent(
            fill_id="buy-fill-2",
            order_id="buy-2",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=1,
            fill_price=72_000,
            filled_at=second_fill_at,
        )
    )
    marked = store.update_position_mark(
        sleeve_id="LEaps",
        symbol=symbol,
        price=75_000,
        marked_at=mark_at,
        stop_price=69_000,
    )

    assert marked is not None
    assert marked.quantity == 3
    assert marked.average_entry_price == (70_000 * 2 + 72_000) / 3
    assert marked.entry_time == first_fill_at
    assert marked.high_watermark_price == 75_000
    assert marked.high_watermark_at == mark_at
    assert marked.last_stop_price == 69_000
    reloaded = VirtualSleeveAccountStore(tmp_path / "accounts.json").position_state("LEaps", symbol)
    assert reloaded == marked


def test_virtual_sleeve_account_keeps_position_state_on_partial_sell_and_clears_on_exit(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    symbol = Symbol("005930", "KRX")
    entry_at = datetime(2026, 5, 9, 9, 1)
    store.initialize_sleeve("LEaps", cash=1_000_000)
    store.apply_fill(
        VirtualFillEvent(
            fill_id="buy-fill",
            order_id="buy-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=3,
            fill_price=70_000,
            filled_at=entry_at,
        )
    )
    store.update_position_mark(
        sleeve_id="LEaps",
        symbol=symbol,
        price=75_000,
        marked_at=datetime(2026, 5, 9, 10, 0),
    )

    store.apply_fill(
        VirtualFillEvent(
            fill_id="sell-fill-1",
            order_id="sell-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=1,
            fill_price=73_000,
            filled_at=datetime(2026, 5, 9, 10, 30),
        )
    )
    partial = store.position_state("LEaps", symbol)

    assert partial is not None
    assert partial.quantity == 2
    assert partial.entry_time == entry_at
    assert partial.high_watermark_price == 75_000

    store.apply_fill(
        VirtualFillEvent(
            fill_id="sell-fill-2",
            order_id="sell-2",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=2,
            fill_price=74_000,
            filled_at=datetime(2026, 5, 9, 11, 0),
        )
    )

    assert store.position_state("LEaps", symbol) is None


def test_virtual_sleeve_account_sell_fill_reduces_known_sleeve_holding(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    symbol = Symbol("005930", "KRX")
    store.initialize_sleeve("LEaps", cash=1_000_000)
    store.apply_fill(
        VirtualFillEvent(
            fill_id="buy-fill",
            order_id="buy-1",
            sleeve_id="LEaps",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=5,
            fill_price=10_000,
            filled_at=datetime(2026, 5, 9, 9, 1),
        )
    )
    sell_order = OrderIntent(
        sleeve_id="LEaps",
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=2,
        reference_price=11_000,
    )
    store.register_order_intent(sell_order, order_id="sell-1")

    portfolio = store.apply_fill(
        VirtualFillEvent(
            fill_id="sell-fill",
            order_id="sell-1",
            symbol=symbol,
            side=OrderSide.SELL,
            quantity=2,
            fill_price=11_000,
            fee=50,
            filled_at=datetime(2026, 5, 9, 9, 5),
        )
    )

    assert portfolio.cash == 971_950
    assert portfolio.quantity(symbol) == 3
    assert portfolio.holdings[symbol.key].average_price == 10_000


def test_virtual_sleeve_account_rejects_oversell_for_known_sleeve(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    symbol = Symbol("005930", "KRX")
    store.initialize_sleeve("LEaps", cash=1_000_000)

    with pytest.raises(ValueError, match="sell fill exceeds"):
        store.apply_fill(
            VirtualFillEvent(
                fill_id="sell-fill",
                order_id="sell-1",
                sleeve_id="LEaps",
                symbol=symbol,
                side=OrderSide.SELL,
                quantity=1,
                fill_price=11_000,
                filled_at=datetime(2026, 5, 9, 9, 5),
            )
        )


def test_virtual_sleeve_account_unknown_fill_goes_to_unassigned(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    symbol = Symbol("005930", "KRX")

    portfolio = store.apply_fill(
        VirtualFillEvent(
            fill_id="external-buy",
            order_id="external-1",
            symbol=symbol,
            side=OrderSide.BUY,
            quantity=1,
            fill_price=10_000,
            filled_at=datetime(2026, 5, 9, 9, 1),
        )
    )

    assert portfolio.cash == -10_000
    assert store.current_portfolio(UNKNOWN_SLEEVE_ID).quantity(symbol) == 1
    assert store.ownership_for_order("external-1").sleeve_id == UNKNOWN_SLEEVE_ID


def test_virtual_sleeve_account_allocates_one_broker_fill_across_sleeves(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000, "ETF": 500_000},
    )
    symbol = Symbol("005930", "KRX")
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        broker_order_id="broker-order-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=10,
        fill_price=70_000,
        fee=100,
        filled_at=datetime(2026, 5, 9, 9, 1),
    )

    portfolios = store.apply_fill_allocations(
        fill,
        (
            FillAllocation(fill_id="broker-fill-1", sleeve_id="LEaps", quantity=6, allocation_id="alloc-a"),
            FillAllocation(fill_id="broker-fill-1", sleeve_id="ETF", quantity=4, allocation_id="alloc-b"),
        ),
    )

    assert portfolios["LEaps"].quantity(symbol) == 6
    assert portfolios["LEaps"].cash == 579_940
    assert portfolios["ETF"].quantity(symbol) == 4
    assert portfolios["ETF"].cash == 219_960
    assert store.current_portfolio("LEaps").holdings[symbol.key].average_price == 70_000
    assert store.current_portfolio("ETF").holdings[symbol.key].average_price == 70_000
    assert store.ownership_for_order("broker-order-1") is None


def test_virtual_sleeve_account_can_ignore_operator_owned_broker_fill(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    fill = VirtualFillEvent(
        fill_id="manual-sell-fill",
        order_id="manual-order",
        broker_order_id="manual-order",
        symbol=Symbol("005930", "KRX"),
        side=OrderSide.SELL,
        quantity=2,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 14, 15, 10),
    )
    assert store.record_broker_fill(fill) is True

    ignored = store.ignore_broker_fill(
        fill.fill_id,
        reason="manual position outside engine sleeve",
        ignored_by="operator",
        ignored_at=datetime(2026, 5, 14, 15, 11),
    )
    statuses = store.fill_allocation_statuses()
    report = store.reconciliation_report([], include_fills=True)

    assert ignored.fill_id == fill.fill_id
    assert statuses[0].status == "ignored"
    assert statuses[0].remaining_quantity == 0
    assert report.unallocated_fill_count == 0
    payload = report.to_dict()
    assert payload["unallocated_fills"] == []
    assert payload["ignored_fills"][0]["fill_id"] == fill.fill_id


def test_virtual_sleeve_account_rejects_allocating_ignored_broker_fill(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    fill = VirtualFillEvent(
        fill_id="manual-sell-fill",
        order_id="manual-order",
        symbol=Symbol("005930", "KRX"),
        side=OrderSide.SELL,
        quantity=2,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 14, 15, 10),
    )
    store.record_broker_fill(fill)
    store.ignore_broker_fill(fill.fill_id, reason="manual")

    with pytest.raises(ValueError, match="ignored broker fill"):
        store.apply_fill_allocations(
            fill,
            (FillAllocation(fill_id=fill.fill_id, sleeve_id="LEaps", quantity=2),),
        )


def test_virtual_sleeve_account_allows_partial_fill_allocation(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    symbol = Symbol("005930", "KRX")
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=6,
        fill_price=169_000,
        filled_at=datetime(2026, 3, 31, 14, 54, 9),
    )
    store.record_broker_fill(fill)

    portfolios = store.apply_fill_allocations(
        fill,
        (FillAllocation(fill_id="broker-fill-1", sleeve_id="LEaps", quantity=4, allocation_id="alloc-a"),),
    )

    assert portfolios["LEaps"].quantity(symbol) == 4
    assert store.current_portfolio("LEaps").quantity(symbol) == 4
    assert store.broker_fill("broker-fill-1") == fill
    statuses = store.fill_allocation_statuses(symbol=symbol)
    assert len(statuses) == 1
    assert statuses[0].allocated_quantity == 4
    assert statuses[0].remaining_quantity == 2
    assert statuses[0].status == "partially_allocated"


def test_virtual_sleeve_account_rejects_overallocated_fill(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        symbol=Symbol("005930", "KRX"),
        side=OrderSide.BUY,
        quantity=6,
        fill_price=169_000,
        filled_at=datetime(2026, 3, 31, 14, 54, 9),
    )

    with pytest.raises(ValueError, match="exceed the fill quantity"):
        store.apply_fill_allocations(
            fill,
            (
                FillAllocation(fill_id="broker-fill-1", sleeve_id="LEaps", quantity=4, allocation_id="alloc-a"),
                FillAllocation(fill_id="broker-fill-1", sleeve_id="ETF", quantity=3, allocation_id="alloc-b"),
            ),
        )


def test_virtual_sleeve_account_can_record_broker_fill_without_changing_portfolio(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    symbol = Symbol("005930", "KRX")
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=10,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 9, 9, 1),
    )

    assert store.record_broker_fill(fill) is True
    assert store.record_broker_fill(fill) is False
    assert store.current_portfolio("LEaps").holdings == {}
    assert store.broker_fill("broker-fill-1") == fill


def test_virtual_sleeve_account_allocates_sell_fill_by_sleeve_projection(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000, "ETF": 500_000},
    )
    symbol = Symbol("005930", "KRX")
    buy_fill = VirtualFillEvent(
        fill_id="broker-buy-1",
        order_id="broker-buy-order-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=10,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 9, 9, 1),
    )
    store.apply_fill_allocations(
        buy_fill,
        (
            FillAllocation(fill_id="broker-buy-1", sleeve_id="LEaps", quantity=6, allocation_id="buy-a"),
            FillAllocation(fill_id="broker-buy-1", sleeve_id="ETF", quantity=4, allocation_id="buy-b"),
        ),
    )

    sell_fill = VirtualFillEvent(
        fill_id="broker-sell-1",
        order_id="broker-sell-order-1",
        symbol=symbol,
        side=OrderSide.SELL,
        quantity=5,
        fill_price=75_000,
        filled_at=datetime(2026, 5, 9, 10, 1),
    )
    portfolios = store.apply_fill_allocations(
        sell_fill,
        (
            FillAllocation(fill_id="broker-sell-1", sleeve_id="LEaps", quantity=3, allocation_id="sell-a"),
            FillAllocation(fill_id="broker-sell-1", sleeve_id="ETF", quantity=2, allocation_id="sell-b"),
        ),
    )

    assert portfolios["LEaps"].quantity(symbol) == 3
    assert portfolios["ETF"].quantity(symbol) == 2
    assert portfolios["LEaps"].holdings[symbol.key].average_price == 70_000
    assert portfolios["ETF"].holdings[symbol.key].average_price == 70_000


def test_virtual_sleeve_account_rejects_allocation_larger_than_fill(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json")
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        symbol=Symbol("005930", "KRX"),
        side=OrderSide.BUY,
        quantity=10,
        fill_price=70_000,
        filled_at=datetime(2026, 5, 9, 9, 1),
    )

    with pytest.raises(ValueError, match="exceed the fill quantity"):
        store.apply_fill_allocations(
            fill,
            (FillAllocation(fill_id="broker-fill-1", sleeve_id="LEaps", quantity=11),),
        )


def test_virtual_sleeve_account_reconciliation_compares_broker_and_virtual_positions(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})
    symbol = Symbol("005930", "KRX")
    fill = VirtualFillEvent(
        fill_id="broker-fill-1",
        order_id="broker-order-1",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=6,
        fill_price=169_000,
        filled_at=datetime(2026, 3, 31, 14, 54, 9),
    )
    store.record_broker_fill(fill)
    store.apply_fill_allocations(
        fill,
        (FillAllocation(fill_id="broker-fill-1", sleeve_id="LEaps", quantity=4, allocation_id="alloc-a"),),
    )

    report = store.reconciliation_report(
        {
            "holdings": [
                {
                    "symbol": "005930",
                    "holding_quantity": 6,
                    "average_purchase_price": 169_000,
                }
            ]
        }
    )

    assert report.status == "needs_reconciliation"
    assert report.mismatch_count == 1
    assert report.unallocated_fill_count == 1
    assert report.rows[0].broker_quantity == 6
    assert report.rows[0].virtual_quantity == 4
    assert report.rows[0].difference == -2
    payload = report.to_dict()
    assert payload["rows"][0]["status"] == "mismatch"
    assert payload["unallocated_fills"][0]["remaining_quantity"] == 2


def test_virtual_sleeve_account_reconciliation_prefers_current_broker_quantity(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"us_etf_rotation": 10_000})
    store.apply_fill(
        VirtualFillEvent(
            fill_id="fill-smh-1",
            order_id="order-smh-1",
            symbol=Symbol("SMH", "US"),
            side=OrderSide.BUY,
            quantity=3,
            fill_price=570.0,
            sleeve_id="us_etf_rotation",
            filled_at=datetime(2026, 5, 15, 10, 0),
        )
    )

    report = store.reconciliation_report(
        {
            "holdings": [
                {
                    "symbol": "SMH",
                    "market": "US",
                    "holding_quantity": 2,
                    "current_quantity": 4,
                    "settled_quantity": 2,
                    "orderable_quantity": 4,
                    "quantity_source": "ccld_qty_smtl1",
                    "average_purchase_price": 566.8588,
                }
            ]
        },
        include_fills=False,
    )

    assert report.status == "needs_reconciliation"
    assert report.rows[0].broker_quantity == 4
    assert report.rows[0].virtual_quantity == 3
    assert report.rows[0].difference == -1
    payload = report.to_dict()
    assert payload["rows"][0]["broker_quantity_source"] == "ccld_qty_smtl1"
    assert payload["rows"][0]["broker_settled_quantity"] == 2
    assert payload["rows"][0]["broker_orderable_quantity"] == 4


def test_virtual_sleeve_account_is_not_used_by_backtest_portfolio(tmp_path):
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 999})
    backtest_portfolio = Portfolio(cash=100)

    assert store.current_portfolio("LEaps").cash == 999
    assert backtest_portfolio.cash == 100


def test_virtual_sleeve_account_syncs_broker_cash_to_default_sleeve(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 100_000, "default sleeve": 0},
    )
    store.current_portfolio("LEaps")
    store.current_portfolio("default sleeve")

    report = store.sync_account_cash(
        {
            "cash_balance": 1_000_000,
            "deposit_total_amount": 1_200_000,
        },
        residual_sleeve_id="default sleeve",
    )

    assert report.status == "matched"
    assert report.broker_cash_balance == 1_000_000
    assert report.sleeve_cash["LEaps"] == 100_000
    assert report.sleeve_cash["default sleeve"] == 900_000
    assert store.current_portfolio("default sleeve").cash == 900_000


def test_virtual_sleeve_account_transfers_cash_between_sleeves(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 0, "default sleeve": 1_000_000},
    )
    store.current_portfolio("default sleeve")

    event = store.transfer_cash(
        from_sleeve_id="default sleeve",
        to_sleeve_id="LEaps",
        amount=250_000,
        reason="initial allocation",
    )

    assert event.amount == 250_000
    assert store.current_portfolio("default sleeve").cash == 750_000
    assert store.current_portfolio("LEaps").cash == 250_000


def test_virtual_sleeve_account_keeps_krw_and_usd_cash_separate(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 1_000_000},
        default_currency="KRW",
    )
    domestic = Symbol("005930", "KRX")
    overseas = Symbol("NVDA", "NAS")

    store.apply_fill(
        VirtualFillEvent(
            fill_id="kr-buy",
            order_id="kr-order",
            sleeve_id="LEaps",
            symbol=domestic,
            side=OrderSide.BUY,
            quantity=2,
            fill_price=100_000,
            filled_at=datetime(2026, 5, 10, 9, 1),
        )
    )
    store.sync_account_cash(
        {"cash_balance": 5_000, "currency": "USD"},
        account_id="kis-overseas",
        currency="USD",
        residual_sleeve_id="default sleeve",
    )
    store.transfer_cash(
        from_sleeve_id="default sleeve",
        to_sleeve_id="LEaps",
        amount=1_000,
        account_id="kis-overseas",
        currency="USD",
    )
    store.apply_fill(
        VirtualFillEvent(
            fill_id="us-buy",
            order_id="us-order",
            sleeve_id="LEaps",
            symbol=overseas,
            side=OrderSide.BUY,
            quantity=1,
            fill_price=250,
            filled_at=datetime(2026, 5, 10, 23, 1),
        )
    )

    portfolio = store.current_portfolio("LEaps")

    assert portfolio.cash_by_currency == {"KRW": 800_000.0, "USD": 750.0}
    assert portfolio.cash_for_currency("KRW") == 800_000
    assert portfolio.cash_for_currency("USD") == 750
    assert portfolio.cash == 800_750


def test_virtual_sleeve_account_cash_reconciliation_is_currency_scoped(tmp_path):
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts.json",
        default_cash_by_sleeve={"LEaps": 100_000, "default sleeve": 0},
    )
    store.current_portfolio("LEaps")
    report = store.sync_account_cash(
        {"cash_balance": 10_000, "currency": "USD"},
        account_id="kis-overseas",
        currency="USD",
        residual_sleeve_id="default sleeve",
    )

    assert report.currency == "USD"
    assert report.sleeve_cash["LEaps"] == 0
    assert report.sleeve_cash["default sleeve"] == 10_000
    assert store.current_portfolio("LEaps").cash_by_currency == {"KRW": 100_000.0}
    assert store.current_portfolio("default sleeve").cash_by_currency == {"USD": 10_000.0}

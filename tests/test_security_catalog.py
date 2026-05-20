from datetime import datetime

from leaps_quant_engine.engine_guard import EngineGuard
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.security import SecurityCatalog
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def test_security_catalog_resolves_krx_sor_default_from_universe_metadata():
    symbol = Symbol("005930", "KRX")
    universe = UniverseDefinition(
        id="u",
        market="KRX",
        symbols=(symbol,),
        indicators=(),
        symbol_properties={symbol.key: {"lot_size": 5, "quantity_step": 5}},
    )

    props = SecurityCatalog.from_universe(universe).resolve(symbol)

    assert props.market_scope == "domestic"
    assert props.currency == "KRW"
    assert props.default_exchange_scope == "SOR"
    assert props.lot_size == 5
    assert props.quantity_step == 5


def test_engine_guard_uses_security_catalog_quantity_step_and_sessions(tmp_path):
    symbol = Symbol("005930", "KRX")
    universe = UniverseDefinition(
        id="u",
        market="KRX",
        symbols=(symbol,),
        indicators=(),
        symbol_properties={
            symbol.key: {
                "quantity_step": 5,
                "supported_sessions": ["regular_continuous"],
            }
        },
    )
    account_store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})

    report = EngineGuard().evaluate(
        batches=(
            OrderIntentBatch(
                "LEaps",
                datetime(2026, 5, 14, 9, 0),
                (OrderIntent("LEaps", symbol, OrderSide.BUY, 3, 70_000),),
            ),
        ),
        account_store=account_store,
        market_scope="domestic",
        require_orderable_session=True,
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="after_hours_close",
            is_orderable=True,
            is_regular_market_open=False,
        ),
        security_catalog=SecurityCatalog.from_universe(universe),
    )

    assert report.blocked is True
    assert "order_quantity_not_on_symbol_step" in report.errors
    assert "unsupported_symbol_session_phase" in report.errors

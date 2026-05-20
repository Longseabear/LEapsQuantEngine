from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from leaps_quant_engine.broker_routing import currency_for_symbol, market_scope_for_symbol
from leaps_quant_engine.market_rules import (
    DOMESTIC_BROKER_ENGINE_SUPPORTED_PHASES,
    OVERSEAS_BROKER_ENGINE_SUPPORTED_PHASES,
    BrokerRouteCapability,
    default_capability_for_market_scope,
)
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(frozen=True, slots=True)
class SymbolProperties:
    symbol: Symbol
    market_scope: str
    currency: str
    lot_size: int = 1
    quantity_step: int = 1
    tick_rule: str = "default"
    default_exchange_scope: str = "SOR"
    supported_sessions: tuple[str, ...] = ()
    overseas_order_exchange: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.key,
            "market_scope": self.market_scope,
            "currency": self.currency,
            "lot_size": self.lot_size,
            "quantity_step": self.quantity_step,
            "tick_rule": self.tick_rule,
            "default_exchange_scope": self.default_exchange_scope,
            "supported_sessions": list(self.supported_sessions),
            "overseas_order_exchange": self.overseas_order_exchange,
            "metadata": dict(self.metadata),
        }

    @property
    def broker_capability(self) -> BrokerRouteCapability:
        base = default_capability_for_market_scope(self.market_scope)
        return BrokerRouteCapability(
            market_scope=base.market_scope,
            fractional_quantity=base.fractional_quantity,
            min_quantity=max(int(self.lot_size or 1), base.min_quantity),
            supports_market_order=base.supports_market_order,
            supports_limit_order=base.supports_limit_order,
            supported_time_in_force=base.supported_time_in_force,
            enforce_tick_size=base.enforce_tick_size,
            supported_live_session_phases=self.supported_sessions or base.supported_live_session_phases,
        )


@dataclass(frozen=True, slots=True)
class SecurityCatalog:
    properties_by_symbol: Mapping[str, SymbolProperties] = field(default_factory=dict)

    @classmethod
    def from_universe(cls, universe: UniverseDefinition) -> "SecurityCatalog":
        return cls({
            symbol.key: symbol_properties_from_metadata(symbol, universe.properties_for(symbol))
            for symbol in universe.symbols
        })

    def get(self, symbol: Symbol | str) -> SymbolProperties | None:
        key = symbol.key if isinstance(symbol, Symbol) else str(symbol)
        return self.properties_by_symbol.get(key)

    def resolve(self, symbol: Symbol) -> SymbolProperties:
        return self.get(symbol) or symbol_properties_from_metadata(symbol, {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_count": len(self.properties_by_symbol),
            "symbols": {
                key: value.to_dict()
                for key, value in sorted(self.properties_by_symbol.items())
            },
        }


def symbol_properties_from_metadata(symbol: Symbol | str, metadata: Mapping[str, Any]) -> SymbolProperties:
    symbol = _coerce_symbol(symbol)
    meta = dict(metadata or {})
    market_scope = str(meta.get("market_scope") or market_scope_for_symbol(symbol)).strip().lower()
    currency = str(meta.get("currency") or currency_for_symbol(symbol)).strip().upper()
    supported_sessions = tuple(
        str(item).strip()
        for item in _as_tuple(meta.get("supported_sessions", _default_sessions_for_scope(market_scope)))
        if str(item).strip()
    )
    default_exchange_scope = str(meta.get("default_exchange_scope") or meta.get("exchange_scope") or "").strip().upper()
    if not default_exchange_scope:
        default_exchange_scope = "SOR" if market_scope == "domestic" else ""
    return SymbolProperties(
        symbol=symbol,
        market_scope=market_scope,
        currency=currency,
        lot_size=max(int(meta.get("lot_size", 1) or 1), 1),
        quantity_step=max(int(meta.get("quantity_step", meta.get("lot_size", 1)) or 1), 1),
        tick_rule=str(meta.get("tick_rule") or ("krx" if market_scope == "domestic" else "us")).strip().lower(),
        default_exchange_scope=default_exchange_scope,
        supported_sessions=supported_sessions,
        overseas_order_exchange=str(meta.get("order_exchange") or meta.get("exchange") or "").strip().upper(),
        metadata=meta,
    )


def _default_sessions_for_scope(market_scope: str) -> tuple[str, ...]:
    if market_scope == "overseas":
        return OVERSEAS_BROKER_ENGINE_SUPPORTED_PHASES
    return DOMESTIC_BROKER_ENGINE_SUPPORTED_PHASES


def _coerce_symbol(symbol: Symbol | str) -> Symbol:
    if isinstance(symbol, Symbol):
        return symbol
    text = str(symbol)
    if ":" in text:
        market, ticker = text.split(":", 1)
        return Symbol(ticker=ticker, market=market)
    return Symbol(ticker=text)


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return (value,)

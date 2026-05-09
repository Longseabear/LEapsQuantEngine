from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from leaps_quant_engine.broker_routing import currency_for_market_scope, market_scope_for_symbol
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.orders import OrderTicketStatus
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


@dataclass(frozen=True, slots=True)
class EngineGuardDecision:
    status: str
    reason: str
    sleeve_id: str
    symbol: str = ""
    order_side: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol,
            "order_side": self.order_side,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class EngineGuardReport:
    generated_at: datetime
    account_id: str | None
    market_scope: str | None
    decisions: tuple[EngineGuardDecision, ...]

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(decision.reason for decision in self.decisions if decision.status == "rejected")

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(decision.reason for decision in self.decisions if decision.status == "warning")

    @property
    def blocked(self) -> bool:
        return bool(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "account_id": self.account_id,
            "market_scope": self.market_scope,
            "blocked": self.blocked,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class EngineGuard:
    """Always-on order safety guard outside strategy risk models."""

    def evaluate(
        self,
        *,
        batches: Iterable[OrderIntentBatch],
        account_store: VirtualSleeveAccountStore,
        order_state_store: OrderRuntimeStateStore | None = None,
        account_id: str | None = None,
        market_scope: str | None = None,
        broker: str = "paper",
        commit: bool = False,
        require_account_route: bool = False,
        generated_at: datetime | None = None,
    ) -> EngineGuardReport:
        generated_at = generated_at or datetime.now()
        decisions: list[EngineGuardDecision] = []
        batches_tuple = tuple(batches)
        if require_account_route and not account_id:
            decisions.append(_decision("rejected", "missing_account_route", "", metadata={"market_scope": market_scope}))
        if commit and broker == "broker-engine" and market_scope == "overseas":
            decisions.append(_decision("rejected", "broker_engine_overseas_submit_not_supported", "", metadata={"account_id": account_id}))

        open_buy_notional, open_sell_quantities = _open_ticket_reservations(order_state_store)
        new_buy_notional: dict[str, float] = {}
        new_sell_quantities: dict[tuple[str, str], int] = {}
        for order in _orders(batches_tuple):
            if market_scope is not None and market_scope_for_symbol(order.symbol) != market_scope:
                decisions.append(
                    _decision(
                        "rejected",
                        "account_route_mismatch",
                        order.sleeve_id,
                        order=order,
                        metadata={
                            "account_id": account_id,
                            "route_market_scope": market_scope,
                            "order_market_scope": market_scope_for_symbol(order.symbol),
                        },
                    )
                )
            if order.reference_price <= 0:
                decisions.append(_decision("rejected", "missing_or_invalid_reference_price", order.sleeve_id, order=order))
            if order.quantity <= 0:
                decisions.append(_decision("rejected", "order_quantity_must_be_positive", order.sleeve_id, order=order))

            if order.side is OrderSide.BUY:
                new_buy_notional[order.sleeve_id] = new_buy_notional.get(order.sleeve_id, 0.0) + order.notional
                continue
            key = (order.sleeve_id, order.symbol.key)
            new_sell_quantities[key] = new_sell_quantities.get(key, 0) + order.quantity

        for sleeve_id, notional in new_buy_notional.items():
            portfolio = account_store.current_portfolio(sleeve_id)
            currency = currency_for_market_scope(market_scope)
            reserved_cash = open_buy_notional.get(sleeve_id, 0.0)
            cash = portfolio.cash_for_currency(currency)
            available_cash = cash - reserved_cash
            if notional > available_cash + 1e-9:
                decisions.append(
                    _decision(
                        "rejected",
                        "reserved_cash_exceeded",
                        sleeve_id,
                        metadata={
                            "requested_buy_notional": notional,
                            "reserved_cash": reserved_cash,
                            "available_cash": available_cash,
                            "cash": cash,
                            "currency": currency,
                        },
                    )
                )

        for (sleeve_id, symbol_key), quantity in new_sell_quantities.items():
            portfolio = account_store.current_portfolio(sleeve_id)
            held_quantity = portfolio.holdings.get(symbol_key).quantity if symbol_key in portfolio.holdings else 0
            reserved_quantity = open_sell_quantities.get((sleeve_id, symbol_key), 0)
            available_quantity = held_quantity - reserved_quantity
            if quantity > available_quantity:
                decisions.append(
                    _decision(
                        "rejected",
                        "reserved_sell_quantity_exceeded",
                        sleeve_id,
                        symbol=symbol_key,
                        order_side=OrderSide.SELL.value,
                        metadata={
                            "requested_sell_quantity": quantity,
                            "held_quantity": held_quantity,
                            "reserved_sell_quantity": reserved_quantity,
                            "available_quantity": available_quantity,
                        },
                    )
                )

        return EngineGuardReport(
            generated_at=generated_at,
            account_id=account_id,
            market_scope=market_scope,
            decisions=tuple(decisions),
        )


def _orders(batches: Iterable[OrderIntentBatch]) -> tuple[OrderIntent, ...]:
    return tuple(order for batch in batches for order in batch.order_intents)


def _open_ticket_reservations(
    order_state_store: OrderRuntimeStateStore | None,
) -> tuple[dict[str, float], dict[tuple[str, str], int]]:
    buy_notional: dict[str, float] = {}
    sell_quantities: dict[tuple[str, str], int] = {}
    if order_state_store is None:
        return buy_notional, sell_quantities
    try:
        tickets = order_state_store.snapshot().open_tickets
    except Exception:  # noqa: BLE001
        return buy_notional, sell_quantities
    for ticket in tickets:
        if ticket.status in {OrderTicketStatus.CANCELLED, OrderTicketStatus.FILLED, OrderTicketStatus.REJECTED}:
            continue
        if ticket.side is OrderSide.BUY:
            buy_notional[ticket.sleeve_id] = (
                buy_notional.get(ticket.sleeve_id, 0.0)
                + ticket.remaining_quantity * ticket.reference_price
            )
            continue
        key = (ticket.sleeve_id, ticket.symbol.key)
        sell_quantities[key] = sell_quantities.get(key, 0) + ticket.remaining_quantity
    return buy_notional, sell_quantities


def _decision(
    status: str,
    reason: str,
    sleeve_id: str,
    *,
    order: OrderIntent | None = None,
    symbol: str = "",
    order_side: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> EngineGuardDecision:
    return EngineGuardDecision(
        status=status,
        reason=reason,
        sleeve_id=sleeve_id,
        symbol=order.symbol.key if order is not None else symbol,
        order_side=order.side.value if order is not None else order_side,
        metadata=dict(metadata or {}),
    )

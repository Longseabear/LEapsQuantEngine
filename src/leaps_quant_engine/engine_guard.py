from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from leaps_quant_engine.broker_routing import currency_for_market_scope, market_scope_for_symbol
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.orders import OrderTicketStatus
from leaps_quant_engine.market_rules import (
    MarketSession,
    default_capability_for_market_scope,
    is_domestic_symbol,
    is_valid_krx_tick,
    is_whole_share_quantity,
)
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
        require_orderable_session: bool = False,
        market_session: MarketSession | Mapping[str, Any] | None = None,
        generated_at: datetime | None = None,
    ) -> EngineGuardReport:
        generated_at = generated_at or datetime.now()
        decisions: list[EngineGuardDecision] = []
        batches_tuple = tuple(batches)
        session = _coerce_market_session(market_session)
        capability = default_capability_for_market_scope(market_scope)
        if require_account_route and not account_id:
            decisions.append(_decision("rejected", "missing_account_route", "", metadata={"market_scope": market_scope}))
        if require_orderable_session and session is None:
            decisions.append(_decision("rejected", "missing_market_session", "", metadata={"market_scope": market_scope}))
        if require_orderable_session and session is not None and not session.is_orderable:
            decisions.append(
                _decision(
                    "rejected",
                    "market_session_not_orderable",
                    "",
                    metadata={
                        "market_scope": session.market_scope,
                        "session_phase": session.session_phase,
                        "source": session.source,
                    },
                )
            )
        if commit and broker == "broker-engine" and market_scope == "overseas":
            decisions.append(_decision("rejected", "broker_engine_overseas_submit_not_supported", "", metadata={"account_id": account_id}))

        decisions.extend(_duplicate_submit_decisions(batches_tuple, order_state_store, commit=commit))
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
            if not capability.fractional_quantity and not is_whole_share_quantity(order.quantity):
                decisions.append(_decision("rejected", "order_quantity_must_be_whole_share", order.sleeve_id, order=order))
            if not capability.supports(order_type=order.order_type, time_in_force=order.time_in_force):
                decisions.append(
                    _decision(
                        "rejected",
                        "unsupported_order_style_for_route",
                        order.sleeve_id,
                        order=order,
                        metadata={
                            "order_type": order.order_type.value,
                            "time_in_force": order.time_in_force.value,
                            "market_scope": market_scope,
                        },
                    )
                )
            if order.order_type is OrderType.LIMIT and order.limit_price is not None and order.limit_price <= 0:
                decisions.append(_decision("rejected", "missing_or_invalid_limit_price", order.sleeve_id, order=order))
            if (
                capability.enforce_tick_size
                and order.order_type is OrderType.LIMIT
                and order.limit_price is not None
                and is_domestic_symbol(order.symbol)
                and not is_valid_krx_tick(order.limit_price)
            ):
                decisions.append(
                    _decision(
                        "warning",
                        "limit_price_not_on_krx_tick",
                        order.sleeve_id,
                        order=order,
                        metadata={"limit_price": order.limit_price},
                    )
                )

            if order.side is OrderSide.BUY:
                new_buy_notional[order.sleeve_id] = new_buy_notional.get(order.sleeve_id, 0.0) + _cash_notional(order)
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


def _duplicate_submit_decisions(
    batches: tuple[OrderIntentBatch, ...],
    order_state_store: OrderRuntimeStateStore | None,
    *,
    commit: bool,
) -> tuple[EngineGuardDecision, ...]:
    if order_state_store is None:
        return ()
    try:
        snapshot = order_state_store.snapshot()
    except Exception as exc:  # noqa: BLE001
        status = "rejected" if commit else "warning"
        return (
            _decision(
                status,
                "order_runtime_store_unavailable_for_duplicate_check",
                "",
                metadata={"error": str(exc)},
            ),
        )
    existing_by_intent = {ticket.order_intent_id: ticket for ticket in snapshot.tickets}
    existing_by_ticket = {ticket.ticket_id: ticket for ticket in snapshot.tickets}
    decisions: list[EngineGuardDecision] = []
    status = "rejected" if commit else "warning"
    for batch in batches:
        for index, order in enumerate(batch.order_intents, start=1):
            order_intent_id = f"{batch.batch_id}:{index}"
            ticket_id = f"ticket:{order_intent_id}"
            existing = existing_by_intent.get(order_intent_id) or existing_by_ticket.get(ticket_id)
            if existing is None:
                continue
            decisions.append(
                _decision(
                    status,
                    "duplicate_order_intent_already_recorded",
                    order.sleeve_id,
                    order=order,
                    metadata={
                        "batch_id": batch.batch_id,
                        "order_intent_id": order_intent_id,
                        "ticket_id": ticket_id,
                        "existing_status": existing.status.value,
                        "existing_broker_order_id": existing.broker_order_id,
                    },
                )
            )
    return tuple(decisions)


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
                + ticket.remaining_quantity * _ticket_cash_price(ticket)
            )
            continue
        key = (ticket.sleeve_id, ticket.symbol.key)
        sell_quantities[key] = sell_quantities.get(key, 0) + ticket.remaining_quantity
    return buy_notional, sell_quantities


def _cash_notional(order: OrderIntent) -> float:
    price = order.limit_price if order.order_type is OrderType.LIMIT and order.limit_price is not None else order.reference_price
    return order.quantity * price


def _ticket_cash_price(ticket: Any) -> float:
    price = ticket.limit_price if ticket.order_type is OrderType.LIMIT and ticket.limit_price is not None else ticket.reference_price
    return float(price)


def _coerce_market_session(value: MarketSession | Mapping[str, Any] | None) -> MarketSession | None:
    if value is None:
        return None
    if isinstance(value, MarketSession):
        return value
    return MarketSession.from_mapping(value)


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

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from leaps_quant_engine.broker_routing import currency_for_market_scope, market_scope_for_symbol
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, TimeInForce
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.orders import OrderTicketStatus
from leaps_quant_engine.market_rules import (
    AFTER_HOURS_ORDERABLE_PHASES,
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
        if (
            require_orderable_session
            and broker == "broker-engine"
            and session is not None
            and session.is_orderable
            and session.session_phase not in capability.supported_live_session_phases
        ):
            decisions.append(
                _decision(
                    "rejected",
                    "unsupported_live_session_phase_for_route",
                    "",
                    metadata={
                        "market_scope": session.market_scope,
                        "session_phase": session.session_phase,
                        "supported_live_session_phases": list(capability.supported_live_session_phases),
                        "source": session.source,
                    },
                )
            )
        decisions.extend(_duplicate_submit_decisions(batches_tuple, order_state_store, commit=commit))
        open_buy_notional, open_buy_quantities, open_sell_quantities = _open_ticket_reservations(order_state_store)
        unapplied_fill_deltas = _unapplied_fill_quantity_deltas(order_state_store, account_store)
        decisions.extend(
            _target_delta_decisions(
                _orders(batches_tuple),
                account_store=account_store,
                open_buy_quantities=open_buy_quantities,
                open_sell_quantities=open_sell_quantities,
                unapplied_fill_deltas=unapplied_fill_deltas,
                commit=commit,
            )
        )
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
            if (
                require_orderable_session
                and broker == "broker-engine"
                and session is not None
                and session.market_scope == "domestic"
                and session.session_phase == "after_hours_single_price"
                and not _metadata_bool(order.metadata.get("allow_after_hours_single_price"))
            ):
                decisions.append(
                    _decision(
                        "rejected",
                        "domestic_after_hours_single_price_requires_explicit_symbol_support",
                        order.sleeve_id,
                        order=order,
                        metadata={
                            "market_scope": session.market_scope,
                            "session_phase": session.session_phase,
                            "hint": "KIS can reject NXT-traded symbols in KRX after-hours single-price. Set allow_after_hours_single_price only after symbol/venue support is verified.",
                        },
                    )
                )
            if (
                require_orderable_session
                and broker == "broker-engine"
                and session is not None
                and _is_extended_order_session(session)
                and not _is_limit_day_order(order)
            ):
                decisions.append(
                    _decision(
                        "rejected",
                        "unsupported_extended_session_order_style",
                        order.sleeve_id,
                        order=order,
                        metadata={
                            "market_scope": session.market_scope,
                            "session_phase": session.session_phase,
                            "order_type": order.order_type.value,
                            "time_in_force": order.time_in_force.value,
                            "supported_order_type": OrderType.LIMIT.value,
                            "supported_time_in_force": TimeInForce.DAY.value,
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
) -> tuple[dict[str, float], dict[tuple[str, str], int], dict[tuple[str, str], int]]:
    buy_notional: dict[str, float] = {}
    buy_quantities: dict[tuple[str, str], int] = {}
    sell_quantities: dict[tuple[str, str], int] = {}
    if order_state_store is None:
        return buy_notional, buy_quantities, sell_quantities
    try:
        tickets = order_state_store.snapshot().open_tickets
    except Exception:  # noqa: BLE001
        return buy_notional, buy_quantities, sell_quantities
    for ticket in tickets:
        if ticket.status in {OrderTicketStatus.CANCELLED, OrderTicketStatus.FILLED, OrderTicketStatus.REJECTED}:
            continue
        key = (ticket.sleeve_id, ticket.symbol.key)
        if ticket.side is OrderSide.BUY:
            buy_notional[ticket.sleeve_id] = (
                buy_notional.get(ticket.sleeve_id, 0.0)
                + ticket.remaining_quantity * _ticket_cash_price(ticket)
            )
            buy_quantities[key] = buy_quantities.get(key, 0) + ticket.remaining_quantity
            continue
        sell_quantities[key] = sell_quantities.get(key, 0) + ticket.remaining_quantity
    return buy_notional, buy_quantities, sell_quantities


def _unapplied_fill_quantity_deltas(
    order_state_store: OrderRuntimeStateStore | None,
    account_store: VirtualSleeveAccountStore,
) -> dict[tuple[str, str], int]:
    deltas: dict[tuple[str, str], int] = {}
    if order_state_store is None:
        return deltas
    try:
        fill_events = order_state_store.snapshot().fill_events
    except Exception:  # noqa: BLE001
        return deltas
    for event in fill_events:
        fill_ids = (f"order-event:{event.event_id}", str(event.metadata.get("fill_id") or "").strip())
        try:
            if any(fill_id and account_store.fill_exists(fill_id) for fill_id in fill_ids):
                continue
        except Exception:  # noqa: BLE001
            continue
        signed_quantity = event.quantity if event.side is OrderSide.BUY else -event.quantity
        key = (event.sleeve_id, event.symbol.key)
        deltas[key] = deltas.get(key, 0) + signed_quantity
    return deltas


def _target_delta_decisions(
    orders: tuple[OrderIntent, ...],
    *,
    account_store: VirtualSleeveAccountStore,
    open_buy_quantities: dict[tuple[str, str], int],
    open_sell_quantities: dict[tuple[str, str], int],
    unapplied_fill_deltas: dict[tuple[str, str], int],
    commit: bool,
) -> tuple[EngineGuardDecision, ...]:
    grouped: dict[tuple[str, str], list[OrderIntent]] = {}
    for order in orders:
        if _metadata_int(order.metadata.get("target_quantity")) is None:
            continue
        grouped.setdefault((order.sleeve_id, order.symbol.key), []).append(order)

    decisions: list[EngineGuardDecision] = []
    status = "rejected" if commit else "warning"
    for (sleeve_id, symbol_key), symbol_orders in grouped.items():
        target_quantities = tuple(
            dict.fromkeys(
                quantity
                for quantity in (_metadata_int(order.metadata.get("target_quantity")) for order in symbol_orders)
                if quantity is not None
            )
        )
        first_order = symbol_orders[0]
        if len(target_quantities) != 1:
            decisions.append(
                _decision(
                    status,
                    "multiple_target_quantities_for_symbol",
                    sleeve_id,
                    order=first_order,
                    metadata={"target_quantities": list(target_quantities)},
                )
            )
            continue

        target_quantity = target_quantities[0]
        portfolio = account_store.current_portfolio(sleeve_id)
        held_quantity = portfolio.holdings.get(symbol_key).quantity if symbol_key in portfolio.holdings else 0
        open_buy_quantity = open_buy_quantities.get((sleeve_id, symbol_key), 0)
        open_sell_quantity = open_sell_quantities.get((sleeve_id, symbol_key), 0)
        unapplied_fill_delta = unapplied_fill_deltas.get((sleeve_id, symbol_key), 0)
        projected_quantity = held_quantity + open_buy_quantity - open_sell_quantity + unapplied_fill_delta
        unreserved_delta = target_quantity - projected_quantity
        requested_buy_quantity = sum(order.quantity for order in symbol_orders if order.side is OrderSide.BUY)
        requested_sell_quantity = sum(order.quantity for order in symbol_orders if order.side is OrderSide.SELL)
        requested_delta = requested_buy_quantity - requested_sell_quantity
        metadata = {
            "held_quantity": held_quantity,
            "open_buy_quantity": open_buy_quantity,
            "open_sell_quantity": open_sell_quantity,
            "unapplied_fill_delta": unapplied_fill_delta,
            "projected_quantity": projected_quantity,
            "target_quantity": target_quantity,
            "unreserved_delta": unreserved_delta,
            "requested_buy_quantity": requested_buy_quantity,
            "requested_sell_quantity": requested_sell_quantity,
            "requested_delta": requested_delta,
        }
        if unreserved_delta == 0 and requested_delta != 0:
            decisions.append(
                _decision(
                    status,
                    "target_quantity_already_covered_by_pending_orders",
                    sleeve_id,
                    order=first_order,
                    metadata=metadata,
                )
            )
            continue
        if unreserved_delta > 0:
            if requested_delta <= 0 or requested_sell_quantity:
                decisions.append(
                    _decision(
                        status,
                        "order_side_conflicts_with_unreserved_target_delta",
                        sleeve_id,
                        order=first_order,
                        metadata=metadata,
                    )
                )
                continue
            if requested_delta > unreserved_delta:
                decisions.append(
                    _decision(
                        status,
                        "order_quantity_exceeds_unreserved_target_delta",
                        sleeve_id,
                        order=first_order,
                        metadata=metadata,
                    )
                )
            continue
        if requested_delta >= 0 or requested_buy_quantity:
            decisions.append(
                _decision(
                    status,
                    "order_side_conflicts_with_unreserved_target_delta",
                    sleeve_id,
                    order=first_order,
                    metadata=metadata,
                )
            )
            continue
        if abs(requested_delta) > abs(unreserved_delta):
            decisions.append(
                _decision(
                    status,
                    "order_quantity_exceeds_unreserved_target_delta",
                    sleeve_id,
                    order=first_order,
                    metadata=metadata,
                )
            )
    return tuple(decisions)


def _cash_notional(order: OrderIntent) -> float:
    price = order.limit_price if order.order_type is OrderType.LIMIT and order.limit_price is not None else order.reference_price
    return order.quantity * price


def _ticket_cash_price(ticket: Any) -> float:
    price = ticket.limit_price if ticket.order_type is OrderType.LIMIT and ticket.limit_price is not None else ticket.reference_price
    return float(price)


def _metadata_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _metadata_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _is_extended_order_session(session: MarketSession) -> bool:
    if session.market_scope == "domestic":
        return session.session_phase in AFTER_HOURS_ORDERABLE_PHASES
    if session.market_scope == "overseas":
        return session.session_phase in {"pre_market", "after_market"}
    return False


def _is_limit_day_order(order: OrderIntent) -> bool:
    return order.order_type is OrderType.LIMIT and order.time_in_force is TimeInForce.DAY


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

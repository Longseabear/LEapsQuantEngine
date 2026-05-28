from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import inspect
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.cadence import within_time_window
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, TimeInForce
from leaps_quant_engine.orders import OrderTicket, OrderTicketStatus
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime_state import RuntimeModelStateView, StatePatch


_TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderTicketStatus.FILLED,
        OrderTicketStatus.CANCELLED,
        OrderTicketStatus.EXPIRED,
        OrderTicketStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class PendingOrderState:
    """Immutable open-order facts visible to execution models."""

    open_ticket_count: int = 0
    open_buy_quantities: Mapping[str, int] = field(default_factory=dict)
    open_sell_quantities: Mapping[str, int] = field(default_factory=dict)
    reserved_buy_notional: Mapping[str, float] = field(default_factory=dict)
    ticket_ids_by_symbol: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    latest_status_by_symbol: Mapping[str, str] = field(default_factory=dict)
    oldest_age_seconds_by_symbol: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "open_buy_quantities", MappingProxyType(dict(self.open_buy_quantities)))
        object.__setattr__(self, "open_sell_quantities", MappingProxyType(dict(self.open_sell_quantities)))
        object.__setattr__(self, "reserved_buy_notional", MappingProxyType(dict(self.reserved_buy_notional)))
        object.__setattr__(
            self,
            "ticket_ids_by_symbol",
            MappingProxyType({symbol: tuple(ticket_ids) for symbol, ticket_ids in self.ticket_ids_by_symbol.items()}),
        )
        object.__setattr__(self, "latest_status_by_symbol", MappingProxyType(dict(self.latest_status_by_symbol)))
        object.__setattr__(
            self,
            "oldest_age_seconds_by_symbol",
            MappingProxyType(dict(self.oldest_age_seconds_by_symbol)),
        )

    @classmethod
    def from_order_tickets(
        cls,
        tickets: Iterable[OrderTicket],
        *,
        sleeve_id: str | None = None,
        as_of: datetime | None = None,
    ) -> "PendingOrderState":
        open_buy_quantities: dict[str, int] = {}
        open_sell_quantities: dict[str, int] = {}
        reserved_buy_notional: dict[str, float] = {}
        ticket_ids_by_symbol: dict[str, list[str]] = {}
        latest_status_by_symbol: dict[str, str] = {}
        latest_created_at_by_symbol: dict[str, datetime] = {}
        oldest_created_at_by_symbol: dict[str, datetime] = {}
        open_ticket_count = 0
        for ticket in tickets:
            if sleeve_id is not None and ticket.sleeve_id != sleeve_id:
                continue
            if ticket.status in _TERMINAL_ORDER_STATUSES:
                continue
            remaining_quantity = int(ticket.remaining_quantity)
            if remaining_quantity <= 0:
                continue
            symbol_key = ticket.symbol.key
            open_ticket_count += 1
            ticket_ids_by_symbol.setdefault(symbol_key, []).append(ticket.ticket_id)
            if ticket.side is OrderSide.BUY:
                open_buy_quantities[symbol_key] = open_buy_quantities.get(symbol_key, 0) + remaining_quantity
                reserved_buy_notional[symbol_key] = (
                    reserved_buy_notional.get(symbol_key, 0.0)
                    + remaining_quantity * _ticket_cash_price(ticket)
                )
            else:
                open_sell_quantities[symbol_key] = open_sell_quantities.get(symbol_key, 0) + remaining_quantity
            if (
                symbol_key not in latest_created_at_by_symbol
                or ticket.created_at >= latest_created_at_by_symbol[symbol_key]
            ):
                latest_created_at_by_symbol[symbol_key] = ticket.created_at
                latest_status_by_symbol[symbol_key] = ticket.status.value
            if (
                symbol_key not in oldest_created_at_by_symbol
                or ticket.created_at < oldest_created_at_by_symbol[symbol_key]
            ):
                oldest_created_at_by_symbol[symbol_key] = ticket.created_at

        oldest_age_seconds_by_symbol: dict[str, float] = {}
        if as_of is not None:
            for symbol_key, created_at in oldest_created_at_by_symbol.items():
                oldest_age_seconds_by_symbol[symbol_key] = max(0.0, (as_of - created_at).total_seconds())

        return cls(
            open_ticket_count=open_ticket_count,
            open_buy_quantities=open_buy_quantities,
            open_sell_quantities=open_sell_quantities,
            reserved_buy_notional=reserved_buy_notional,
            ticket_ids_by_symbol={symbol: tuple(ticket_ids) for symbol, ticket_ids in ticket_ids_by_symbol.items()},
            latest_status_by_symbol=latest_status_by_symbol,
            oldest_age_seconds_by_symbol=oldest_age_seconds_by_symbol,
        )

    @property
    def reserved_cash(self) -> float:
        return sum(self.reserved_buy_notional.values())

    def open_buy_quantity(self, symbol: str | Any) -> int:
        return int(self.open_buy_quantities.get(_symbol_key(symbol), 0))

    def open_sell_quantity(self, symbol: str | Any) -> int:
        return int(self.open_sell_quantities.get(_symbol_key(symbol), 0))

    def projected_quantity(self, symbol: str | Any, current_quantity: int) -> int:
        return int(current_quantity) + self.open_buy_quantity(symbol) - self.open_sell_quantity(symbol)

    def unordered_delta(self, symbol: str | Any, *, target_quantity: int, current_quantity: int) -> int:
        return int(target_quantity) - self.projected_quantity(symbol, current_quantity)

    def symbol_metadata(self, symbol: str | Any, *, current_quantity: int, target_quantity: int) -> dict[str, Any]:
        symbol_key = _symbol_key(symbol)
        projected_quantity = self.projected_quantity(symbol, current_quantity)
        return {
            "pending_buy_quantity": self.open_buy_quantity(symbol_key),
            "pending_sell_quantity": self.open_sell_quantity(symbol_key),
            "projected_quantity": projected_quantity,
            "unordered_delta_quantity": int(target_quantity) - projected_quantity,
            "pending_open_ticket_ids": list(self.ticket_ids_by_symbol.get(symbol_key, ())),
            "latest_order_status": self.latest_status_by_symbol.get(symbol_key, ""),
            "oldest_open_order_age_seconds": self.oldest_age_seconds_by_symbol.get(symbol_key),
            "reserved_buy_notional": self.reserved_buy_notional.get(symbol_key, 0.0),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_ticket_count": self.open_ticket_count,
            "reserved_cash": self.reserved_cash,
            "open_buy_quantities": dict(self.open_buy_quantities),
            "open_sell_quantities": dict(self.open_sell_quantities),
            "reserved_buy_notional": dict(self.reserved_buy_notional),
            "ticket_ids_by_symbol": {
                symbol: list(ticket_ids)
                for symbol, ticket_ids in self.ticket_ids_by_symbol.items()
            },
            "latest_status_by_symbol": dict(self.latest_status_by_symbol),
            "oldest_age_seconds_by_symbol": dict(self.oldest_age_seconds_by_symbol),
        }


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    sleeve_id: str
    generated_at: datetime
    portfolio: Portfolio
    data: DataSlice
    approved_targets: tuple[PortfolioTarget, ...]
    market_session: MarketSession | None = None
    market_sessions: Mapping[str, MarketSession] = field(default_factory=dict)
    model_state: RuntimeModelStateView = field(default_factory=RuntimeModelStateView)
    pending_orders: PendingOrderState = field(default_factory=PendingOrderState)
    target_batch_id: str = ""
    source_target_batch_id: str = ""

    def __post_init__(self) -> None:
        sessions = dict(self.market_sessions)
        if self.market_session is not None:
            sessions.setdefault(self.market_session.market_scope, self.market_session)
        object.__setattr__(self, "market_sessions", MappingProxyType(sessions))

    def session_for_symbol(self, symbol: str | Any) -> MarketSession | None:
        return self.market_sessions.get(_market_scope_for_symbol(symbol)) or self.market_session


class ExecutionModel(Protocol):
    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
        execution_context: ExecutionContext | None = None,
        market_session: MarketSession | None = None,
    ) -> list[OrderIntent]:
        """Convert approved portfolio targets into order intents."""


@dataclass(frozen=True, slots=True)
class OrderIntentBatch:
    sleeve_id: str
    generated_at: datetime
    order_intents: tuple[OrderIntent, ...]
    model_name: str = ""
    reason: str = ""
    state_patches: tuple[StatePatch, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    batch_id: str = field(default_factory=lambda: f"order-intents-{uuid4()}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def order_count(self) -> int:
        return len(self.order_intents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "generated_at": self.generated_at.isoformat(),
            "model_name": self.model_name,
            "reason": self.reason,
            "order_count": self.order_count,
            "state_patch_count": len(self.state_patches),
            "orders": [
                {
                    "sleeve_id": order.sleeve_id,
                    "symbol": order.symbol.key,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "reference_price": order.reference_price,
                    "order_type": order.order_type.value,
                    "limit_price": order.limit_price,
                    "time_in_force": order.time_in_force.value,
                    "tag": order.tag,
                    "notional": order.notional,
                    "metadata": dict(order.metadata),
                }
                for order in self.order_intents
            ],
            "state_patches": [patch.to_dict() for patch in self.state_patches],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class StandardExecutionModel:
    """Transforms target quantity deltas into configurable order intents."""

    order_type: OrderType | str = OrderType.LIMIT
    time_in_force: TimeInForce | str = TimeInForce.DAY
    limit_offset_bps: float = 0.0
    max_slice_quantity: int | None = None
    max_slice_notional: float | None = None
    max_slices: int | None = None
    tag_prefix: str = ""
    urgency: str = ""
    max_order_age_seconds: float | None = None
    price_drift_bps: float | None = None
    min_replace_interval_seconds: float | None = None
    max_replacements: int | None = None
    buy_window: str = ""
    sell_window: str = ""
    window_timezone: str = "UTC"

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_type", _coerce_order_type(self.order_type))
        object.__setattr__(self, "time_in_force", _coerce_time_in_force(self.time_in_force))
        object.__setattr__(self, "limit_offset_bps", float(self.limit_offset_bps))
        if self.max_slice_quantity is not None and self.max_slice_quantity <= 0:
            raise ValueError("max_slice_quantity must be positive when provided.")
        if self.max_slice_notional is not None and self.max_slice_notional <= 0:
            raise ValueError("max_slice_notional must be positive when provided.")
        if self.max_slices is not None and self.max_slices <= 0:
            raise ValueError("max_slices must be positive when provided.")
        if self.max_order_age_seconds is not None and self.max_order_age_seconds <= 0:
            raise ValueError("max_order_age_seconds must be positive when provided.")
        if self.price_drift_bps is not None and self.price_drift_bps < 0:
            raise ValueError("price_drift_bps must be non-negative when provided.")
        if self.min_replace_interval_seconds is not None and self.min_replace_interval_seconds < 0:
            raise ValueError("min_replace_interval_seconds must be non-negative when provided.")
        if self.max_replacements is not None and self.max_replacements < 0:
            raise ValueError("max_replacements must be non-negative when provided.")
        if self.buy_window:
            within_time_window(datetime(2000, 1, 3, 12, 0), self.buy_window, timezone=self.window_timezone)
        if self.sell_window:
            within_time_window(datetime(2000, 1, 3, 12, 0), self.sell_window, timezone=self.window_timezone)

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
        execution_context: ExecutionContext | None = None,
        market_session: MarketSession | None = None,
    ) -> list[OrderIntent]:
        orders: list[OrderIntent] = []
        for target in targets:
            current_quantity = portfolio.quantity(target.symbol)
            delta = target.quantity - current_quantity
            if delta == 0:
                continue
            bar = data.get(target.symbol)
            if bar is None:
                continue
            reference_price = float(bar.close)
            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            pending_orders = execution_context.pending_orders if execution_context is not None else PendingOrderState()
            bypass_unordered_quantity = _bypass_unordered_quantity_guard(self, target, delta)
            unordered_delta = (
                delta
                if bypass_unordered_quantity
                else pending_orders.unordered_delta(
                    target.symbol,
                    target_quantity=target.quantity,
                    current_quantity=current_quantity,
                )
            )
            if unordered_delta == 0:
                continue
            unordered_side = OrderSide.BUY if unordered_delta > 0 else OrderSide.SELL
            if unordered_side is not side and not bypass_unordered_quantity:
                continue
            as_of = execution_context.generated_at if execution_context is not None else data.time
            if not self._side_window_allows(unordered_side, as_of):
                continue
            quantities = _split_quantity(
                abs(unordered_delta),
                reference_price=reference_price,
                max_slice_quantity=self.max_slice_quantity,
                max_slice_notional=self.max_slice_notional,
                max_slices=self.max_slices,
            )
            limit_price = _limit_price(
                reference_price,
                side=unordered_side,
                order_type=self.order_type,
                limit_offset_bps=self.limit_offset_bps,
            )
            slice_count = len(quantities)
            pending_metadata = pending_orders.symbol_metadata(
                target.symbol,
                current_quantity=current_quantity,
                target_quantity=target.quantity,
            )
            for index, quantity in enumerate(quantities, start=1):
                orders.append(
                    OrderIntent(
                        sleeve_id=sleeve_id,
                        symbol=target.symbol,
                        side=unordered_side,
                        quantity=quantity,
                        reference_price=reference_price,
                        tag=_tag(self.tag_prefix, target.tag),
                        order_type=self.order_type,
                        limit_price=limit_price,
                        time_in_force=self.time_in_force,
                        metadata={
                            "target_quantity": target.quantity,
                            "current_quantity": current_quantity,
                            "delta_quantity": delta,
                            "parent_quantity": abs(unordered_delta),
                            "raw_delta_quantity": delta,
                            "unordered_delta_quantity": unordered_delta,
                            "unordered_quantity_bypassed": bypass_unordered_quantity,
                            "target_lifecycle": "order_intent_created",
                            "target_batch_id": execution_context.target_batch_id if execution_context else "",
                            "source_target_batch_id": execution_context.source_target_batch_id if execution_context else "",
                            "slice_index": index,
                            "slice_count": slice_count,
                            "limit_offset_bps": self.limit_offset_bps,
                            **pending_metadata,
                        }
                        | _execution_policy_metadata(self),
                    )
                )
        return orders

    def _side_window_allows(self, side: OrderSide, as_of: datetime) -> bool:
        if side is OrderSide.BUY:
            return within_time_window(as_of, self.buy_window, timezone=self.window_timezone)
        return within_time_window(as_of, self.sell_window, timezone=self.window_timezone)


@dataclass(frozen=True, slots=True)
class ImmediateExecutionModel(StandardExecutionModel):
    """Default one-ticket limit execution model."""


@dataclass(frozen=True, slots=True)
class MarketExecutionModel(StandardExecutionModel):
    """Creates market order intents from target deltas."""

    order_type: OrderType | str = OrderType.MARKET


@dataclass(frozen=True, slots=True)
class LimitExecutionModel(StandardExecutionModel):
    """Creates limit order intents from target deltas."""

    order_type: OrderType | str = OrderType.LIMIT


@dataclass(frozen=True, slots=True)
class SlicedExecutionModel(StandardExecutionModel):
    """Creates multiple child order intents when quantity or notional caps are set."""


def _coerce_order_type(value: OrderType | str) -> OrderType:
    if isinstance(value, OrderType):
        return value
    return OrderType(str(value or OrderType.LIMIT.value).strip().lower())


def _coerce_time_in_force(value: TimeInForce | str) -> TimeInForce:
    if isinstance(value, TimeInForce):
        return value
    return TimeInForce(str(value or TimeInForce.DAY.value).strip().lower())


def _limit_price(
    reference_price: float,
    *,
    side: OrderSide,
    order_type: OrderType,
    limit_offset_bps: float,
) -> float | None:
    if order_type is OrderType.MARKET:
        return None
    offset = float(limit_offset_bps) / 10_000.0
    if side is OrderSide.BUY:
        return max(0.0, reference_price * (1.0 + offset))
    return max(0.0, reference_price * (1.0 - offset))


def _split_quantity(
    quantity: int,
    *,
    reference_price: float,
    max_slice_quantity: int | None,
    max_slice_notional: float | None,
    max_slices: int | None,
) -> tuple[int, ...]:
    quantity = int(quantity)
    if quantity <= 0:
        return ()
    slice_cap = quantity
    if max_slice_quantity is not None:
        slice_cap = min(slice_cap, int(max_slice_quantity))
    if max_slice_notional is not None and reference_price > 0:
        notional_cap = max(1, int(float(max_slice_notional) // reference_price))
        slice_cap = min(slice_cap, notional_cap)
    slice_cap = max(1, slice_cap)
    if slice_cap >= quantity and not max_slices:
        return (quantity,)

    chunks: list[int] = []
    remaining = quantity
    while remaining > 0:
        if max_slices is not None and len(chunks) + 1 >= max_slices:
            chunks.append(remaining)
            break
        chunk = min(slice_cap, remaining)
        chunks.append(chunk)
        remaining -= chunk
    return tuple(chunks)


def _execution_policy_metadata(model: StandardExecutionModel) -> dict[str, Any]:
    policy: dict[str, Any] = {}
    urgency = str(model.urgency or "").strip()
    if urgency:
        policy["urgency"] = urgency
    if model.max_order_age_seconds is not None:
        policy["max_order_age_seconds"] = float(model.max_order_age_seconds)
    if model.price_drift_bps is not None:
        policy["price_drift_bps"] = float(model.price_drift_bps)
    if model.min_replace_interval_seconds is not None:
        policy["min_replace_interval_seconds"] = float(model.min_replace_interval_seconds)
    if model.max_replacements is not None:
        policy["max_replacements"] = int(model.max_replacements)
    if model.buy_window:
        policy["buy_window"] = model.buy_window
    if model.sell_window:
        policy["sell_window"] = model.sell_window
    if model.buy_window or model.sell_window:
        policy["window_timezone"] = model.window_timezone
    return {"execution_policy": policy} if policy else {}


def _bypass_unordered_quantity_guard(
    model: StandardExecutionModel,
    target: PortfolioTarget,
    delta: int,
) -> bool:
    if delta >= 0:
        return False
    text = f"{model.urgency} {target.tag}".strip().lower()
    return any(
        token in text
        for token in (
            "hard_exit",
            "urgent",
            "risk_exit",
            "risk-off",
            "risk_off",
            "stop",
            "trailing",
            "force_exit",
            "forced_exit",
        )
    )


def _tag(prefix: str, tag: str) -> str:
    prefix = str(prefix or "").strip()
    tag = str(tag or "").strip()
    if prefix and tag:
        return f"{prefix}:{tag}"
    return prefix or tag


@dataclass(frozen=True, slots=True)
class ExecutionEngine:
    model: ExecutionModel = field(default_factory=ImmediateExecutionModel)
    reason: str = "execution"

    def execute(self, context: ExecutionContext) -> OrderIntentBatch:
        orders = tuple(
            _with_execution_context_metadata(order, context)
            for order in _create_orders(self.model, context)
        )
        state_patches = _state_patches_for_model(self.model, context, orders)
        return OrderIntentBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.generated_at,
            order_intents=orders,
            model_name=type(self.model).__name__,
            reason=self.reason,
            state_patches=state_patches,
            metadata={
                "approved_target_count": len(context.approved_targets),
                "created_order_count": len(orders),
                "target_batch_id": context.target_batch_id,
                "source_target_batch_id": context.source_target_batch_id,
                "pending_orders": context.pending_orders.to_dict(),
                "market_sessions": {
                    market_scope: session.to_dict()
                    for market_scope, session in sorted(context.market_sessions.items())
                },
            },
        )


def _create_orders(model: ExecutionModel, context: ExecutionContext) -> list[OrderIntent]:
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(model.create_orders).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_kwargs or "execution_context" in parameters:
        kwargs["execution_context"] = context
    if supports_kwargs or "market_session" in parameters:
        kwargs["market_session"] = context.market_session
    return model.create_orders(
        context.sleeve_id,
        context.portfolio,
        context.data,
        list(context.approved_targets),
        **kwargs,
    )


def _state_patches_for_model(
    model: ExecutionModel,
    context: ExecutionContext,
    orders: tuple[OrderIntent, ...],
) -> tuple[StatePatch, ...]:
    producer = getattr(model, "state_patches", None)
    if not callable(producer):
        return ()
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(producer).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_kwargs or "context" in parameters:
        kwargs["context"] = context
    if supports_kwargs or "orders" in parameters:
        kwargs["orders"] = orders
    if kwargs:
        result = producer(**kwargs)
    elif not parameters:
        result = producer()
    else:
        result = producer(context, orders)
    patches = tuple(result or ())
    for patch in patches:
        if not isinstance(patch, StatePatch):
            raise TypeError("state_patches(...) must return StatePatch objects.")
    return patches


def _with_execution_context_metadata(order: OrderIntent, context: ExecutionContext) -> OrderIntent:
    session = context.session_for_symbol(order.symbol)
    if session is None:
        return order
    metadata = dict(order.metadata)
    metadata.setdefault("order_session", session.session_phase)
    metadata.setdefault("market_session_phase", session.session_phase)
    metadata.setdefault("market_session_scope", session.market_scope)
    metadata.setdefault("market_session_source", session.source)
    metadata.setdefault("is_regular_market_open", session.is_regular_market_open)
    return replace(order, metadata=metadata)


def _market_scope_for_symbol(symbol: str | Any) -> str:
    key = symbol.key if hasattr(symbol, "key") else str(symbol)
    market = key.split(":", 1)[0].strip().upper() if ":" in key else str(getattr(symbol, "market", "")).strip().upper()
    return "domestic" if market in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"} else "overseas"


def _symbol_key(symbol: str | Any) -> str:
    return symbol.key if hasattr(symbol, "key") else str(symbol)


def _ticket_cash_price(ticket: OrderTicket) -> float:
    if ticket.order_type is OrderType.LIMIT and ticket.limit_price is not None:
        return float(ticket.limit_price)
    return float(ticket.reference_price)

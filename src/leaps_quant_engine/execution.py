from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import inspect
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, TimeInForce
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime_state import RuntimeModelStateView, StatePatch


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
            quantities = _split_quantity(
                abs(delta),
                reference_price=reference_price,
                max_slice_quantity=self.max_slice_quantity,
                max_slice_notional=self.max_slice_notional,
                max_slices=self.max_slices,
            )
            limit_price = _limit_price(
                reference_price,
                side=side,
                order_type=self.order_type,
                limit_offset_bps=self.limit_offset_bps,
            )
            slice_count = len(quantities)
            for index, quantity in enumerate(quantities, start=1):
                orders.append(
                    OrderIntent(
                        sleeve_id=sleeve_id,
                        symbol=target.symbol,
                        side=side,
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
                            "parent_quantity": abs(delta),
                            "slice_index": index,
                            "slice_count": slice_count,
                            "limit_offset_bps": self.limit_offset_bps,
                        },
                    )
                )
        return orders


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

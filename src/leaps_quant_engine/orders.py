from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol, TimeInForce


class OrderIntentBatchLike(Protocol):
    batch_id: str
    order_intents: tuple[OrderIntent, ...]


class OrderTicketStatus(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class OrderEventType(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    event_id: str
    ticket_id: str
    order_intent_id: str
    sleeve_id: str
    symbol: Symbol
    side: OrderSide
    event_type: OrderEventType
    occurred_at: datetime
    quantity: int = 0
    fill_price: float | None = None
    broker_order_id: str | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def notional(self) -> float:
        return self.quantity * (self.fill_price or 0.0)

    @property
    def is_fill(self) -> bool:
        return self.event_type in {OrderEventType.PARTIALLY_FILLED, OrderEventType.FILLED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ticket_id": self.ticket_id,
            "order_intent_id": self.order_intent_id,
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol.key,
            "side": self.side.value,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "notional": self.notional,
            "broker_order_id": self.broker_order_id,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OrderEvent":
        return cls(
            event_id=str(payload["event_id"]),
            ticket_id=str(payload["ticket_id"]),
            order_intent_id=str(payload["order_intent_id"]),
            sleeve_id=str(payload["sleeve_id"]),
            symbol=_symbol_from_key(str(payload["symbol"])),
            side=OrderSide(str(payload["side"])),
            event_type=OrderEventType(str(payload["event_type"])),
            occurred_at=datetime.fromisoformat(str(payload["occurred_at"])),
            quantity=int(payload.get("quantity") or 0),
            fill_price=_float_or_none(payload.get("fill_price")),
            broker_order_id=_text_or_none(payload.get("broker_order_id")),
            reason=str(payload.get("reason") or ""),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class OrderTicket:
    ticket_id: str
    order_intent_id: str
    batch_id: str
    sleeve_id: str
    symbol: Symbol
    side: OrderSide
    quantity: int
    reference_price: float
    tag: str = ""
    order_type: OrderType | str = OrderType.LIMIT
    limit_price: float | None = None
    time_in_force: TimeInForce | str = TimeInForce.DAY
    status: OrderTicketStatus = OrderTicketStatus.CREATED
    created_at: datetime = field(default_factory=datetime.now)
    broker_order_id: str | None = None
    filled_quantity: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        order_type = _coerce_order_type(self.order_type)
        limit_price = None if self.limit_price is None else float(self.limit_price)
        if order_type is OrderType.MARKET:
            limit_price = None
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "time_in_force", _coerce_time_in_force(self.time_in_force))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_intent(
        cls,
        intent: OrderIntent,
        *,
        batch_id: str,
        order_intent_id: str,
        created_at: datetime,
    ) -> "OrderTicket":
        return cls(
            ticket_id=f"ticket:{order_intent_id}",
            order_intent_id=order_intent_id,
            batch_id=batch_id,
            sleeve_id=intent.sleeve_id,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            reference_price=intent.reference_price,
            tag=intent.tag,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            time_in_force=intent.time_in_force,
            metadata=dict(intent.metadata),
            created_at=created_at,
        )

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.quantity - self.filled_quantity)

    def event(
        self,
        event_type: OrderEventType,
        *,
        occurred_at: datetime | None = None,
        quantity: int = 0,
        fill_price: float | None = None,
        broker_order_id: str | None = None,
        reason: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> OrderEvent:
        return OrderEvent(
            event_id=f"order-event-{uuid4()}",
            ticket_id=self.ticket_id,
            order_intent_id=self.order_intent_id,
            sleeve_id=self.sleeve_id,
            symbol=self.symbol,
            side=self.side,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(),
            quantity=quantity,
            fill_price=fill_price,
            broker_order_id=broker_order_id,
            reason=reason,
            metadata=dict(metadata or {}),
        )

    def apply_event(self, event: OrderEvent) -> "OrderTicket":
        if event.ticket_id != self.ticket_id:
            raise ValueError("Order event ticket_id does not match ticket.")
        broker_order_id = event.broker_order_id or self.broker_order_id
        filled_quantity = self.filled_quantity
        status = self.status
        if event.event_type is OrderEventType.SUBMITTED:
            status = OrderTicketStatus.SUBMITTED
        elif event.event_type is OrderEventType.ACCEPTED:
            status = OrderTicketStatus.ACCEPTED
        elif event.event_type is OrderEventType.PARTIALLY_FILLED:
            filled_quantity = min(self.quantity, filled_quantity + event.quantity)
            status = OrderTicketStatus.FILLED if filled_quantity >= self.quantity else OrderTicketStatus.PARTIALLY_FILLED
        elif event.event_type is OrderEventType.FILLED:
            filled_quantity = min(self.quantity, filled_quantity + (event.quantity or self.remaining_quantity))
            status = OrderTicketStatus.FILLED
        elif event.event_type is OrderEventType.CANCEL_REQUESTED:
            status = OrderTicketStatus.CANCEL_REQUESTED
        elif event.event_type is OrderEventType.CANCELLED:
            status = OrderTicketStatus.CANCELLED
        elif event.event_type is OrderEventType.REJECTED:
            status = OrderTicketStatus.REJECTED
        elif event.event_type is OrderEventType.CREATED:
            status = OrderTicketStatus.CREATED
        return replace(
            self,
            status=status,
            broker_order_id=broker_order_id,
            filled_quantity=filled_quantity,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "order_intent_id": self.order_intent_id,
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol.key,
            "side": self.side.value,
            "quantity": self.quantity,
            "reference_price": self.reference_price,
            "tag": self.tag,
            "order_type": self.order_type.value,
            "limit_price": self.limit_price,
            "time_in_force": self.time_in_force.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "broker_order_id": self.broker_order_id,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.remaining_quantity,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "OrderTicket":
        return cls(
            ticket_id=str(payload["ticket_id"]),
            order_intent_id=str(payload["order_intent_id"]),
            batch_id=str(payload["batch_id"]),
            sleeve_id=str(payload["sleeve_id"]),
            symbol=_symbol_from_key(str(payload["symbol"])),
            side=OrderSide(str(payload["side"])),
            quantity=int(payload.get("quantity") or 0),
            reference_price=float(payload.get("reference_price") or 0.0),
            tag=str(payload.get("tag") or ""),
            order_type=_coerce_order_type(payload.get("order_type") or OrderType.LIMIT),
            limit_price=_float_or_none(payload.get("limit_price")),
            time_in_force=_coerce_time_in_force(payload.get("time_in_force") or TimeInForce.DAY),
            status=OrderTicketStatus(str(payload.get("status") or OrderTicketStatus.CREATED.value)),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            broker_order_id=_text_or_none(payload.get("broker_order_id")),
            filled_quantity=int(payload.get("filled_quantity") or 0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class OrderIntentCollision:
    symbol: Symbol
    buy_order_intent_ids: tuple[str, ...]
    sell_order_intent_ids: tuple[str, ...]
    buy_sleeve_ids: tuple[str, ...]
    sell_sleeve_ids: tuple[str, ...]
    reason: str = "same_symbol_opposing_sleeve_intents"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.key,
            "buy_order_intent_ids": list(self.buy_order_intent_ids),
            "sell_order_intent_ids": list(self.sell_order_intent_ids),
            "buy_sleeve_ids": list(self.buy_sleeve_ids),
            "sell_sleeve_ids": list(self.sell_sleeve_ids),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class OrderCoordinationResult:
    generated_at: datetime
    tickets: tuple[OrderTicket, ...]
    events: tuple[OrderEvent, ...]
    collisions: tuple[OrderIntentCollision, ...] = ()

    @property
    def has_collisions(self) -> bool:
        return bool(self.collisions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "ticket_count": len(self.tickets),
            "event_count": len(self.events),
            "collision_count": len(self.collisions),
            "has_collisions": self.has_collisions,
            "tickets": [ticket.to_dict() for ticket in self.tickets],
            "events": [event.to_dict() for event in self.events],
            "collisions": [collision.to_dict() for collision in self.collisions],
        }


class SlippageModel(Protocol):
    def fill_price(self, ticket: OrderTicket) -> float:
        """Return the simulated fill price for a ticket."""


@dataclass(frozen=True, slots=True)
class FeeEstimate:
    total: float = 0.0
    commission: float = 0.0
    taxes: float = 0.0
    regulatory: float = 0.0
    currency: str = ""
    model_name: str = "zero_fee"

    def to_metadata(self) -> dict[str, Any]:
        return {
            "fee": self.total,
            "commission": self.commission,
            "taxes": self.taxes,
            "regulatory_fee": self.regulatory,
            "fee_currency": self.currency,
            "fee_model": self.model_name,
        }


class FeeModel(Protocol):
    def estimate(self, ticket: OrderTicket, *, fill_price: float, quantity: int) -> FeeEstimate:
        """Return simulated transaction costs for a fill."""


@dataclass(frozen=True, slots=True)
class ZeroFeeModel:
    model_name: str = "zero_fee"

    def estimate(self, ticket: OrderTicket, *, fill_price: float, quantity: int) -> FeeEstimate:
        return FeeEstimate(currency=_currency_for_symbol(ticket.symbol), model_name=self.model_name)


@dataclass(frozen=True, slots=True)
class FixedRateFeeModel:
    commission_bps: float = 0.0
    buy_tax_bps: float = 0.0
    sell_tax_bps: float = 0.0
    regulatory_bps: float = 0.0
    minimum_fee: float = 0.0
    model_name: str = "fixed_rate_fee"

    def estimate(self, ticket: OrderTicket, *, fill_price: float, quantity: int) -> FeeEstimate:
        notional = max(float(fill_price) * int(quantity), 0.0)
        commission = _bps_cost(notional, self.commission_bps)
        tax_bps = self.buy_tax_bps if ticket.side is OrderSide.BUY else self.sell_tax_bps
        taxes = _bps_cost(notional, tax_bps)
        regulatory = _bps_cost(notional, self.regulatory_bps)
        total_before_min = commission + taxes + regulatory
        total = max(total_before_min, float(self.minimum_fee)) if total_before_min > 0 else 0.0
        return FeeEstimate(
            total=total,
            commission=commission,
            taxes=taxes,
            regulatory=regulatory,
            currency=_currency_for_symbol(ticket.symbol),
            model_name=self.model_name,
        )


@dataclass(frozen=True, slots=True)
class KisFeeModel:
    """Configurable KIS-style fee preset for simulation only."""

    domestic_commission_bps: float = 1.40527
    domestic_sell_tax_bps: float = 20.0
    overseas_us_commission_bps: float = 25.0
    overseas_us_sell_regulatory_bps: float = 0.0
    model_name: str = "kis_fee"

    def estimate(self, ticket: OrderTicket, *, fill_price: float, quantity: int) -> FeeEstimate:
        market = ticket.symbol.market.upper()
        if market in {"KR", "KRX", "KOSPI", "KOSDAQ"}:
            return FixedRateFeeModel(
                commission_bps=self.domestic_commission_bps,
                sell_tax_bps=self.domestic_sell_tax_bps,
                model_name=self.model_name,
            ).estimate(ticket, fill_price=fill_price, quantity=quantity)
        return FixedRateFeeModel(
            commission_bps=self.overseas_us_commission_bps,
            regulatory_bps=self.overseas_us_sell_regulatory_bps if ticket.side is OrderSide.SELL else 0.0,
            model_name=self.model_name,
        ).estimate(ticket, fill_price=fill_price, quantity=quantity)


@dataclass(frozen=True, slots=True)
class ZeroSlippageModel:
    model_name: str = "zero_slippage"

    def fill_price(self, ticket: OrderTicket) -> float:
        return ticket.reference_price


@dataclass(frozen=True, slots=True)
class FixedBpsSlippageModel:
    bps: float
    model_name: str = "fixed_bps"

    def fill_price(self, ticket: OrderTicket) -> float:
        rate = max(float(self.bps), 0.0) / 10_000.0
        if ticket.side is OrderSide.BUY:
            return ticket.reference_price * (1.0 + rate)
        return max(0.0, ticket.reference_price * (1.0 - rate))


@dataclass(frozen=True, slots=True)
class OrderCoordinator:
    def coordinate(
        self,
        batches: Iterable[OrderIntentBatchLike],
        *,
        generated_at: datetime | None = None,
    ) -> OrderCoordinationResult:
        generated_at = generated_at or datetime.now()
        tickets: list[OrderTicket] = []
        for batch in batches:
            for index, intent in enumerate(batch.order_intents, start=1):
                order_intent_id = f"{batch.batch_id}:{index}"
                tickets.append(
                    OrderTicket.from_intent(
                        intent,
                        batch_id=batch.batch_id,
                        order_intent_id=order_intent_id,
                        created_at=generated_at,
                    )
                )
        events = tuple(ticket.event(OrderEventType.CREATED, occurred_at=generated_at) for ticket in tickets)
        return OrderCoordinationResult(
            generated_at=generated_at,
            tickets=tuple(tickets),
            events=events,
            collisions=_detect_same_symbol_opposing_sleeves(tickets),
        )


@dataclass(frozen=True, slots=True)
class SimulatedFillModel:
    slippage_model: SlippageModel = field(default_factory=ZeroSlippageModel)
    fee_model: FeeModel = field(default_factory=ZeroFeeModel)
    enforce_limit_price: bool = False

    def fill(
        self,
        tickets: Iterable[OrderTicket],
        *,
        occurred_at: datetime,
    ) -> tuple[OrderEvent, ...]:
        events: list[OrderEvent] = []
        for ticket in tickets:
            if ticket.remaining_quantity <= 0 or ticket.status in {OrderTicketStatus.CANCELLED, OrderTicketStatus.REJECTED}:
                continue
            fill_price = self.slippage_model.fill_price(ticket)
            if self.enforce_limit_price and not _is_marketable(ticket, fill_price):
                continue
            fee = self.fee_model.estimate(ticket, fill_price=fill_price, quantity=ticket.remaining_quantity)
            events.append(
                ticket.event(
                    OrderEventType.FILLED,
                    occurred_at=occurred_at,
                    quantity=ticket.remaining_quantity,
                    fill_price=fill_price,
                    reason="simulated_immediate_fill",
                    metadata={
                        **_slippage_metadata(ticket, fill_price, self.slippage_model),
                        **fee.to_metadata(),
                    },
                )
            )
        return tuple(events)


def _slippage_metadata(ticket: OrderTicket, fill_price: float, model: SlippageModel) -> dict[str, Any]:
    reference_price = ticket.reference_price
    if reference_price <= 0:
        side_adjusted_per_share = 0.0
        slippage_bps = 0.0
    elif ticket.side is OrderSide.BUY:
        side_adjusted_per_share = fill_price - reference_price
        slippage_bps = (side_adjusted_per_share / reference_price) * 10_000.0
    else:
        side_adjusted_per_share = reference_price - fill_price
        slippage_bps = (side_adjusted_per_share / reference_price) * 10_000.0
    quantity = ticket.remaining_quantity
    return {
        "reference_price": reference_price,
        "fill_price": fill_price,
        "slippage_model": getattr(model, "model_name", type(model).__name__),
        "slippage_per_share": side_adjusted_per_share,
        "slippage_bps": slippage_bps,
        "slippage_cost": side_adjusted_per_share * quantity,
    }


def _is_marketable(ticket: OrderTicket, fill_price: float) -> bool:
    if ticket.order_type is OrderType.MARKET or ticket.limit_price is None:
        return True
    if ticket.side is OrderSide.BUY:
        return fill_price <= ticket.limit_price + 1e-9
    return fill_price >= ticket.limit_price - 1e-9


def _bps_cost(notional: float, bps: float) -> float:
    return max(float(notional), 0.0) * max(float(bps), 0.0) / 10_000.0


def _currency_for_symbol(symbol: Symbol) -> str:
    return "KRW" if symbol.market.upper() in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"} else "USD"


def _detect_same_symbol_opposing_sleeves(tickets: list[OrderTicket]) -> tuple[OrderIntentCollision, ...]:
    by_symbol: dict[str, list[OrderTicket]] = {}
    for ticket in tickets:
        by_symbol.setdefault(ticket.symbol.key, []).append(ticket)
    collisions: list[OrderIntentCollision] = []
    for symbol_tickets in by_symbol.values():
        buys = [ticket for ticket in symbol_tickets if ticket.side is OrderSide.BUY]
        sells = [ticket for ticket in symbol_tickets if ticket.side is OrderSide.SELL]
        if not buys or not sells:
            continue
        if set(ticket.sleeve_id for ticket in buys).isdisjoint(ticket.sleeve_id for ticket in sells):
            collisions.append(
                OrderIntentCollision(
                    symbol=symbol_tickets[0].symbol,
                    buy_order_intent_ids=tuple(ticket.order_intent_id for ticket in buys),
                    sell_order_intent_ids=tuple(ticket.order_intent_id for ticket in sells),
                    buy_sleeve_ids=tuple(sorted({ticket.sleeve_id for ticket in buys})),
                    sell_sleeve_ids=tuple(sorted({ticket.sleeve_id for ticket in sells})),
                )
            )
    return tuple(collisions)


def _symbol_from_key(symbol_key: str) -> Symbol:
    if ":" not in symbol_key:
        return Symbol(symbol_key)
    market, ticker = symbol_key.split(":", 1)
    return Symbol(ticker=ticker, market=market)


def _coerce_order_type(value: OrderType | str) -> OrderType:
    if isinstance(value, OrderType):
        return value
    return OrderType(str(value or OrderType.LIMIT.value).strip().lower())


def _coerce_time_in_force(value: TimeInForce | str) -> TimeInForce:
    if isinstance(value, TimeInForce):
        return value
    return TimeInForce(str(value or TimeInForce.DAY.value).strip().lower())


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return float(text)


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None

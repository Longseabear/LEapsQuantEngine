from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol

from leaps_quant_engine.models import OrderSide, OrderType, TimeInForce
from leaps_quant_engine.orders import OrderEvent, OrderEventType, OrderTicket, OrderTicketStatus
from leaps_quant_engine.market_rules import (
    DOMESTIC_ORDER_SESSION_TO_DIVISION,
    round_krx_price_to_tick,
    round_overseas_price_to_tick,
)


class BrokerExecutionError(RuntimeError):
    """Raised when a broker gateway cannot accept or reconcile an order ticket."""


class BrokerExecutionGateway(Protocol):
    """Boundary between deterministic order tickets and broker side effects."""

    def submit(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> OrderEvent:
        """Submit one ticket and return the normalized broker/order lifecycle event."""

    def cancel(
        self,
        ticket: OrderTicket,
        *,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> OrderEvent:
        """Request cancellation for one ticket and return the normalized event."""

    def poll(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> tuple[OrderEvent, ...]:
        """Poll broker-side status and return any newly observed lifecycle events."""


@dataclass(frozen=True, slots=True)
class BrokerExecutionResult:
    generated_at: datetime
    tickets: tuple[OrderTicket, ...]
    events: tuple[OrderEvent, ...]

    @property
    def event_count(self) -> int:
        return len(self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "ticket_count": len(self.tickets),
            "event_count": len(self.events),
            "tickets": [ticket.to_dict() for ticket in self.tickets],
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class BrokerExecutionService:
    """Applies broker gateway events to tickets without mutating portfolio state."""

    gateway: BrokerExecutionGateway

    def submit(
        self,
        tickets: Iterable[OrderTicket],
        *,
        occurred_at: datetime | None = None,
    ) -> BrokerExecutionResult:
        generated_at = occurred_at or datetime.now()
        updated_tickets: list[OrderTicket] = []
        events: list[OrderEvent] = []
        for ticket in tickets:
            if ticket.status is not OrderTicketStatus.CREATED:
                updated_tickets.append(ticket)
                continue
            try:
                event = self.gateway.submit(ticket, occurred_at=generated_at)
            except Exception as exc:  # noqa: BLE001
                event = ticket.event(
                    OrderEventType.REJECTED,
                    occurred_at=generated_at,
                    reason=f"broker_submit_failed: {exc}",
                    metadata={
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
            events.append(event)
            updated_tickets.append(ticket.apply_event(event))
        return BrokerExecutionResult(generated_at=generated_at, tickets=tuple(updated_tickets), events=tuple(events))

    def cancel(
        self,
        tickets: Iterable[OrderTicket],
        *,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> BrokerExecutionResult:
        generated_at = occurred_at or datetime.now()
        updated_tickets: list[OrderTicket] = []
        events: list[OrderEvent] = []
        terminal = {
            OrderTicketStatus.CANCELLED,
            OrderTicketStatus.EXPIRED,
            OrderTicketStatus.FILLED,
            OrderTicketStatus.REJECTED,
        }
        for ticket in tickets:
            if ticket.status in terminal:
                updated_tickets.append(ticket)
                continue
            event = self.gateway.cancel(ticket, reason=reason, occurred_at=generated_at)
            events.append(event)
            updated_tickets.append(ticket.apply_event(event))
        return BrokerExecutionResult(generated_at=generated_at, tickets=tuple(updated_tickets), events=tuple(events))

    def poll(
        self,
        tickets: Iterable[OrderTicket],
        *,
        occurred_at: datetime | None = None,
    ) -> BrokerExecutionResult:
        generated_at = occurred_at or datetime.now()
        updated_tickets: list[OrderTicket] = []
        events: list[OrderEvent] = []
        terminal = {
            OrderTicketStatus.CANCELLED,
            OrderTicketStatus.EXPIRED,
            OrderTicketStatus.FILLED,
            OrderTicketStatus.REJECTED,
        }
        for ticket in tickets:
            current = ticket
            if current.status in terminal:
                updated_tickets.append(current)
                continue
            for event in self.gateway.poll(current, occurred_at=generated_at):
                events.append(event)
                current = current.apply_event(event)
            updated_tickets.append(current)
        return BrokerExecutionResult(generated_at=generated_at, tickets=tuple(updated_tickets), events=tuple(events))


@dataclass(frozen=True, slots=True)
class PaperBrokerExecutionGateway:
    """Deterministic broker gateway for paper/live dress rehearsal runs."""

    broker_id_prefix: str = "paper"
    fill_on_poll: bool = True

    def submit(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> OrderEvent:
        return ticket.event(
            OrderEventType.SUBMITTED,
            occurred_at=occurred_at,
            broker_order_id=f"{self.broker_id_prefix}:{ticket.ticket_id}",
            reason="paper_order_submitted",
        )

    def cancel(
        self,
        ticket: OrderTicket,
        *,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> OrderEvent:
        return ticket.event(
            OrderEventType.CANCELLED,
            occurred_at=occurred_at,
            broker_order_id=ticket.broker_order_id,
            reason=reason or "paper_order_cancelled",
        )

    def poll(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> tuple[OrderEvent, ...]:
        if not self.fill_on_poll:
            return ()
        if ticket.status not in {OrderTicketStatus.SUBMITTED, OrderTicketStatus.ACCEPTED}:
            return ()
        if ticket.remaining_quantity <= 0:
            return ()
        return (
            ticket.event(
                OrderEventType.FILLED,
                occurred_at=occurred_at,
                quantity=ticket.remaining_quantity,
                fill_price=ticket.reference_price,
                broker_order_id=ticket.broker_order_id,
                reason="paper_immediate_fill",
            ),
        )


class BrokerEngineCommandClient(Protocol):
    def call_operation(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call one local broker-engine operation synchronously."""


@dataclass(frozen=True, slots=True)
class BrokerEngineExecutionGateway:
    """Gateway for the local StockProgram-style broker-engine boundary."""

    client: BrokerEngineCommandClient
    consumer_id: str = "leaps-quant-engine"
    submit_operation: str = "place_domestic_cash_order"
    cancel_operation: str = "revise_or_cancel_domestic_order"
    order_division: str = "00"
    exchange_scope: str = "KRX"
    use_command_queue: bool = True
    use_hashkey: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def submit(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> OrderEvent:
        overseas_ticket = _is_overseas_ticket(ticket)
        if overseas_ticket:
            operation = "place_overseas_stock_order" if self.submit_operation == "place_domestic_cash_order" else self.submit_operation
            arguments = _overseas_order_arguments(
                ticket,
                order_division=self.order_division,
                use_hashkey=self.use_hashkey,
            )
        else:
            operation = self.submit_operation
            arguments = _domestic_order_arguments(
                ticket,
                order_division=self.order_division,
                exchange_scope=self.exchange_scope,
                use_hashkey=self.use_hashkey,
            )
        metadata = self._command_metadata(ticket, desired_action="submit")
        if self.use_command_queue and _has_method(self.client, "enqueue_command"):
            result = self.client.enqueue_command(  # type: ignore[attr-defined]
                operation,
                arguments=arguments,
                metadata=metadata,
            )
            command_id = _required_text(result, "command_id")
            return ticket.event(
                OrderEventType.SUBMITTED,
                occurred_at=occurred_at,
                broker_order_id=command_id,
                reason="broker_engine_command_enqueued",
                metadata={
                    "submit_mode": "command_queue",
                    "operation": operation,
                    "arguments": arguments,
                    "result": result,
                },
            )

        result = self.client.call_operation(operation, arguments)
        broker_order_id = _broker_order_id_from_result(result)
        return ticket.event(
            OrderEventType.ACCEPTED,
            occurred_at=occurred_at,
            broker_order_id=broker_order_id,
            reason="broker_engine_order_accepted",
            metadata={
                "submit_mode": "call_operation",
                "operation": operation,
                "arguments": arguments,
                "result": result,
            },
        )

    def cancel(
        self,
        ticket: OrderTicket,
        *,
        reason: str = "",
        occurred_at: datetime | None = None,
    ) -> OrderEvent:
        if not ticket.broker_order_id:
            raise BrokerExecutionError("Cannot cancel a ticket without broker_order_id.")
        overseas_ticket = _is_overseas_ticket(ticket)
        if overseas_ticket:
            operation = "revise_or_cancel_overseas_stock_order" if self.cancel_operation == "revise_or_cancel_domestic_order" else self.cancel_operation
            arguments = _overseas_cancel_arguments(
                ticket,
                order_division=self.order_division,
                use_hashkey=self.use_hashkey,
            )
        else:
            operation = self.cancel_operation
            arguments = _domestic_cancel_arguments(
                ticket,
                order_division=self.order_division,
                exchange_scope=self.exchange_scope,
                use_hashkey=self.use_hashkey,
            )
        metadata = self._command_metadata(ticket, desired_action="cancel")
        if self.use_command_queue and _has_method(self.client, "enqueue_command"):
            result = self.client.enqueue_command(  # type: ignore[attr-defined]
                operation,
                arguments=arguments,
                metadata=metadata,
            )
            return ticket.event(
                OrderEventType.CANCEL_REQUESTED,
                occurred_at=occurred_at,
                broker_order_id=ticket.broker_order_id,
                reason=reason or "broker_engine_cancel_enqueued",
                metadata={
                    "submit_mode": "command_queue",
                    "operation": operation,
                    "arguments": arguments,
                    "result": result,
                },
            )

        result = self.client.call_operation(operation, arguments)
        return ticket.event(
            OrderEventType.CANCELLED,
            occurred_at=occurred_at,
            broker_order_id=ticket.broker_order_id,
            reason=reason or "broker_engine_cancelled",
            metadata={
                "submit_mode": "call_operation",
                "operation": operation,
                "arguments": arguments,
                "result": result,
            },
        )

    def poll(self, ticket: OrderTicket, *, occurred_at: datetime | None = None) -> tuple[OrderEvent, ...]:
        if ticket.status is not OrderTicketStatus.SUBMITTED:
            return ()
        if not ticket.broker_order_id or not _has_method(self.client, "get_snapshots"):
            return ()
        snapshots_payload = self.client.get_snapshots(  # type: ignore[attr-defined]
            consumer_id=self.consumer_id,
            snapshot_type="command_status",
            resource_id=ticket.broker_order_id,
            limit=1,
        )
        snapshots = snapshots_payload.get("snapshots", [])
        if not snapshots:
            return ()
        snapshot = dict(snapshots[0])
        payload = dict(snapshot.get("payload") or {})
        status = str(payload.get("status") or "").strip().lower()
        if status in {"", "queued", "running"}:
            return ()
        if status == "failed":
            return (
                ticket.event(
                    OrderEventType.REJECTED,
                    occurred_at=occurred_at,
                    broker_order_id=ticket.broker_order_id,
                    reason=str(payload.get("error") or "broker_command_failed"),
                    metadata={"snapshot": snapshot},
                ),
            )
        if status != "completed":
            return ()
        result = dict(payload.get("result") or {})
        return (
            ticket.event(
                OrderEventType.ACCEPTED,
                occurred_at=occurred_at,
                broker_order_id=_broker_order_id_from_result(result) or ticket.broker_order_id,
                reason="broker_engine_command_completed",
                metadata={"snapshot": snapshot, "result": result},
            ),
        )

    def _command_metadata(self, ticket: OrderTicket, *, desired_action: str) -> dict[str, Any]:
        order_session = _ticket_order_session(ticket)
        order_division = (
            _overseas_order_division(ticket, fallback=self.order_division)
            if _is_overseas_ticket(ticket)
            else _domestic_order_division(ticket, fallback=self.order_division)
        )
        return {
            "consumer_id": self.consumer_id,
            "desired_action": desired_action,
            "plan_id": ticket.batch_id,
            "chain_id": ticket.ticket_id,
            "strategy_leg_id": ticket.sleeve_id,
            "intent_id": ticket.order_intent_id,
            "ticket_id": ticket.ticket_id,
            "sleeve_id": ticket.sleeve_id,
            "symbol": ticket.symbol.key,
            "order_session": order_session,
            "market_session_phase": str(ticket.metadata.get("market_session_phase") or order_session),
            "order_division": order_division,
            **dict(self.metadata),
        }


def _domestic_order_arguments(
    ticket: OrderTicket,
    *,
    order_division: str,
    exchange_scope: str,
    use_hashkey: bool,
) -> dict[str, Any]:
    if ticket.remaining_quantity <= 0:
        raise BrokerExecutionError("Cannot submit a ticket with no remaining quantity.")
    if ticket.symbol.market.upper() not in {"KR", "KRX"}:
        raise BrokerExecutionError("BrokerEngineExecutionGateway currently supports domestic KRX tickets only.")
    return {
        "side": ticket.side.value,
        "symbol": ticket.symbol.ticker,
        "quantity": ticket.remaining_quantity,
        "price": _domestic_order_price(ticket),
        "order_division": _domestic_order_division(ticket, fallback=order_division),
        "exchange_scope": exchange_scope,
        "use_hashkey": use_hashkey,
    }


def _overseas_order_arguments(
    ticket: OrderTicket,
    *,
    order_division: str,
    use_hashkey: bool,
) -> dict[str, Any]:
    if ticket.remaining_quantity <= 0:
        raise BrokerExecutionError("Cannot submit a ticket with no remaining quantity.")
    exchange = _overseas_order_exchange(ticket)
    return {
        "side": ticket.side.value,
        "exchange": exchange,
        "symbol": ticket.symbol.ticker,
        "quantity": ticket.remaining_quantity,
        "price": _overseas_order_price(ticket, exchange=exchange),
        "order_division": _overseas_order_division(ticket, fallback=order_division),
        "use_hashkey": use_hashkey,
    }


def _domestic_cancel_arguments(
    ticket: OrderTicket,
    *,
    order_division: str,
    exchange_scope: str,
    use_hashkey: bool,
) -> dict[str, Any]:
    branch_no, order_no = _split_broker_order_id(ticket.broker_order_id or "")
    if not branch_no:
        raise BrokerExecutionError("Domestic cancel requires a broker_order_id with branch and order number.")
    return {
        "original_branch_no": branch_no,
        "original_order_no": order_no,
        "order_division": _domestic_order_division(ticket, fallback=order_division),
        "rvse_cncl_dvsn_cd": "02",
        "quantity": ticket.remaining_quantity,
        "price": _domestic_order_price(ticket),
        "qty_all_ord_yn": "Y",
        "exchange_scope": exchange_scope,
        "use_hashkey": use_hashkey,
    }


def _overseas_cancel_arguments(
    ticket: OrderTicket,
    *,
    order_division: str,
    use_hashkey: bool,
) -> dict[str, Any]:
    _branch_no, order_no = _split_broker_order_id(ticket.broker_order_id or "")
    exchange = _overseas_order_exchange(ticket)
    return {
        "exchange": exchange,
        "symbol": ticket.symbol.ticker,
        "original_order_no": order_no,
        "rvse_cncl_dvsn_cd": "02",
        "quantity": max(ticket.remaining_quantity, 1),
        "price": _overseas_order_price(ticket, allow_zero=True, exchange=exchange),
        "order_division": _overseas_order_division(ticket, fallback=order_division),
        "use_hashkey": use_hashkey,
    }


_DOMESTIC_ORDER_DIVISION_BY_STYLE = {
    (OrderType.LIMIT, TimeInForce.DAY): "00",
    (OrderType.MARKET, TimeInForce.DAY): "01",
    (OrderType.LIMIT, TimeInForce.IOC): "11",
    (OrderType.LIMIT, TimeInForce.FOK): "12",
    (OrderType.MARKET, TimeInForce.IOC): "13",
    (OrderType.MARKET, TimeInForce.FOK): "14",
}


_OVERSEAS_ORDER_DIVISION_BY_STYLE = {
    (OrderType.LIMIT, TimeInForce.DAY): "00",
    (OrderType.MARKET, TimeInForce.DAY): "01",
}


_COMMON_US_ETF_ORDER_EXCHANGES = {
    "QQQ": "NASD",
    "SMH": "NASD",
    "TLT": "NASD",
    "IEF": "NASD",
    "SPY": "AMEX",
    "IWM": "AMEX",
    "DIA": "AMEX",
    "XLK": "AMEX",
    "XLF": "AMEX",
    "XLV": "AMEX",
    "XLE": "AMEX",
    "XLI": "AMEX",
    "XLP": "AMEX",
    "XLU": "AMEX",
    "USMV": "AMEX",
    "GLD": "AMEX",
}


def _domestic_order_division(ticket: OrderTicket, *, fallback: str) -> str:
    explicit = str(ticket.metadata.get("order_division") or "").strip()
    if explicit:
        return explicit
    session_division = DOMESTIC_ORDER_SESSION_TO_DIVISION.get(_ticket_order_session(ticket))
    if session_division and session_division != "00":
        return session_division
    return _DOMESTIC_ORDER_DIVISION_BY_STYLE.get((ticket.order_type, ticket.time_in_force), fallback)


def _overseas_order_division(ticket: OrderTicket, *, fallback: str) -> str:
    explicit = str(ticket.metadata.get("order_division") or "").strip()
    if explicit:
        return explicit
    return _OVERSEAS_ORDER_DIVISION_BY_STYLE.get((ticket.order_type, ticket.time_in_force), fallback)


def _ticket_order_session(ticket: OrderTicket) -> str:
    return str(ticket.metadata.get("order_session") or ticket.metadata.get("market_session_phase") or "").strip()


def _overseas_order_price(ticket: OrderTicket, *, allow_zero: bool = False, exchange: str | None = None) -> float:
    if ticket.order_type is OrderType.MARKET:
        return 0.0
    price = ticket.limit_price if ticket.limit_price is not None else ticket.reference_price
    if price <= 0 and not allow_zero:
        raise BrokerExecutionError("Overseas limit order price must be greater than zero.")
    return round_overseas_price_to_tick(float(price), side=ticket.side, exchange=exchange)


def _overseas_order_exchange(ticket: OrderTicket) -> str:
    explicit = str(
        ticket.metadata.get("order_exchange")
        or ticket.metadata.get("kis_order_exchange")
        or ticket.metadata.get("exchange")
        or ""
    ).strip().upper()
    if explicit:
        return _normalize_overseas_order_exchange(explicit)
    ticker = ticket.symbol.ticker.upper()
    if ticker in _COMMON_US_ETF_ORDER_EXCHANGES:
        return _COMMON_US_ETF_ORDER_EXCHANGES[ticker]
    market = ticket.symbol.market.upper()
    if market in {"NAS", "NASDAQ", "NASD"}:
        return "NASD"
    if market in {"NYS", "NYSE"}:
        return "NYSE"
    if market in {"AMS", "AMEX", "US"}:
        return "AMEX"
    raise BrokerExecutionError(f"Overseas order exchange is required for {ticket.symbol.key}.")


def _normalize_overseas_order_exchange(value: str) -> str:
    aliases = {
        "NAS": "NASD",
        "NASDAQ": "NASD",
        "NYS": "NYSE",
        "AMS": "AMEX",
        "HKS": "SEHK",
        "SHS": "SHAA",
        "SZS": "SZAA",
    }
    normalized = aliases.get(str(value or "").strip().upper(), str(value or "").strip().upper())
    if normalized not in {"NASD", "NYSE", "AMEX", "SEHK", "SHAA", "SZAA", "TKSE", "HASE", "VNSE"}:
        raise BrokerExecutionError(f"Unsupported overseas order exchange: {value}.")
    return normalized


def _is_overseas_ticket(ticket: OrderTicket) -> bool:
    return ticket.symbol.market.upper() not in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"}


def _domestic_order_price(ticket: OrderTicket) -> int:
    if ticket.order_type is OrderType.MARKET:
        return 0
    price = ticket.limit_price if ticket.limit_price is not None else ticket.reference_price
    return round_krx_price_to_tick(price, side=ticket.side)


def _split_broker_order_id(broker_order_id: str) -> tuple[str, str]:
    text = str(broker_order_id or "").strip()
    if not text:
        raise BrokerExecutionError("broker_order_id is required.")
    if ":" in text:
        branch_no, order_no = text.split(":", 1)
        return branch_no.strip(), order_no.strip()
    return "", text


def _broker_order_id_from_result(result: Mapping[str, Any]) -> str:
    order_no = str(result.get("order_no") or result.get("broker_order_id") or "").strip()
    branch_no = str(result.get("branch_no") or result.get("broker_branch_no") or "").strip()
    if branch_no and order_no:
        return f"{branch_no}:{order_no}"
    return order_no


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    text = str(payload.get(key) or "").strip()
    if not text:
        raise BrokerExecutionError(f"broker-engine result missing '{key}'.")
    return text


def _has_method(value: Any, name: str) -> bool:
    return callable(getattr(value, name, None))

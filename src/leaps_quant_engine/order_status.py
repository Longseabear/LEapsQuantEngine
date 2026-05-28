from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from leaps_quant_engine.models import OrderSide
from leaps_quant_engine.models import OrderType
from leaps_quant_engine.order_state import OrderRuntimeSnapshot, OrderRuntimeStateStore
from leaps_quant_engine.orders import OrderEvent, OrderEventType, OrderTicket, OrderTicketStatus
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.virtual_account import FillAllocationStatus, PortfolioMutationRecord, VirtualSleeveAccountStore


@dataclass(frozen=True, slots=True)
class SleeveOrderRuntimeStatus:
    sleeve_id: str
    portfolio: Portfolio
    open_tickets: tuple[OrderTicket, ...]
    terminal_ticket_count: int
    recent_events: tuple[OrderEvent, ...]
    recent_portfolio_mutations: tuple[PortfolioMutationRecord, ...] = ()

    @property
    def pending_buy_notional(self) -> float:
        return sum(
            ticket.remaining_quantity * _cash_price(ticket)
            for ticket in self.open_tickets
            if ticket.side is OrderSide.BUY
        )

    @property
    def pending_sell_quantities(self) -> dict[str, int]:
        quantities: dict[str, int] = {}
        for ticket in self.open_tickets:
            if ticket.side is not OrderSide.SELL:
                continue
            quantities[ticket.symbol.key] = quantities.get(ticket.symbol.key, 0) + ticket.remaining_quantity
        return quantities

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sleeve_id": self.sleeve_id,
            "portfolio": _portfolio_to_dict(self.portfolio),
            "open_ticket_count": len(self.open_tickets),
            "terminal_ticket_count": self.terminal_ticket_count,
            "pending_buy_notional": self.pending_buy_notional,
            "pending_sell_quantities": self.pending_sell_quantities,
            "recent_event_count": len(self.recent_events),
            "recent_events": [event.to_dict() for event in self.recent_events],
            "recent_portfolio_mutation_count": len(self.recent_portfolio_mutations),
            "recent_portfolio_mutations": [
                mutation.to_dict() for mutation in self.recent_portfolio_mutations
            ],
        }
        if include_details:
            payload["open_tickets"] = [_ticket_to_status_dict(ticket) for ticket in self.open_tickets]
        return payload


@dataclass(frozen=True, slots=True)
class OrderRuntimeStatusReport:
    generated_at: datetime
    runtime_id: str
    order_store_path: Path | None
    account_store_path: Path | None
    broker_account_id: str | None
    market_scope: str | None
    order_snapshot: OrderRuntimeSnapshot
    sleeves: tuple[SleeveOrderRuntimeStatus, ...]
    allocation_statuses: tuple[FillAllocationStatus, ...]
    recent_events: tuple[OrderEvent, ...]
    warnings: tuple[str, ...] = ()
    currency: str = "KRW"

    @property
    def unallocated_fill_count(self) -> int:
        return sum(1 for status in self.allocation_statuses if status.remaining_quantity > 0)

    @property
    def ignored_fill_count(self) -> int:
        return sum(1 for status in self.allocation_statuses if status.status == "ignored")

    @property
    def needs_attention(self) -> bool:
        return bool(self.order_snapshot.open_tickets or self.unallocated_fill_count or self.warnings)

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        allocation_status_counts = _allocation_status_counts(self.allocation_statuses)
        return {
            "generated_at": self.generated_at.isoformat(),
            "runtime_id": self.runtime_id,
            "order_store_path": str(self.order_store_path) if self.order_store_path is not None else None,
            "account_store_path": str(self.account_store_path) if self.account_store_path is not None else None,
            "broker_account_id": self.broker_account_id,
            "market_scope": self.market_scope,
            "currency": self.currency,
            "needs_attention": self.needs_attention,
            "order_runtime": {
                **self.order_snapshot.to_dict(include_details=False),
                "ticket_status_counts": _ticket_status_counts(self.order_snapshot.tickets),
                "event_type_counts": _event_type_counts(self.order_snapshot.events),
                "recent_events": [event.to_dict() for event in self.recent_events],
                "open_tickets": [_ticket_to_status_dict(ticket) for ticket in self.order_snapshot.open_tickets],
                "tickets": [_ticket_to_status_dict(ticket) for ticket in self.order_snapshot.tickets]
                if include_details
                else [],
                "events": [event.to_dict() for event in self.order_snapshot.events] if include_details else [],
            },
            "virtual_account": {
                "raw_broker_fill_count": len(self.allocation_statuses),
                "unallocated_fill_count": self.unallocated_fill_count,
                "ignored_fill_count": self.ignored_fill_count,
                "allocation_status_counts": allocation_status_counts,
                "unallocated_fills": [
                    status.to_dict()
                    for status in self.allocation_statuses
                    if status.remaining_quantity > 0
                ]
                if include_details
                else [],
                "ignored_fills": [
                    status.to_dict()
                    for status in self.allocation_statuses
                    if status.status == "ignored"
                ]
                if include_details
                else [],
            },
            "sleeves": [sleeve.to_dict(include_details=include_details) for sleeve in self.sleeves],
            "warnings": list(self.warnings),
        }


def build_order_runtime_status(
    *,
    runtime_id: str,
    sleeve_ids: tuple[str, ...],
    order_state_store: OrderRuntimeStateStore,
    account_store: VirtualSleeveAccountStore,
    order_store_path: Path | None = None,
    account_store_path: Path | None = None,
    broker_account_id: str | None = None,
    market_scope: str | None = None,
    currency: str = "KRW",
    recent_events: int = 10,
    generated_at: datetime | None = None,
) -> OrderRuntimeStatusReport:
    generated_at = generated_at or datetime.now()
    warnings: list[str] = []
    if order_store_path is not None and not order_store_path.exists():
        warnings.append("order_runtime_store_missing")

    snapshot = order_state_store.snapshot(captured_at=generated_at)
    allocation_statuses = account_store.fill_allocation_statuses()
    sleeve_reports: list[SleeveOrderRuntimeStatus] = []
    for sleeve_id in sleeve_ids:
        portfolio = account_store.current_portfolio(sleeve_id)
        sleeve_tickets = tuple(ticket for ticket in snapshot.tickets if ticket.sleeve_id == sleeve_id)
        sleeve_reports.append(
            SleeveOrderRuntimeStatus(
                sleeve_id=sleeve_id,
                portfolio=portfolio,
                open_tickets=tuple(ticket for ticket in sleeve_tickets if ticket.status not in _TERMINAL_STATUSES),
                terminal_ticket_count=sum(1 for ticket in sleeve_tickets if ticket.status in _TERMINAL_STATUSES),
                recent_events=_recent_order_events(snapshot.events, recent_events, sleeve_id=sleeve_id),
                recent_portfolio_mutations=account_store.portfolio_mutations(
                    sleeve_id=sleeve_id,
                    limit=recent_events,
                ),
            )
        )

    return OrderRuntimeStatusReport(
        generated_at=generated_at,
        runtime_id=runtime_id,
        order_store_path=order_store_path,
        account_store_path=account_store_path,
        broker_account_id=broker_account_id,
        market_scope=market_scope,
        order_snapshot=snapshot,
        sleeves=tuple(sleeve_reports),
        allocation_statuses=allocation_statuses,
        recent_events=_recent_order_events(snapshot.events, recent_events),
        warnings=tuple(warnings),
        currency=currency,
    )


_TERMINAL_STATUSES = frozenset(
    {
        OrderTicketStatus.FILLED,
        OrderTicketStatus.CANCELLED,
        OrderTicketStatus.EXPIRED,
        OrderTicketStatus.REJECTED,
    }
)


def _recent_order_events(
    events: tuple[OrderEvent, ...],
    limit: int,
    *,
    sleeve_id: str | None = None,
) -> tuple[OrderEvent, ...]:
    if limit <= 0:
        return ()
    filtered = tuple(event for event in events if sleeve_id is None or event.sleeve_id == sleeve_id)
    return tuple(
        sorted(
            filtered,
            key=lambda event: (event.occurred_at, event.event_id),
            reverse=True,
        )[:limit]
    )


def _ticket_status_counts(tickets: tuple[OrderTicket, ...]) -> dict[str, int]:
    counts = {status.value: 0 for status in OrderTicketStatus}
    for ticket in tickets:
        counts[ticket.status.value] += 1
    return {status: count for status, count in counts.items() if count}


def _event_type_counts(events: tuple[OrderEvent, ...]) -> dict[str, int]:
    counts = {event_type.value: 0 for event_type in OrderEventType}
    for event in events:
        counts[event.event_type.value] += 1
    return {event_type: count for event_type, count in counts.items() if count}


def _allocation_status_counts(statuses: tuple[FillAllocationStatus, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status in statuses:
        counts[status.status] = counts.get(status.status, 0) + 1
    return counts


def _ticket_to_status_dict(ticket: OrderTicket) -> dict[str, Any]:
    payload = ticket.to_dict()
    payload["remaining_notional"] = ticket.remaining_quantity * _cash_price(ticket)
    return payload


def _cash_price(ticket: OrderTicket) -> float:
    if ticket.order_type is OrderType.LIMIT and ticket.limit_price is not None:
        return ticket.limit_price
    return ticket.reference_price


def _portfolio_to_dict(portfolio: Portfolio) -> dict[str, Any]:
    holdings = sorted(portfolio.holdings.values(), key=lambda holding: holding.symbol.key)
    return {
        "cash": portfolio.cash,
        "cash_by_currency": dict(portfolio.cash_by_currency),
        "holding_count": len(holdings),
        "holdings": [
            {
                "symbol": holding.symbol.ticker,
                "market": holding.symbol.market,
                "quantity": holding.quantity,
                "average_price": holding.average_price,
            }
            for holding in holdings
        ],
    }

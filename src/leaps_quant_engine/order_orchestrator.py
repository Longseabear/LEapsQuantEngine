from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol

from leaps_quant_engine.brokerage import BrokerExecutionResult, BrokerExecutionService
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.orders import (
    OrderCoordinationResult,
    OrderCoordinator,
    OrderEvent,
    OrderEventType,
    OrderTicket,
)
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.portfolio import Portfolio


class OrderAccountStore(Protocol):
    """Virtual account surface needed by live/paper order orchestration."""

    def register_order_ticket(self, ticket: OrderTicket, *, broker_order_id: str = "") -> Any:
        """Persist ticket-to-sleeve ownership before broker submission."""

    def apply_order_event(self, event: OrderEvent) -> Portfolio:
        """Apply broker/order events to the virtual account ledger."""

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        """Return the latest virtual sleeve portfolio."""


@dataclass(frozen=True, slots=True)
class MultiSleeveOrderOrchestrationResult:
    generated_at: datetime
    coordination: OrderCoordinationResult
    submission: BrokerExecutionResult
    polling: BrokerExecutionResult
    applied_event_ids: tuple[str, ...]
    touched_sleeve_ids: tuple[str, ...]
    sleeve_portfolios: dict[str, Portfolio]

    @property
    def final_tickets(self) -> tuple[OrderTicket, ...]:
        return self.polling.tickets

    @property
    def events(self) -> tuple[OrderEvent, ...]:
        return self.coordination.events + self.submission.events + self.polling.events

    @property
    def fill_events(self) -> tuple[OrderEvent, ...]:
        return tuple(event for event in self.events if event.is_fill)

    @property
    def rejected_events(self) -> tuple[OrderEvent, ...]:
        return tuple(event for event in self.events if event.event_type is OrderEventType.REJECTED)

    @property
    def has_collisions(self) -> bool:
        return self.coordination.has_collisions

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "ticket_count": len(self.final_tickets),
            "event_count": len(self.events),
            "fill_event_count": len(self.fill_events),
            "rejected_event_count": len(self.rejected_events),
            "collision_count": len(self.coordination.collisions),
            "has_collisions": self.has_collisions,
            "applied_event_ids": list(self.applied_event_ids),
            "touched_sleeve_ids": list(self.touched_sleeve_ids),
            "coordination": self.coordination.to_dict() if include_details else {
                "ticket_count": len(self.coordination.tickets),
                "event_count": len(self.coordination.events),
                "collision_count": len(self.coordination.collisions),
                "has_collisions": self.has_collisions,
            },
            "submission": self.submission.to_dict() if include_details else {
                "ticket_count": len(self.submission.tickets),
                "event_count": len(self.submission.events),
            },
            "polling": self.polling.to_dict() if include_details else {
                "ticket_count": len(self.polling.tickets),
                "event_count": len(self.polling.events),
            },
            "sleeve_portfolios": {
                sleeve_id: _portfolio_to_dict(portfolio)
                for sleeve_id, portfolio in self.sleeve_portfolios.items()
            },
        }


@dataclass(frozen=True, slots=True)
class MultiSleeveOrderOrchestrator:
    """Coordinates account-level order handling after sleeve execution models run."""

    broker: BrokerExecutionService
    account_store: OrderAccountStore
    coordinator: OrderCoordinator = field(default_factory=OrderCoordinator)
    order_state_store: OrderRuntimeStateStore | None = None
    poll_after_submit: bool = True

    def run_batches(
        self,
        batches: Iterable[OrderIntentBatch],
        *,
        generated_at: datetime | None = None,
        poll_after_submit: bool | None = None,
    ) -> MultiSleeveOrderOrchestrationResult:
        generated_at = generated_at or datetime.now()
        coordination = self.coordinator.coordinate(batches, generated_at=generated_at)
        self._record_tickets(coordination.tickets, recorded_at=generated_at)
        self._record_events(coordination.events, recorded_at=generated_at)
        for ticket in coordination.tickets:
            self.account_store.register_order_ticket(ticket)

        submission = self.broker.submit(coordination.tickets, occurred_at=generated_at)
        self._record_events(submission.events, recorded_at=generated_at)
        applied_event_ids = list(self._apply_events(submission.events))

        should_poll = self.poll_after_submit if poll_after_submit is None else poll_after_submit
        if should_poll:
            polling = self.broker.poll(submission.tickets, occurred_at=generated_at)
            self._record_events(polling.events, recorded_at=generated_at)
            applied_event_ids.extend(self._apply_events(polling.events))
        else:
            polling = BrokerExecutionResult(
                generated_at=generated_at,
                tickets=submission.tickets,
                events=(),
            )

        touched_sleeve_ids = tuple(sorted({ticket.sleeve_id for ticket in polling.tickets}))
        sleeve_portfolios = {
            sleeve_id: self.account_store.current_portfolio(sleeve_id)
            for sleeve_id in touched_sleeve_ids
        }
        return MultiSleeveOrderOrchestrationResult(
            generated_at=generated_at,
            coordination=coordination,
            submission=submission,
            polling=polling,
            applied_event_ids=tuple(applied_event_ids),
            touched_sleeve_ids=touched_sleeve_ids,
            sleeve_portfolios=sleeve_portfolios,
        )

    def run_cycles(
        self,
        cycles: Iterable[Any],
        *,
        generated_at: datetime | None = None,
        poll_after_submit: bool | None = None,
    ) -> MultiSleeveOrderOrchestrationResult:
        return self.run_batches(
            (cycle.execution_batch for cycle in cycles),
            generated_at=generated_at,
            poll_after_submit=poll_after_submit,
        )

    def _apply_events(self, events: Iterable[OrderEvent]) -> tuple[str, ...]:
        applied: list[str] = []
        for event in events:
            self.account_store.apply_order_event(event)
            applied.append(event.event_id)
        return tuple(applied)

    def _record_tickets(self, tickets: Iterable[OrderTicket], *, recorded_at: datetime) -> None:
        if self.order_state_store is None:
            return
        self.order_state_store.record_tickets(tickets, recorded_at=recorded_at)

    def _record_events(self, events: Iterable[OrderEvent], *, recorded_at: datetime) -> None:
        if self.order_state_store is None:
            return
        self.order_state_store.record_events(events, recorded_at=recorded_at)


def _portfolio_to_dict(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "cash": portfolio.cash,
        "cash_by_currency": dict(portfolio.cash_by_currency),
        "holdings": {
            symbol_key: {
                "symbol": holding.symbol.key,
                "quantity": holding.quantity,
                "average_price": holding.average_price,
            }
            for symbol_key, holding in portfolio.holdings.items()
        },
    }

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, Protocol

from leaps_quant_engine.orders import OrderEvent, OrderTicket, OrderTicketStatus


TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderTicketStatus.FILLED,
        OrderTicketStatus.CANCELLED,
        OrderTicketStatus.REJECTED,
    }
)


class OrderRuntimeStateStore(Protocol):
    def record_tickets(self, tickets: Iterable[OrderTicket], *, recorded_at: datetime | None = None) -> None:
        """Persist newly created tickets before broker submission."""

    def record_events(self, events: Iterable[OrderEvent], *, recorded_at: datetime | None = None) -> None:
        """Persist order lifecycle events in append-only order."""

    def snapshot(self, *, captured_at: datetime | None = None) -> "OrderRuntimeSnapshot":
        """Rebuild the current ticket/event view from stored records."""

    def open_tickets(self) -> tuple[OrderTicket, ...]:
        """Return non-terminal tickets that still need broker polling."""


@dataclass(frozen=True, slots=True)
class OrderRuntimeSnapshot:
    captured_at: datetime
    record_count: int
    tickets: tuple[OrderTicket, ...]
    events: tuple[OrderEvent, ...]

    @property
    def open_tickets(self) -> tuple[OrderTicket, ...]:
        return tuple(ticket for ticket in self.tickets if ticket.status not in TERMINAL_ORDER_STATUSES)

    @property
    def terminal_tickets(self) -> tuple[OrderTicket, ...]:
        return tuple(ticket for ticket in self.tickets if ticket.status in TERMINAL_ORDER_STATUSES)

    @property
    def fill_events(self) -> tuple[OrderEvent, ...]:
        return tuple(event for event in self.events if event.is_fill)

    def ticket(self, ticket_id: str) -> OrderTicket | None:
        return next((ticket for ticket in self.tickets if ticket.ticket_id == ticket_id), None)

    def open_tickets_for_sleeve(self, sleeve_id: str) -> tuple[OrderTicket, ...]:
        return tuple(ticket for ticket in self.open_tickets if ticket.sleeve_id == sleeve_id)

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "record_count": self.record_count,
            "ticket_count": len(self.tickets),
            "event_count": len(self.events),
            "open_ticket_count": len(self.open_tickets),
            "terminal_ticket_count": len(self.terminal_tickets),
            "fill_event_count": len(self.fill_events),
            "open_tickets": [ticket.to_dict() for ticket in self.open_tickets],
            "tickets": [ticket.to_dict() for ticket in self.tickets] if include_details else [],
            "events": [event.to_dict() for event in self.events] if include_details else [],
        }


@dataclass(frozen=True, slots=True)
class FileOrderRuntimeStateStore:
    """Append-only JSONL order runtime store for restart-safe ticket polling."""

    path: Path

    def record_ticket(self, ticket: OrderTicket, *, recorded_at: datetime | None = None) -> None:
        self._append_record(
            {
                "record_type": "ticket",
                "recorded_at": (recorded_at or datetime.now()).isoformat(),
                "ticket_id": ticket.ticket_id,
                "payload": ticket.to_dict(),
            }
        )

    def record_tickets(self, tickets: Iterable[OrderTicket], *, recorded_at: datetime | None = None) -> None:
        timestamp = recorded_at or datetime.now()
        for ticket in tickets:
            self.record_ticket(ticket, recorded_at=timestamp)

    def record_event(self, event: OrderEvent, *, recorded_at: datetime | None = None) -> None:
        self._append_record(
            {
                "record_type": "event",
                "recorded_at": (recorded_at or datetime.now()).isoformat(),
                "event_id": event.event_id,
                "ticket_id": event.ticket_id,
                "payload": event.to_dict(),
            }
        )

    def record_events(self, events: Iterable[OrderEvent], *, recorded_at: datetime | None = None) -> None:
        timestamp = recorded_at or datetime.now()
        for event in events:
            self.record_event(event, recorded_at=timestamp)

    def snapshot(self, *, captured_at: datetime | None = None) -> OrderRuntimeSnapshot:
        records = list(self._iter_records())
        base_tickets: dict[str, OrderTicket] = {}
        event_by_id: dict[str, OrderEvent] = {}
        events: list[OrderEvent] = []
        for record in records:
            record_type = str(record.get("record_type") or "")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record_type == "ticket":
                ticket = OrderTicket.from_dict(payload)
                base_tickets[ticket.ticket_id] = ticket
                continue
            if record_type == "event":
                event = OrderEvent.from_dict(payload)
                if event.event_id in event_by_id:
                    continue
                event_by_id[event.event_id] = event
                events.append(event)

        tickets = dict(base_tickets)
        for event in events:
            ticket = tickets.get(event.ticket_id)
            if ticket is None:
                continue
            tickets[event.ticket_id] = ticket.apply_event(event)
        sorted_tickets = tuple(
            sorted(
                tickets.values(),
                key=lambda ticket: (ticket.created_at, ticket.ticket_id),
            )
        )
        return OrderRuntimeSnapshot(
            captured_at=captured_at or datetime.now(),
            record_count=len(records),
            tickets=sorted_tickets,
            events=tuple(events),
        )

    def open_tickets(self) -> tuple[OrderTicket, ...]:
        return self.snapshot().open_tickets

    def ticket(self, ticket_id: str) -> OrderTicket | None:
        return self.snapshot().ticket(ticket_id)

    def _append_record(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _iter_records(self) -> Iterable[dict[str, Any]]:
        if not self.path.exists():
            return ()
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if isinstance(payload, dict):
                    records.append(payload)
        return tuple(records)

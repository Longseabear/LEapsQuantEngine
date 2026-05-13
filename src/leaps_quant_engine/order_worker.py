from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Protocol

from leaps_quant_engine.account_sync import execution_to_virtual_fill
from leaps_quant_engine.brokerage import BrokerExecutionResult, BrokerExecutionService
from leaps_quant_engine.order_orchestrator import OrderAccountStore
from leaps_quant_engine.order_state import OrderRuntimeSnapshot, OrderRuntimeStateStore
from leaps_quant_engine.orders import OrderEvent, OrderEventType, OrderTicket
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.virtual_account import VirtualFillEvent


@dataclass(frozen=True, slots=True)
class OpenTicketPollReport:
    polled_at: datetime
    before: OrderRuntimeSnapshot
    polling: BrokerExecutionResult
    after: OrderRuntimeSnapshot
    applied_event_ids: tuple[str, ...]
    touched_sleeve_ids: tuple[str, ...]
    sleeve_portfolios: dict[str, Portfolio]

    @property
    def polled_ticket_count(self) -> int:
        return len(self.polling.tickets)

    @property
    def event_count(self) -> int:
        return len(self.polling.events)

    @property
    def fill_event_count(self) -> int:
        return sum(1 for event in self.polling.events if event.is_fill)

    @property
    def rejected_event_count(self) -> int:
        return sum(1 for event in self.polling.events if event.event_type is OrderEventType.REJECTED)

    @property
    def open_ticket_count_before(self) -> int:
        return len(self.before.open_tickets)

    @property
    def open_ticket_count_after(self) -> int:
        return len(self.after.open_tickets)

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "polled_at": self.polled_at.isoformat(),
            "polled_ticket_count": self.polled_ticket_count,
            "event_count": self.event_count,
            "fill_event_count": self.fill_event_count,
            "rejected_event_count": self.rejected_event_count,
            "open_ticket_count_before": self.open_ticket_count_before,
            "open_ticket_count_after": self.open_ticket_count_after,
            "applied_event_ids": list(self.applied_event_ids),
            "touched_sleeve_ids": list(self.touched_sleeve_ids),
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
class OpenTicketPollWorker:
    """Poll stored open order tickets and apply newly observed broker events."""

    broker: BrokerExecutionService
    order_state_store: OrderRuntimeStateStore
    account_store: OrderAccountStore

    def poll_once(
        self,
        *,
        polled_at: datetime | None = None,
        sleeve_id: str | None = None,
    ) -> OpenTicketPollReport:
        polled_at = polled_at or datetime.now()
        before = self.order_state_store.snapshot(captured_at=polled_at)
        tickets = _filter_tickets(before.open_tickets, sleeve_id=sleeve_id)
        polling = self.broker.poll(tickets, occurred_at=polled_at)
        self.order_state_store.record_events(polling.events, recorded_at=polled_at)
        applied_event_ids = self._apply_events(polling.events)
        after = self.order_state_store.snapshot(captured_at=polled_at)
        touched_sleeve_ids = tuple(
            sorted(
                {
                    ticket.sleeve_id
                    for ticket in tickets
                }
                | {
                    event.sleeve_id
                    for event in polling.events
                }
            )
        )
        sleeve_portfolios = {
            touched_sleeve_id: self.account_store.current_portfolio(touched_sleeve_id)
            for touched_sleeve_id in touched_sleeve_ids
        }
        return OpenTicketPollReport(
            polled_at=polled_at,
            before=before,
            polling=polling,
            after=after,
            applied_event_ids=applied_event_ids,
            touched_sleeve_ids=touched_sleeve_ids,
            sleeve_portfolios=sleeve_portfolios,
        )

    def _apply_events(self, events: Iterable[OrderEvent]) -> tuple[str, ...]:
        applied: list[str] = []
        for event in events:
            self.account_store.apply_order_event(event)
            applied.append(event.event_id)
        return tuple(applied)


def _filter_tickets(tickets: Iterable[OrderTicket], *, sleeve_id: str | None) -> tuple[OrderTicket, ...]:
    if sleeve_id is None:
        return tuple(tickets)
    return tuple(ticket for ticket in tickets if ticket.sleeve_id == sleeve_id)


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


class ExecutionHistoryClient(Protocol):
    def get_execution_history(
        self,
        *,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
    ) -> dict[str, Any]:
        """Return broker execution history through broker-engine."""

    def get_holdings(self, *, market: str = "domestic") -> dict[str, Any]:
        """Return broker holdings through broker-engine."""


class ExecutionReconcileAccountStore(Protocol):
    def fill_exists(self, fill_id: str) -> bool:
        """Return true when a virtual fill id is already applied."""

    def ownership_for_order(self, order_id: str) -> Any | None:
        """Return sleeve ownership by local order id or broker order alias."""

    def apply_fill(self, fill: VirtualFillEvent) -> Portfolio:
        """Apply one broker fill to a sleeve portfolio."""

    def record_broker_fill(self, fill: VirtualFillEvent) -> bool:
        """Record an unknown broker fill for later sleeve allocation."""

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        """Return the current virtual sleeve portfolio."""

    def reconciliation_report(self, broker_holdings: dict[str, Any] | list[dict[str, Any]]) -> Any:
        """Compare broker holdings to aggregate virtual sleeve positions."""


@dataclass(frozen=True, slots=True)
class ExecutionHistoryReconcileReport:
    reconciled_at: datetime
    start_date: str
    end_date: str
    market: str
    side: str
    symbol: str
    execution_count: int
    imported_fill_count: int
    duplicate_fill_count: int
    existing_order_event_fill_count: int
    unallocated_fill_count: int
    skipped_fill_count: int
    truncated: bool
    errors: tuple[str, ...]
    rejected_executions: tuple[dict[str, Any], ...]
    touched_sleeve_ids: tuple[str, ...]
    sleeve_portfolios: dict[str, Portfolio]
    reconciliation: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        if self.errors or self.skipped_fill_count:
            return "warnings"
        return "ok"

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "reconciled_at": self.reconciled_at.isoformat(),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "market": self.market,
            "side": self.side,
            "symbol": self.symbol,
            "execution_count": self.execution_count,
            "imported_fill_count": self.imported_fill_count,
            "duplicate_fill_count": self.duplicate_fill_count,
            "existing_order_event_fill_count": self.existing_order_event_fill_count,
            "unallocated_fill_count": self.unallocated_fill_count,
            "skipped_fill_count": self.skipped_fill_count,
            "truncated": self.truncated,
            "errors": list(self.errors),
            "rejected_executions": list(self.rejected_executions) if include_details else [],
            "touched_sleeve_ids": list(self.touched_sleeve_ids),
            "sleeve_portfolios": {
                sleeve_id: _portfolio_to_dict(portfolio)
                for sleeve_id, portfolio in self.sleeve_portfolios.items()
            },
            "reconciliation": self.reconciliation,
        }


@dataclass(frozen=True, slots=True)
class ExecutionHistoryReconcileWorker:
    """Import broker execution-history fills without blocking on bad rows."""

    account_client: ExecutionHistoryClient
    account_store: ExecutionReconcileAccountStore
    order_state_store: OrderRuntimeStateStore | None = None
    default_max_executions: int = 500

    def reconcile_once(
        self,
        *,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
        assign_unknown_to_sleeve_id: str | None = None,
        record_unknown_fills: bool = True,
        max_executions: int | None = None,
        report_sleeve_ids: tuple[str, ...] = (),
        reconcile_holdings: bool = True,
        reconciled_at: datetime | None = None,
    ) -> ExecutionHistoryReconcileReport:
        reconciled_at = reconciled_at or datetime.now()
        errors: list[str] = []
        rejected: list[dict[str, Any]] = []
        imported = 0
        duplicate = 0
        existing_order_event = 0
        unallocated = 0
        skipped = 0
        touched_sleeves: set[str] = set(report_sleeve_ids)
        executions: tuple[dict[str, Any], ...] = ()
        truncated = False

        try:
            history = self.account_client.get_execution_history(
                start_date=start_date,
                end_date=end_date,
                market=market,
                side=side,
                symbol=symbol,
            )
            executions = tuple(_extract_executions(history))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"execution_history_fetch_failed: {exc}")

        limit = max_executions if max_executions is not None else self.default_max_executions
        if limit >= 0 and len(executions) > limit:
            executions = executions[:limit]
            truncated = True
            errors.append(f"execution_history_truncated_to_{limit}")

        existing_fill_events = self._existing_fill_events()
        for row in executions:
            try:
                fill = execution_to_virtual_fill(
                    row,
                    market=market,
                    assign_unknown_to_sleeve_id=assign_unknown_to_sleeve_id,
                )
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                rejected.append({"reason": str(exc), "execution": row})
                continue

            if self.account_store.fill_exists(fill.fill_id):
                self._record_imported_fill_event(fill)
                duplicate += 1
                continue
            if _matches_existing_order_event_fill(fill, existing_fill_events):
                existing_order_event += 1
                continue

            ownership = self.account_store.ownership_for_order(fill.order_id)
            if ownership is None and not fill.sleeve_id:
                if not record_unknown_fills:
                    skipped += 1
                    rejected.append({"reason": "unknown_order_ownership", "execution": row})
                    continue
                try:
                    if self.account_store.record_broker_fill(fill):
                        unallocated += 1
                    else:
                        duplicate += 1
                except Exception as exc:  # noqa: BLE001
                    skipped += 1
                    rejected.append({"reason": str(exc), "execution": row})
                continue

            sleeve_id = fill.sleeve_id or getattr(ownership, "sleeve_id", "")
            if sleeve_id:
                touched_sleeves.add(str(sleeve_id))
            try:
                self.account_store.apply_fill(fill)
            except Exception as exc:  # noqa: BLE001
                skipped += 1
                rejected.append({"reason": str(exc), "execution": row})
                continue
            self._record_imported_fill_event(fill)
            imported += 1

        reconciliation = None
        if reconcile_holdings:
            try:
                holdings = self.account_client.get_holdings(market=market)
                reconciliation = self.account_store.reconciliation_report(holdings).to_dict()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"holdings_reconciliation_failed: {exc}")

        sleeve_portfolios: dict[str, Portfolio] = {}
        for sleeve_id in sorted(touched_sleeves):
            try:
                sleeve_portfolios[sleeve_id] = self.account_store.current_portfolio(sleeve_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"sleeve_portfolio_report_failed:{sleeve_id}: {exc}")

        return ExecutionHistoryReconcileReport(
            reconciled_at=reconciled_at,
            start_date=start_date,
            end_date=end_date,
            market=market,
            side=side,
            symbol=symbol,
            execution_count=len(executions),
            imported_fill_count=imported,
            duplicate_fill_count=duplicate,
            existing_order_event_fill_count=existing_order_event,
            unallocated_fill_count=unallocated,
            skipped_fill_count=skipped,
            truncated=truncated,
            errors=tuple(errors),
            rejected_executions=tuple(rejected),
            touched_sleeve_ids=tuple(sorted(touched_sleeves)),
            sleeve_portfolios=sleeve_portfolios,
            reconciliation=reconciliation,
        )

    def _existing_fill_events(self) -> tuple[OrderEvent, ...]:
        if self.order_state_store is None:
            return ()
        try:
            return self.order_state_store.snapshot().fill_events
        except Exception:  # noqa: BLE001
            return ()

    def _record_imported_fill_event(self, fill: VirtualFillEvent) -> None:
        if self.order_state_store is None:
            return
        try:
            snapshot = self.order_state_store.snapshot()
        except Exception:  # noqa: BLE001
            return
        if _matches_existing_order_event_fill(fill, snapshot.fill_events):
            return
        ticket = _matching_open_ticket_for_fill(fill, snapshot.open_tickets)
        if ticket is None:
            return
        event_type = (
            OrderEventType.FILLED
            if fill.quantity >= ticket.remaining_quantity
            else OrderEventType.PARTIALLY_FILLED
        )
        event = ticket.event(
            event_type,
            occurred_at=fill.filled_at,
            quantity=fill.quantity,
            fill_price=fill.fill_price,
            broker_order_id=ticket.broker_order_id or fill.broker_order_id,
            reason="execution_history_reconcile_fill",
            metadata={"fill_id": fill.fill_id},
        )
        self.order_state_store.record_event(event, recorded_at=fill.filled_at)


def _extract_executions(payload: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    executions = payload.get("executions", [])
    if not isinstance(executions, list):
        raise ValueError("execution history payload must contain an executions list.")
    return tuple(dict(row) for row in executions if isinstance(row, dict))


def _matches_existing_order_event_fill(fill: VirtualFillEvent, events: Iterable[OrderEvent]) -> bool:
    fill_aliases = _broker_order_aliases(fill.broker_order_id or fill.order_id)
    for event in events:
        if not event.is_fill:
            continue
        if event.symbol != fill.symbol or event.side is not fill.side:
            continue
        if event.quantity != fill.quantity:
            continue
        if event.fill_price is None or abs(event.fill_price - fill.fill_price) > 1e-6:
            continue
        event_aliases = _broker_order_aliases(event.broker_order_id or event.order_intent_id)
        if fill_aliases and event_aliases and fill_aliases.isdisjoint(event_aliases):
            continue
        return True
    return False


def _matching_open_ticket_for_fill(fill: VirtualFillEvent, tickets: Iterable[OrderTicket]) -> OrderTicket | None:
    fill_aliases = _broker_order_aliases(fill.broker_order_id or fill.order_id)
    for ticket in tickets:
        if ticket.symbol != fill.symbol or ticket.side is not fill.side:
            continue
        ticket_aliases = _broker_order_aliases(ticket.broker_order_id or ticket.order_intent_id)
        if fill_aliases and ticket_aliases and not fill_aliases.isdisjoint(ticket_aliases):
            return ticket
        if fill.order_id and fill.order_id == ticket.order_intent_id:
            return ticket
    return None


def _broker_order_aliases(broker_order_id: str) -> set[str]:
    text = str(broker_order_id or "").strip()
    if not text:
        return set()
    aliases = {text}
    for separator in (":", "|"):
        if separator in text:
            parts = [part.strip() for part in text.split(separator) if part.strip()]
            aliases.update(parts)
            if len(parts) >= 2:
                aliases.add(parts[-1])
    return aliases

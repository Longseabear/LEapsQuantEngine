from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.order_status import OrderRuntimeStatusReport, build_order_runtime_status
from leaps_quant_engine.orders import OrderEvent, OrderTicket, OrderTicketStatus
from leaps_quant_engine.order_worker import (
    ExecutionHistoryReconcileReport,
    ExecutionHistoryReconcileWorker,
    OpenTicketPollReport,
    OpenTicketPollWorker,
)
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


@dataclass(frozen=True, slots=True)
class OrderMaintenancePolicy:
    stale_after_seconds: float = 0.0
    cancel_stale: bool = False
    cancel_partially_filled: bool = True


@dataclass(frozen=True, slots=True)
class OrderMaintenanceReport:
    checked_at: datetime
    stale_tickets: tuple[OrderTicket, ...]
    cancel_events: tuple[OrderEvent, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def stale_ticket_count(self) -> int:
        return len(self.stale_tickets)

    @property
    def cancel_event_count(self) -> int:
        return len(self.cancel_events)

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.isoformat(),
            "stale_ticket_count": self.stale_ticket_count,
            "cancel_event_count": self.cancel_event_count,
            "stale_tickets": [ticket.to_dict() for ticket in self.stale_tickets] if include_details else [],
            "cancel_events": [event.to_dict() for event in self.cancel_events] if include_details else [],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class OrderSupervisorRunReport:
    started_at: datetime
    finished_at: datetime
    runtime_id: str
    sleeve_ids: tuple[str, ...]
    poll_enabled: bool
    reconcile_enabled: bool
    poll_reports: tuple[OpenTicketPollReport, ...]
    maintenance_report: OrderMaintenanceReport | None
    reconcile_report: ExecutionHistoryReconcileReport | None
    final_status: OrderRuntimeStatusReport
    errors: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.errors:
            return "warnings"
        if self.reconcile_report is not None and self.reconcile_report.status != "ok":
            return "warnings"
        if self.final_status.needs_attention:
            return "needs_attention"
        return "ok"

    @property
    def poll_event_count(self) -> int:
        return sum(report.event_count for report in self.poll_reports)

    @property
    def poll_fill_event_count(self) -> int:
        return sum(report.fill_event_count for report in self.poll_reports)

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "runtime_id": self.runtime_id,
            "sleeve_ids": list(self.sleeve_ids),
            "poll_enabled": self.poll_enabled,
            "reconcile_enabled": self.reconcile_enabled,
            "poll_report_count": len(self.poll_reports),
            "poll_event_count": self.poll_event_count,
            "poll_fill_event_count": self.poll_fill_event_count,
            "poll_reports": [report.to_dict(include_details=include_details) for report in self.poll_reports],
            "maintenance_report": self.maintenance_report.to_dict(include_details=include_details)
            if self.maintenance_report is not None
            else None,
            "reconcile_report": self.reconcile_report.to_dict(include_details=include_details)
            if self.reconcile_report is not None
            else None,
            "final_status": self.final_status.to_dict(include_details=include_details),
            "errors": list(self.errors),
        }


@dataclass(frozen=True, slots=True)
class OrderRuntimeSupervisor:
    """Bounded order runtime operation for poll, reconcile, and final status."""

    runtime_id: str
    sleeve_ids: tuple[str, ...]
    order_state_store: OrderRuntimeStateStore
    account_store: VirtualSleeveAccountStore
    poll_worker: OpenTicketPollWorker | None = None
    reconcile_worker: ExecutionHistoryReconcileWorker | None = None
    order_store_path: Path | None = None
    account_store_path: Path | None = None
    broker_account_id: str | None = None
    market_scope: str | None = None
    currency: str = "KRW"
    maintenance_policy: OrderMaintenancePolicy = field(default_factory=OrderMaintenancePolicy)

    def run_once(
        self,
        *,
        poll: bool = True,
        reconcile: bool = True,
        start_date: str = "",
        end_date: str = "",
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
        assign_unknown_to_sleeve_id: str | None = None,
        record_unknown_fills: bool = True,
        max_executions: int | None = None,
        reconcile_holdings: bool = True,
        recent_events: int = 10,
        run_at: datetime | None = None,
        initial_errors: Sequence[str] = (),
    ) -> OrderSupervisorRunReport:
        started_at = run_at or datetime.now()
        errors: list[str] = list(initial_errors)
        poll_reports: list[OpenTicketPollReport] = []
        maintenance_report: OrderMaintenanceReport | None = None
        reconcile_report: ExecutionHistoryReconcileReport | None = None

        poll_enabled = poll and self.poll_worker is not None
        if poll and self.poll_worker is None:
            errors.append("poll_requested_without_poll_worker")
        if poll_enabled:
            poll_reports.extend(self._poll_open_tickets(started_at, errors))

        maintenance_report = self._maintain_open_tickets(started_at, errors)

        reconcile_enabled = reconcile and self.reconcile_worker is not None
        if reconcile and self.reconcile_worker is None:
            errors.append("reconcile_requested_without_reconcile_worker")
        if reconcile_enabled:
            try:
                reconcile_report = self.reconcile_worker.reconcile_once(
                    start_date=start_date,
                    end_date=end_date,
                    market=market,
                    side=side,
                    symbol=symbol,
                    assign_unknown_to_sleeve_id=assign_unknown_to_sleeve_id,
                    record_unknown_fills=record_unknown_fills,
                    max_executions=max_executions,
                    report_sleeve_ids=self.sleeve_ids,
                    reconcile_holdings=reconcile_holdings,
                    reconciled_at=started_at,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"reconcile_failed: {exc}")

        finished_at = datetime.now()
        final_status = build_order_runtime_status(
            runtime_id=self.runtime_id,
            sleeve_ids=self.sleeve_ids,
            order_state_store=self.order_state_store,
            account_store=self.account_store,
            order_store_path=self.order_store_path,
            account_store_path=self.account_store_path,
            broker_account_id=self.broker_account_id,
            market_scope=self.market_scope,
            currency=self.currency,
            recent_events=recent_events,
            generated_at=finished_at,
        )
        return OrderSupervisorRunReport(
            started_at=started_at,
            finished_at=finished_at,
            runtime_id=self.runtime_id,
            sleeve_ids=self.sleeve_ids,
            poll_enabled=poll_enabled,
            reconcile_enabled=reconcile_enabled,
            poll_reports=tuple(poll_reports),
            maintenance_report=maintenance_report,
            reconcile_report=reconcile_report,
            final_status=final_status,
            errors=tuple(errors),
        )

    def _poll_open_tickets(self, polled_at: datetime, errors: list[str]) -> tuple[OpenTicketPollReport, ...]:
        if self.poll_worker is None:
            return ()
        poll_sleeve_ids: Sequence[str | None] = self.sleeve_ids or (None,)
        reports: list[OpenTicketPollReport] = []
        for sleeve_id in poll_sleeve_ids:
            try:
                reports.append(self.poll_worker.poll_once(polled_at=polled_at, sleeve_id=sleeve_id))
            except Exception as exc:  # noqa: BLE001
                suffix = f":{sleeve_id}" if sleeve_id else ""
                errors.append(f"poll_failed{suffix}: {exc}")
        return tuple(reports)

    def _maintain_open_tickets(self, checked_at: datetime, errors: list[str]) -> OrderMaintenanceReport | None:
        policy = self.maintenance_policy
        if policy.stale_after_seconds <= 0:
            return None
        snapshot = self.order_state_store.snapshot(captured_at=checked_at)
        stale = tuple(
            ticket for ticket in snapshot.open_tickets
            if _is_stale_ticket(ticket, checked_at, policy=policy)
        )
        if not stale:
            return OrderMaintenanceReport(checked_at=checked_at, stale_tickets=())
        if not policy.cancel_stale:
            return OrderMaintenanceReport(checked_at=checked_at, stale_tickets=stale)
        if self.poll_worker is None:
            warning = "cancel_stale_requested_without_poll_worker"
            errors.append(warning)
            return OrderMaintenanceReport(checked_at=checked_at, stale_tickets=stale, warnings=(warning,))
        try:
            cancel_result = self.poll_worker.broker.cancel(
                stale,
                reason="stale_ticket_cancel",
                occurred_at=checked_at,
            )
            self.order_state_store.record_events(cancel_result.events, recorded_at=checked_at)
            for event in cancel_result.events:
                self.account_store.apply_order_event(event)
            return OrderMaintenanceReport(
                checked_at=checked_at,
                stale_tickets=stale,
                cancel_events=cancel_result.events,
            )
        except Exception as exc:  # noqa: BLE001
            warning = f"cancel_stale_failed: {exc}"
            errors.append(warning)
            return OrderMaintenanceReport(checked_at=checked_at, stale_tickets=stale, warnings=(warning,))


def _is_stale_ticket(ticket: OrderTicket, checked_at: datetime, *, policy: OrderMaintenancePolicy) -> bool:
    if ticket.status is OrderTicketStatus.PARTIALLY_FILLED and not policy.cancel_partially_filled:
        return False
    age = (checked_at - ticket.created_at).total_seconds()
    return age >= policy.stale_after_seconds

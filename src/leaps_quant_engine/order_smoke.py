from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from leaps_quant_engine.brokerage import BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.order_status import OrderRuntimeStatusReport
from leaps_quant_engine.order_submit import OrderRuntimeSubmitReport, OrderRuntimeSubmitter
from leaps_quant_engine.order_supervisor import OrderRuntimeSupervisor, OrderSupervisorRunReport
from leaps_quant_engine.order_worker import OpenTicketPollWorker
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


@dataclass(frozen=True, slots=True)
class OrderRuntimePaperSmokeReport:
    started_at: datetime
    runtime_id: str
    sleeve_ids: tuple[str, ...]
    submit: OrderRuntimeSubmitReport
    supervisor: OrderSupervisorRunReport | None
    final_status: OrderRuntimeStatusReport

    @property
    def status(self) -> str:
        if self.submit.errors:
            return "blocked"
        if self.supervisor is not None and self.supervisor.errors:
            return "warnings"
        return "ok"

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "runtime_id": self.runtime_id,
            "sleeve_ids": list(self.sleeve_ids),
            "submit": self.submit.to_dict(include_details=include_details),
            "supervisor": self.supervisor.to_dict(include_details=include_details)
            if self.supervisor is not None
            else None,
            "final_status": self.final_status.to_dict(include_details=include_details),
        }


@dataclass(frozen=True, slots=True)
class OrderRuntimePaperSmokeRunner:
    """Paper-only smoke path from order-intent artifact to final status."""

    runtime_id: str
    sleeve_ids: tuple[str, ...]
    order_state_store: OrderRuntimeStateStore
    account_store: VirtualSleeveAccountStore
    order_store_path: Path | None = None
    account_store_path: Path | None = None
    broker_account_id: str | None = None
    market_scope: str | None = None
    currency: str = "KRW"

    def run_batches(
        self,
        batches: Iterable[OrderIntentBatch],
        *,
        max_submit_notional: float | None = None,
        allowed_symbols: tuple[str, ...] = (),
        paper_no_fill: bool = False,
        recent_events: int = 10,
        started_at: datetime | None = None,
    ) -> OrderRuntimePaperSmokeReport:
        started_at = started_at or datetime.now()
        batches_tuple = tuple(batches)
        submit_broker = BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=not paper_no_fill))
        submit_report = OrderRuntimeSubmitter(
            runtime_id=self.runtime_id,
            order_state_store=self.order_state_store,
            account_store=self.account_store,
            orchestrator=MultiSleeveOrderOrchestrator(
                broker=submit_broker,
                account_store=self.account_store,
                order_state_store=self.order_state_store,
                poll_after_submit=False,
            ),
            order_store_path=self.order_store_path,
            account_store_path=self.account_store_path,
            broker_account_id=self.broker_account_id,
            market_scope=self.market_scope,
            currency=self.currency,
        ).submit_batches(
            batches_tuple,
            allowed_sleeve_ids=self.sleeve_ids,
            broker="paper",
            commit=True,
            poll_after_submit=False,
            max_submit_notional=max_submit_notional,
            allowed_symbols=allowed_symbols,
            recent_events=recent_events,
            generated_at=started_at,
        )

        supervisor_report = None
        final_status = submit_report.final_status
        if not submit_report.errors:
            supervisor_report = OrderRuntimeSupervisor(
                runtime_id=self.runtime_id,
                sleeve_ids=self.sleeve_ids,
                order_state_store=self.order_state_store,
                account_store=self.account_store,
                poll_worker=OpenTicketPollWorker(
                    broker=BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=not paper_no_fill)),
                    order_state_store=self.order_state_store,
                    account_store=self.account_store,
                ),
                order_store_path=self.order_store_path,
                account_store_path=self.account_store_path,
                broker_account_id=self.broker_account_id,
                market_scope=self.market_scope,
                currency=self.currency,
            ).run_once(
                poll=True,
                reconcile=False,
                recent_events=recent_events,
                run_at=started_at,
            )
            final_status = supervisor_report.final_status

        return OrderRuntimePaperSmokeReport(
            started_at=started_at,
            runtime_id=self.runtime_id,
            sleeve_ids=self.sleeve_ids,
            submit=submit_report,
            supervisor=supervisor_report,
            final_status=final_status,
        )

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from leaps_quant_engine.cycle_journal import CycleJournalEntry, CycleJournalStore
from leaps_quant_engine.order_status import OrderRuntimeStatusReport
from leaps_quant_engine.runtime_integrity import current_engine_source_fingerprint


@dataclass(frozen=True, slots=True)
class RuntimeHealthCheck:
    name: str
    status: str
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeHealthReport:
    generated_at: datetime
    runtime_id: str
    sleeve_ids: tuple[str, ...]
    checks: tuple[RuntimeHealthCheck, ...]

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if "critical" in statuses:
            return "critical"
        if "warning" in statuses:
            return "needs_attention"
        return "ok"

    @property
    def recommended_next_actions(self) -> tuple[str, ...]:
        actions: list[str] = []
        for check in self.checks:
            if check.status == "ok":
                continue
            if check.name in {"open_ticket_age", "open_tickets"}:
                actions.append("run_order_runtime_supervise")
            elif check.name in {"unallocated_fills", "order_runtime_needs_attention"}:
                actions.append("review_virtual_account_allocations")
            elif check.name in {"missing_journal_store", "no_cycle_journal_entries", "last_cycle_age"}:
                actions.append("run_runtime_once_or_check_worker")
            elif check.name == "snapshot_quality":
                actions.append("refresh_snapshot_worker")
            elif check.name == "unsupported_broker_route":
                actions.append("use_paper_or_supported_broker_route")
            elif check.name in {"engine_code_changed_since_last_cycle", "latest_cycle_missing_code_identity"}:
                actions.append("reload_runtime_or_run_preflight")
        return tuple(dict.fromkeys(actions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "runtime_id": self.runtime_id,
            "sleeve_ids": list(self.sleeve_ids),
            "checks": [check.to_dict() for check in self.checks],
            "recommended_next_actions": list(self.recommended_next_actions),
        }


def build_runtime_health_report(
    *,
    runtime_id: str,
    sleeve_ids: tuple[str, ...],
    journal_store: CycleJournalStore | None,
    order_status: OrderRuntimeStatusReport | None = None,
    journal_path: Path | None = None,
    broker: str = "paper",
    max_cycle_age_seconds: float = 300.0,
    max_open_ticket_age_seconds: float = 600.0,
    repeated_error_window: int = 3,
    generated_at: datetime | None = None,
) -> RuntimeHealthReport:
    generated_at = generated_at or datetime.now()
    checks: list[RuntimeHealthCheck] = []
    current_engine_hash = current_engine_source_fingerprint().digest
    latest_by_sleeve: dict[str, CycleJournalEntry | None] = {}
    if journal_path is not None and not journal_path.exists():
        checks.append(RuntimeHealthCheck("missing_journal_store", "warning", metadata={"path": str(journal_path)}))
    for sleeve_id in sleeve_ids:
        latest = journal_store.latest(sleeve_id=sleeve_id) if journal_store is not None else None
        latest_by_sleeve[sleeve_id] = latest
        if latest is None:
            checks.append(RuntimeHealthCheck("no_cycle_journal_entries", "warning", metadata={"sleeve_id": sleeve_id}))
            continue
        age_seconds = max(0.0, (generated_at - latest.generated_at).total_seconds())
        checks.append(
            RuntimeHealthCheck(
                "last_cycle_age",
                "warning" if age_seconds > max_cycle_age_seconds else "ok",
                metadata={
                    "sleeve_id": sleeve_id,
                    "age_seconds": age_seconds,
                    "max_cycle_age_seconds": max_cycle_age_seconds,
                    "entry_id": latest.entry_id,
                },
            )
        )
        if latest.snapshot_status in {"stale", "invalid"}:
            checks.append(
                RuntimeHealthCheck(
                    "snapshot_quality",
                    "critical" if latest.snapshot_status == "invalid" else "warning",
                    reason=str(latest.snapshot_status),
                    metadata={"sleeve_id": sleeve_id, "entry_id": latest.entry_id},
                )
            )
        latest_engine_hash = latest.metadata.get("engine_source_hash")
        if latest_engine_hash:
            if latest_engine_hash != current_engine_hash:
                checks.append(
                    RuntimeHealthCheck(
                        "engine_code_changed_since_last_cycle",
                        "warning",
                        metadata={
                            "sleeve_id": sleeve_id,
                            "latest": latest_engine_hash,
                            "current": current_engine_hash,
                            "entry_id": latest.entry_id,
                        },
                    )
                )
        else:
            checks.append(
                RuntimeHealthCheck(
                    "latest_cycle_missing_code_identity",
                    "warning",
                    metadata={"sleeve_id": sleeve_id, "entry_id": latest.entry_id},
                )
            )
        recent = journal_store.entries(sleeve_id=sleeve_id, limit=repeated_error_window) if journal_store is not None else ()
        if recent and len(recent) >= repeated_error_window and all(entry.is_error for entry in recent):
            checks.append(
                RuntimeHealthCheck(
                    "repeated_cycle_failure",
                    "critical",
                    metadata={"sleeve_id": sleeve_id, "window": repeated_error_window},
                )
            )

    if order_status is not None:
        if order_status.market_scope == "overseas" and broker == "broker-engine":
            checks.append(
                RuntimeHealthCheck(
                    "unsupported_broker_route",
                    "warning",
                    metadata={"broker_account_id": order_status.broker_account_id, "market_scope": order_status.market_scope},
                )
            )
        if order_status.unallocated_fill_count:
            checks.append(
                RuntimeHealthCheck(
                    "unallocated_fills",
                    "warning",
                    metadata={"count": order_status.unallocated_fill_count},
                )
            )
        open_tickets = order_status.order_snapshot.open_tickets
        checks.append(
            RuntimeHealthCheck(
                "open_tickets",
                "warning" if open_tickets else "ok",
                metadata={"count": len(open_tickets)},
            )
        )
        old_tickets = [
            ticket
            for ticket in open_tickets
            if (generated_at - ticket.created_at).total_seconds() > max_open_ticket_age_seconds
        ]
        if old_tickets:
            checks.append(
                RuntimeHealthCheck(
                    "open_ticket_age",
                    "warning",
                    metadata={
                        "count": len(old_tickets),
                        "max_open_ticket_age_seconds": max_open_ticket_age_seconds,
                        "ticket_ids": [ticket.ticket_id for ticket in old_tickets],
                    },
                )
            )
        if order_status.needs_attention:
            checks.append(
                RuntimeHealthCheck(
                    "order_runtime_needs_attention",
                    "warning",
                    metadata={"broker_account_id": order_status.broker_account_id},
                )
            )

    if not checks:
        checks.append(RuntimeHealthCheck("runtime_health_inputs", "ok"))
    return RuntimeHealthReport(
        generated_at=generated_at,
        runtime_id=runtime_id,
        sleeve_ids=sleeve_ids,
        checks=tuple(checks),
    )

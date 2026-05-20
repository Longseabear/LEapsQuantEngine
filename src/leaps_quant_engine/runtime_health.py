from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from leaps_quant_engine.cycle_journal import CycleJournalEntry, CycleJournalStore
from leaps_quant_engine.framework.portfolio_blend import ACTIVE_TRANSITION_NAMESPACE, DEFAULT_PORTFOLIO_BLEND_MODEL_ID
from leaps_quant_engine.kis_gateway import fetch_kis_gateway_health
from leaps_quant_engine.market_calendar import session_report_for_market_scope
from leaps_quant_engine.order_status import OrderRuntimeStatusReport
from leaps_quant_engine.runtime_integrity import current_engine_source_fingerprint
from leaps_quant_engine.runtime_state import RuntimeStateStore


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
            elif check.name in {"engine_code_changed_since_last_cycle", "latest_cycle_missing_code_identity"}:
                actions.append("reload_runtime_or_run_preflight")
            elif check.name in {"portfolio_blend_deadline_overdue", "portfolio_blend_due_for_completion"}:
                actions.append("run_runtime_once_or_check_worker")
            elif check.name in {"portfolio_blend_missing_deadline", "portfolio_blend_invalid_state"}:
                actions.append("reload_runtime_or_run_preflight")
            elif check.name == "kis_gateway_liveness":
                actions.append("start_or_check_kis_gateway")
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
    runtime_state_store: RuntimeStateStore | None = None,
    journal_path: Path | None = None,
    broker: str = "paper",
    max_cycle_age_seconds: float = 300.0,
    max_open_ticket_age_seconds: float = 600.0,
    portfolio_blend_overdue_grace_seconds: float = 300.0,
    kis_gateway_base_url: str | None = None,
    repeated_error_window: int = 3,
    generated_at: datetime | None = None,
) -> RuntimeHealthReport:
    generated_at = generated_at or datetime.now().astimezone()
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
        latest_generated_at, generated_cmp = _same_datetime_kind(latest.generated_at, generated_at)
        age_seconds = max(0.0, (generated_cmp - latest_generated_at).total_seconds())
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

    if runtime_state_store is not None:
        checks.extend(
            _portfolio_blend_state_checks(
                runtime_state_store,
                sleeve_ids=sleeve_ids,
                generated_at=generated_at,
                overdue_grace_seconds=portfolio_blend_overdue_grace_seconds,
            )
        )

    if kis_gateway_base_url:
        checks.append(_kis_gateway_health_check(kis_gateway_base_url))

    if order_status is not None:
        if order_status.market_scope:
            calendar_report = session_report_for_market_scope(order_status.market_scope, now=generated_at)
            checks.append(
                RuntimeHealthCheck(
                    "market_calendar",
                    "warning" if calendar_report.quality.status == "degraded" else "ok",
                    reason=";".join(calendar_report.quality.warnings),
                    metadata=calendar_report.to_dict(),
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


def _kis_gateway_health_check(base_url: str) -> RuntimeHealthCheck:
    try:
        health = fetch_kis_gateway_health(base_url, timeout_seconds=3.0)
    except Exception as exc:  # noqa: BLE001 - health must report without raising.
        return RuntimeHealthCheck(
            "kis_gateway_liveness",
            "critical",
            reason=str(exc),
            metadata={"base_url": base_url},
        )
    status = str(health.get("status") or "").lower()
    return RuntimeHealthCheck(
        "kis_gateway_liveness",
        "ok" if status == "ok" else "warning",
        reason="" if status == "ok" else f"gateway_status={status or 'unknown'}",
        metadata={
            "base_url": base_url,
            "server": health.get("server"),
            "lane": health.get("lane"),
            "counters": health.get("counters"),
        },
    )


def _portfolio_blend_state_checks(
    runtime_state_store: RuntimeStateStore,
    *,
    sleeve_ids: tuple[str, ...],
    generated_at: datetime,
    overdue_grace_seconds: float,
) -> tuple[RuntimeHealthCheck, ...]:
    checks: list[RuntimeHealthCheck] = []
    grace = timedelta(seconds=max(float(overdue_grace_seconds or 0.0), 0.0))
    for sleeve_id in sleeve_ids:
        records = runtime_state_store.entries(
            sleeve_id=sleeve_id,
            model_id=DEFAULT_PORTFOLIO_BLEND_MODEL_ID,
            namespace=ACTIVE_TRANSITION_NAMESPACE,
        )
        for record in records:
            payload = dict(record.value)
            metadata = {
                "sleeve_id": sleeve_id,
                "transition_id": str(payload.get("transition_id") or ""),
                "started_at": payload.get("started_at"),
                "deadline_at": payload.get("deadline_at"),
                "duration_minutes": _safe_float(payload.get("duration_minutes")),
                "elapsed_minutes": _safe_float(payload.get("elapsed_minutes")),
                "from_elapsed_minutes": _safe_float(payload.get("from_elapsed_minutes")),
                "updated_at": record.updated_at.isoformat(),
            }
            deadline = _optional_datetime(payload.get("deadline_at"))
            duration = _safe_float(payload.get("duration_minutes"))
            elapsed = _safe_float(payload.get("elapsed_minutes"))
            if deadline is None:
                checks.append(
                    RuntimeHealthCheck(
                        "portfolio_blend_missing_deadline",
                        "warning",
                        metadata=metadata,
                    )
                )
                continue
            deadline_cmp, generated_cmp = _same_datetime_kind(deadline, generated_at)
            if generated_cmp > deadline_cmp + grace:
                checks.append(
                    RuntimeHealthCheck(
                        "portfolio_blend_deadline_overdue",
                        "warning",
                        metadata={
                            **metadata,
                            "overdue_seconds": (generated_cmp - deadline_cmp).total_seconds(),
                            "grace_seconds": grace.total_seconds(),
                        },
                    )
                )
            elif duration is not None and elapsed is not None and duration > 0 and elapsed >= duration:
                checks.append(
                    RuntimeHealthCheck(
                        "portfolio_blend_due_for_completion",
                        "warning",
                        metadata=metadata,
                    )
                )
            else:
                checks.append(RuntimeHealthCheck("portfolio_blend_active", "ok", metadata=metadata))
    return tuple(checks)


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_datetime_kind(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None and end.tzinfo is not None:
        return start.replace(tzinfo=end.tzinfo), end
    if start.tzinfo is not None and end.tzinfo is None:
        return start.replace(tzinfo=None), end
    return start, end

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from leaps_quant_engine.cycle_journal import CycleJournalEntry, CycleJournalStore
from leaps_quant_engine.order_status import OrderRuntimeStatusReport


@dataclass(frozen=True, slots=True)
class RecoveryAccountReport:
    broker_account_id: str | None
    market_scope: str | None
    order_store_path: Path | None
    account_store_path: Path | None
    last_cycle: CycleJournalEntry | None
    order_status: OrderRuntimeStatusReport
    blocked_reasons: tuple[str, ...]

    @property
    def recommended_next_actions(self) -> tuple[str, ...]:
        actions: list[str] = []
        if self.order_status.order_snapshot.open_tickets:
            actions.append("poll_open_tickets")
        if self.order_status.unallocated_fill_count:
            actions.append("allocate_unassigned_fills")
        if self.last_cycle is None:
            actions.append("run_runtime_once")
        elif self.last_cycle.snapshot_status in {"stale", "invalid"}:
            actions.append("refresh_snapshots")
        if self.blocked_reasons:
            actions.append("review_blocked_reasons")
        return tuple(dict.fromkeys(actions))

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "broker_account_id": self.broker_account_id,
            "market_scope": self.market_scope,
            "order_store_path": str(self.order_store_path) if self.order_store_path is not None else None,
            "account_store_path": str(self.account_store_path) if self.account_store_path is not None else None,
            "last_cycle": self.last_cycle.to_dict() if self.last_cycle is not None else None,
            "open_tickets": [
                ticket.to_dict()
                for ticket in self.order_status.order_snapshot.open_tickets
            ]
            if include_details
            else [],
            "open_ticket_count": len(self.order_status.order_snapshot.open_tickets),
            "unallocated_fill_count": self.order_status.unallocated_fill_count,
            "account_reconciliation": {
                "status": "not_checked",
                "reason": "broker holdings are not fetched by recovery report",
            },
            "blocked_reasons": list(self.blocked_reasons),
            "recommended_next_actions": list(self.recommended_next_actions),
            "order_runtime": self.order_status.to_dict(include_details=include_details),
        }


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    generated_at: datetime
    runtime_id: str
    config_version: str
    sleeve_ids: tuple[str, ...]
    accounts: tuple[RecoveryAccountReport, ...]

    @property
    def blocked_reasons(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(reason for account in self.accounts for reason in account.blocked_reasons))

    @property
    def recommended_next_actions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(action for account in self.accounts for action in account.recommended_next_actions))

    @property
    def status(self) -> str:
        if self.blocked_reasons:
            return "blocked"
        if self.recommended_next_actions:
            return "needs_attention"
        return "ok"

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "runtime_id": self.runtime_id,
            "config_version": self.config_version,
            "sleeve_ids": list(self.sleeve_ids),
            "blocked_reasons": list(self.blocked_reasons),
            "recommended_next_actions": list(self.recommended_next_actions),
            "accounts": [account.to_dict(include_details=include_details) for account in self.accounts],
        }


def build_recovery_account_report(
    *,
    order_status: OrderRuntimeStatusReport,
    journal_store: CycleJournalStore | None,
    sleeve_ids: tuple[str, ...],
    blocked_reasons: tuple[str, ...] = (),
) -> RecoveryAccountReport:
    last_cycles = [
        entry
        for sleeve_id in sleeve_ids
        for entry in [journal_store.latest(sleeve_id=sleeve_id, account_id=order_status.broker_account_id) if journal_store is not None else None]
        if entry is not None
    ]
    if not last_cycles and journal_store is not None:
        last_cycles = [
            entry
            for sleeve_id in sleeve_ids
            for entry in [journal_store.latest(sleeve_id=sleeve_id)]
            if entry is not None
        ]
    last_cycle = max(last_cycles, key=lambda entry: entry.generated_at) if last_cycles else None
    reasons: list[str] = list(blocked_reasons)
    if order_status.order_store_path is not None and not order_status.order_store_path.exists():
        reasons.append("order_runtime_store_missing")
    return RecoveryAccountReport(
        broker_account_id=order_status.broker_account_id,
        market_scope=order_status.market_scope,
        order_store_path=order_status.order_store_path,
        account_store_path=order_status.account_store_path,
        last_cycle=last_cycle,
        order_status=order_status,
        blocked_reasons=tuple(dict.fromkeys(reasons)),
    )


def build_recovery_report(
    *,
    runtime_id: str,
    config_version: str,
    sleeve_ids: tuple[str, ...],
    accounts: tuple[RecoveryAccountReport, ...],
    generated_at: datetime | None = None,
) -> RecoveryReport:
    return RecoveryReport(
        generated_at=generated_at or datetime.now(),
        runtime_id=runtime_id,
        config_version=config_version,
        sleeve_ids=sleeve_ids,
        accounts=accounts,
    )

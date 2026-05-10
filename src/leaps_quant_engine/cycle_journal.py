from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.runtime_integrity import current_engine_source_fingerprint


CYCLE_JOURNAL_SCHEMA_VERSION = "cycle_journal.v1"


class CycleJournalStore(Protocol):
    def append(self, entry: "CycleJournalEntry") -> None:
        """Append one cycle journal entry."""

    def entries(
        self,
        *,
        sleeve_id: str | None = None,
        account_id: str | None = None,
        market_scope: str | None = None,
        limit: int | None = None,
    ) -> tuple["CycleJournalEntry", ...]:
        """Return journal entries, optionally filtered by sleeve and route."""

    def latest(
        self,
        *,
        sleeve_id: str | None = None,
        account_id: str | None = None,
        market_scope: str | None = None,
    ) -> "CycleJournalEntry | None":
        """Return the latest matching journal entry."""


@dataclass(frozen=True, slots=True)
class CycleJournalEntry:
    runtime_id: str
    config_version: str
    sleeve_id: str
    generated_at: datetime
    recorded_at: datetime
    source: str
    status: str
    account_id: str | None = None
    route_id: str | None = None
    market_scope: str | None = None
    snapshot_status: str | None = None
    snapshot_as_of: str | None = None
    counts: Mapping[str, int | float] = field(default_factory=dict)
    timings: Mapping[str, float] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    entry_id: str = field(default_factory=lambda: f"cycle-journal-{uuid4()}")
    schema_version: str = CYCLE_JOURNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        object.__setattr__(self, "timings", MappingProxyType(dict(self.timings)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))

    @property
    def is_error(self) -> bool:
        return bool(self.errors) or self.status in {"error", "blocked", "failed"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "runtime_id": self.runtime_id,
            "config_version": self.config_version,
            "sleeve_id": self.sleeve_id,
            "account_id": self.account_id,
            "route_id": self.route_id,
            "market_scope": self.market_scope,
            "generated_at": self.generated_at.isoformat(),
            "recorded_at": self.recorded_at.isoformat(),
            "source": self.source,
            "status": self.status,
            "snapshot_status": self.snapshot_status,
            "snapshot_as_of": self.snapshot_as_of,
            "counts": dict(self.counts),
            "timings": dict(self.timings),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CycleJournalEntry":
        return cls(
            schema_version=str(payload.get("schema_version") or CYCLE_JOURNAL_SCHEMA_VERSION),
            entry_id=str(payload.get("entry_id") or f"cycle-journal-{uuid4()}"),
            runtime_id=str(payload.get("runtime_id") or ""),
            config_version=str(payload.get("config_version") or ""),
            sleeve_id=str(payload.get("sleeve_id") or ""),
            account_id=_optional_text(payload.get("account_id")),
            route_id=_optional_text(payload.get("route_id")),
            market_scope=_optional_text(payload.get("market_scope")),
            generated_at=_parse_datetime(payload.get("generated_at")),
            recorded_at=_parse_datetime(payload.get("recorded_at")),
            source=str(payload.get("source") or ""),
            status=str(payload.get("status") or "unknown"),
            snapshot_status=_optional_text(payload.get("snapshot_status")),
            snapshot_as_of=_optional_text(payload.get("snapshot_as_of")),
            counts=dict(payload.get("counts") or {}),
            timings=dict(payload.get("timings") or {}),
            warnings=tuple(str(item) for item in payload.get("warnings") or ()),
            errors=tuple(str(item) for item in payload.get("errors") or ()),
            metadata=dict(payload.get("metadata") or {}),
        )

    @classmethod
    def from_runtime_run_once_report(
        cls,
        report: Any,
        *,
        account_id: str | None = None,
        route_id: str | None = None,
        market_scope: str | None = None,
        source: str = "runtime-run-once",
        recorded_at: datetime | None = None,
        errors: Iterable[str] = (),
        warnings: Iterable[str] = (),
    ) -> "CycleJournalEntry":
        worker_cycle = report.worker.cycles[-1] if getattr(report.worker, "cycles", ()) else None
        framework = getattr(report, "framework", None)
        snapshot_quality = getattr(worker_cycle, "snapshot_quality", None)
        snapshot_status = getattr(getattr(snapshot_quality, "status", None), "value", None)
        quality_reasons = tuple(getattr(snapshot_quality, "reasons", ()) or ())
        counts: dict[str, int | float] = {
            "worker_cycle_count": getattr(report.worker, "cycles_completed", 0),
            "updated_symbol_count": getattr(worker_cycle, "updated_symbol_count", 0) if worker_cycle else 0,
            "failed_symbol_count": getattr(worker_cycle, "failed_symbol_count", 0) if worker_cycle else 0,
            "new_insight_count": framework.new_insight_batch.insight_count if framework is not None else 0,
            "active_insight_count": framework.active_insight_count if framework is not None else 0,
            "allocation_target_count": framework.portfolio_target_batch.target_count if framework is not None else 0,
            "sized_target_count": framework.order_sizing_batch.target_count if framework is not None else 0,
            "risk_decision_count": len(framework.risk_decisions.decisions) if framework is not None else 0,
            "approved_target_count": len(framework.risk_decisions.approved_targets) if framework is not None else 0,
            "order_intent_count": len(framework.order_intents) if framework is not None else 0,
        }
        timings = framework.timings.to_dict() if framework is not None else {}
        status = "ok"
        all_errors = tuple(errors)
        all_warnings = tuple(warnings) + quality_reasons
        if all_errors:
            status = "error"
        elif snapshot_status in {"stale", "invalid"}:
            status = "warnings"
        return cls(
            runtime_id=report.runtime_id,
            config_version=report.config_version,
            sleeve_id=report.sleeve_id,
            account_id=account_id,
            route_id=route_id,
            market_scope=market_scope,
            generated_at=_report_generated_at(report, worker_cycle, framework),
            recorded_at=recorded_at or datetime.now(),
            source=source,
            status=status,
            snapshot_status=snapshot_status,
            snapshot_as_of=getattr(worker_cycle, "snapshot_as_of", None) if worker_cycle else None,
            counts=counts,
            timings=timings,
            warnings=all_warnings,
            errors=all_errors,
            metadata={
                "engine_source_hash": current_engine_source_fingerprint().digest,
                "coarse_universe_id": getattr(report, "coarse_universe_id", ""),
                "active_universe_id": getattr(report, "active_universe_id", ""),
            },
        )

    @classmethod
    def from_framework_cycle(
        cls,
        cycle: Any,
        *,
        runtime_id: str,
        config_version: str = "",
        account_id: str | None = None,
        route_id: str | None = None,
        market_scope: str | None = None,
        source: str = "framework-backtest",
        recorded_at: datetime | None = None,
        snapshot_status: str | None = "fresh",
        warnings: Iterable[str] = (),
        errors: Iterable[str] = (),
    ) -> "CycleJournalEntry":
        generated_at = cycle.execution_batch.generated_at
        all_errors = tuple(errors)
        return cls(
            runtime_id=runtime_id,
            config_version=config_version,
            sleeve_id=cycle.sleeve_id,
            account_id=account_id,
            route_id=route_id,
            market_scope=market_scope,
            generated_at=generated_at,
            recorded_at=recorded_at or datetime.now(),
            source=source,
            status="error" if all_errors else "ok",
            snapshot_status=snapshot_status,
            snapshot_as_of=generated_at.isoformat(),
            counts={
                "new_insight_count": cycle.new_insight_batch.insight_count,
                "active_insight_count": cycle.active_insight_count,
                "allocation_target_count": cycle.portfolio_target_batch.target_count,
                "sized_target_count": cycle.order_sizing_batch.target_count,
                "risk_decision_count": len(cycle.risk_decisions.decisions),
                "approved_target_count": len(cycle.risk_decisions.approved_targets),
                "order_intent_count": len(cycle.order_intents),
            },
            timings=cycle.timings.to_dict(),
            warnings=tuple(warnings),
            errors=all_errors,
            metadata={
                "engine_source_hash": current_engine_source_fingerprint().digest,
                "source_snapshot_id": cycle.source_snapshot_id,
                "indicator_snapshot_id": cycle.indicator_snapshot_id,
            },
        )


@dataclass(frozen=True, slots=True)
class FileCycleJournalStore:
    path: Path

    def append(self, entry: CycleJournalEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")

    def append_many(self, entries: Iterable[CycleJournalEntry]) -> None:
        for entry in entries:
            self.append(entry)

    def entries(
        self,
        *,
        sleeve_id: str | None = None,
        account_id: str | None = None,
        market_scope: str | None = None,
        limit: int | None = None,
    ) -> tuple[CycleJournalEntry, ...]:
        matches = [
            entry
            for entry in self._iter_entries()
            if (sleeve_id is None or entry.sleeve_id == sleeve_id)
            and (account_id is None or entry.account_id == account_id)
            and (market_scope is None or entry.market_scope == market_scope)
        ]
        if limit is not None and limit >= 0:
            matches = matches[-limit:]
        return tuple(matches)

    def latest(
        self,
        *,
        sleeve_id: str | None = None,
        account_id: str | None = None,
        market_scope: str | None = None,
    ) -> CycleJournalEntry | None:
        matches = self.entries(sleeve_id=sleeve_id, account_id=account_id, market_scope=market_scope)
        if not matches:
            return None
        return matches[-1]

    def _iter_entries(self) -> tuple[CycleJournalEntry, ...]:
        if not self.path.exists():
            return ()
        entries: list[CycleJournalEntry] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                payload = json.loads(text)
                if isinstance(payload, Mapping):
                    entries.append(CycleJournalEntry.from_dict(payload))
        return tuple(entries)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.now()
    return datetime.fromisoformat(text)


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _report_generated_at(report: Any, worker_cycle: Any | None, framework: Any | None) -> datetime:
    if framework is not None:
        return framework.execution_batch.generated_at
    if worker_cycle is not None and getattr(worker_cycle, "completed_at", None):
        return _parse_datetime(worker_cycle.completed_at)
    return datetime.now()

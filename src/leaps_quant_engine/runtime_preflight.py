from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from leaps_quant_engine.cycle_journal import CycleJournalStore
from leaps_quant_engine.market_data import MarketDataError
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.order_status import OrderRuntimeStatusReport
from leaps_quant_engine.portfolio import StaticPortfolioProvider
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies, bootstrap_sleeve_runtime, resolve_runtime_path
from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot
from leaps_quant_engine.runtime_integrity import RuntimeCodeIdentity, build_runtime_code_identity


@dataclass(frozen=True, slots=True)
class RuntimePreflightCheck:
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
class RuntimePreflightReport:
    generated_at: datetime
    runtime_id: str
    mode: str
    sleeve_ids: tuple[str, ...]
    code_identity: RuntimeCodeIdentity
    checks: tuple[RuntimePreflightCheck, ...]

    @property
    def status(self) -> str:
        statuses = {check.status for check in self.checks}
        if "critical" in statuses:
            return "blocked"
        if "warning" in statuses:
            return "needs_attention"
        return "ok"

    @property
    def recommended_next_actions(self) -> tuple[str, ...]:
        actions: list[str] = []
        for check in self.checks:
            if check.status == "ok":
                continue
            if check.name in {"bootstrap_sleeve", "runtime_file_fingerprints"}:
                actions.append("fix_config_or_model_imports")
            elif check.name in {"journal_missing", "journal_empty"}:
                actions.append("run_runtime_once_with_journal")
            elif check.name in {"config_changed_since_last_cycle", "engine_code_changed_since_last_cycle", "runtime_fingerprint_changed_since_last_cycle"}:
                actions.append("stage_reload_and_run_runtime_once")
            elif check.name in {"latest_cycle_missing_code_identity"}:
                actions.append("run_one_cycle_to_seed_code_identity")
            elif check.name in {"account_store_path", "order_store_path", "sleeve_account_route"}:
                actions.append("verify_order_runtime_routes_and_stores")
            elif check.name in {"open_tickets"}:
                actions.append("run_order_runtime_supervise")
            elif check.name in {"unallocated_fills"}:
                actions.append("review_virtual_account_allocations")
        return tuple(dict.fromkeys(actions))

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "runtime_id": self.runtime_id,
            "mode": self.mode,
            "sleeve_ids": list(self.sleeve_ids),
            "code_identity": self.code_identity.to_dict(include_files=include_details),
            "checks": [check.to_dict() for check in self.checks],
            "recommended_next_actions": list(self.recommended_next_actions),
        }


def build_runtime_preflight_report(
    *,
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: Iterable[str] | None = None,
    journal_store: CycleJournalStore | None = None,
    journal_path: Path | None = None,
    order_statuses: Iterable[OrderRuntimeStatusReport] = (),
    strict_live: bool = False,
    check_bootstrap: bool = True,
    generated_at: datetime | None = None,
) -> RuntimePreflightReport:
    generated_at = generated_at or datetime.now()
    selected_sleeve_ids = tuple(dict.fromkeys(sleeve_ids or (sleeve.sleeve_id for sleeve in snapshot.config.sleeves)))
    code_identity = build_runtime_code_identity(snapshot, sleeve_ids=selected_sleeve_ids)
    checks: list[RuntimePreflightCheck] = [
        RuntimePreflightCheck(
            "config_loaded",
            "ok",
            metadata={
                "config_path": str(snapshot.source_path),
                "config_version": snapshot.version,
            },
        ),
        RuntimePreflightCheck(
            "engine_source_fingerprint",
            "ok",
            metadata=code_identity.engine_source.to_dict(),
        ),
    ]
    checks.extend(_file_checks(code_identity, strict_live=strict_live))
    checks.extend(_route_checks(snapshot, selected_sleeve_ids, strict_live=strict_live))
    checks.extend(_journal_checks(snapshot, selected_sleeve_ids, code_identity, journal_store, journal_path))
    checks.extend(_order_status_checks(order_statuses))
    if check_bootstrap:
        checks.extend(_bootstrap_checks(snapshot, selected_sleeve_ids))
    return RuntimePreflightReport(
        generated_at=generated_at,
        runtime_id=snapshot.config.runtime_id,
        mode=snapshot.config.mode,
        sleeve_ids=selected_sleeve_ids,
        code_identity=code_identity,
        checks=tuple(checks),
    )


def _file_checks(code_identity: RuntimeCodeIdentity, *, strict_live: bool) -> tuple[RuntimePreflightCheck, ...]:
    if not code_identity.missing_files:
        return (
            RuntimePreflightCheck(
                "runtime_file_fingerprints",
                "ok",
                metadata={
                    "file_count": len(code_identity.file_fingerprints),
                    "runtime_fingerprint": code_identity.runtime_fingerprint,
                },
            ),
        )
    return (
        RuntimePreflightCheck(
            "runtime_file_fingerprints",
            "critical" if strict_live else "warning",
            reason="missing_runtime_files",
            metadata={
                "missing_files": [item.to_dict() for item in code_identity.missing_files],
                "runtime_fingerprint": code_identity.runtime_fingerprint,
            },
        ),
    )


def _route_checks(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    *,
    strict_live: bool,
) -> tuple[RuntimePreflightCheck, ...]:
    checks: list[RuntimePreflightCheck] = []
    route_status = "critical" if strict_live and snapshot.config.mode == "live" else "warning"
    for sleeve_id in sleeve_ids:
        sleeve = snapshot.config.sleeve(sleeve_id)
        account_ids = tuple(dict.fromkeys([
            *(sleeve.broker_account_routes.values()),
            *([sleeve.broker_account_id] if sleeve.broker_account_id else []),
        ]))
        if snapshot.config.mode in {"live", "paper"} and not account_ids:
            checks.append(
                RuntimePreflightCheck(
                    "sleeve_account_route",
                    route_status,
                    reason="missing_broker_account_route",
                    metadata={"sleeve_id": sleeve_id},
                )
            )
        for account_id in account_ids:
            account = snapshot.config.broker_account(str(account_id))
            account_store_path = resolve_runtime_path(snapshot, account.account_store_path).resolve()
            order_store_path = (
                resolve_runtime_path(snapshot, account.order_store_path).resolve()
                if account.order_store_path is not None
                else (account_store_path.parent.parent / "order-runtime" / f"{account_store_path.stem}.jsonl").resolve()
            )
            checks.append(_path_parent_check("account_store_path", account_store_path, account_id, strict_live=strict_live))
            checks.append(_path_parent_check("order_store_path", order_store_path, account_id, strict_live=strict_live))
    return tuple(checks)


def _path_parent_check(name: str, path: Path, account_id: str, *, strict_live: bool) -> RuntimePreflightCheck:
    parent_exists = path.parent.exists()
    if parent_exists:
        return RuntimePreflightCheck(
            name,
            "ok",
            metadata={"account_id": account_id, "path": str(path), "exists": path.exists()},
        )
    return RuntimePreflightCheck(
        name,
        "critical" if strict_live else "warning",
        reason="parent_directory_missing",
        metadata={"account_id": account_id, "path": str(path), "parent": str(path.parent)},
    )


def _journal_checks(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    code_identity: RuntimeCodeIdentity,
    journal_store: CycleJournalStore | None,
    journal_path: Path | None,
) -> tuple[RuntimePreflightCheck, ...]:
    checks: list[RuntimePreflightCheck] = []
    if journal_store is None:
        checks.append(
            RuntimePreflightCheck(
                "journal_missing",
                "warning" if snapshot.config.mode in {"live", "paper"} else "ok",
                metadata={"path": str(journal_path) if journal_path is not None else None},
            )
        )
        return tuple(checks)
    if journal_path is not None and not journal_path.exists():
        checks.append(RuntimePreflightCheck("journal_missing", "warning", metadata={"path": str(journal_path)}))
    for sleeve_id in sleeve_ids:
        latest = journal_store.latest(sleeve_id=sleeve_id)
        if latest is None:
            checks.append(RuntimePreflightCheck("journal_empty", "warning", metadata={"sleeve_id": sleeve_id}))
            continue
        checks.append(
            RuntimePreflightCheck(
                "last_cycle_seen",
                "ok",
                metadata={
                    "sleeve_id": sleeve_id,
                    "entry_id": latest.entry_id,
                    "generated_at": latest.generated_at.isoformat(),
                    "config_version": latest.config_version,
                    "status": latest.status,
                },
            )
        )
        if latest.config_version != snapshot.version:
            checks.append(
                RuntimePreflightCheck(
                    "config_changed_since_last_cycle",
                    "warning",
                    metadata={"sleeve_id": sleeve_id, "latest": latest.config_version, "current": snapshot.version},
                )
            )
        latest_engine_hash = latest.metadata.get("engine_source_hash")
        latest_runtime_fingerprint = latest.metadata.get("runtime_fingerprint")
        if not latest_engine_hash and not latest_runtime_fingerprint:
            checks.append(RuntimePreflightCheck("latest_cycle_missing_code_identity", "warning", metadata={"sleeve_id": sleeve_id}))
            continue
        if latest_engine_hash and latest_engine_hash != code_identity.engine_source.digest:
            checks.append(
                RuntimePreflightCheck(
                    "engine_code_changed_since_last_cycle",
                    "warning",
                    metadata={
                        "sleeve_id": sleeve_id,
                        "latest": latest_engine_hash,
                        "current": code_identity.engine_source.digest,
                    },
                )
            )
        if latest_runtime_fingerprint and latest_runtime_fingerprint != code_identity.runtime_fingerprint:
            checks.append(
                RuntimePreflightCheck(
                    "runtime_fingerprint_changed_since_last_cycle",
                    "warning",
                    metadata={
                        "sleeve_id": sleeve_id,
                        "latest": latest_runtime_fingerprint,
                        "current": code_identity.runtime_fingerprint,
                    },
                )
            )
    return tuple(checks)


def _order_status_checks(order_statuses: Iterable[OrderRuntimeStatusReport]) -> tuple[RuntimePreflightCheck, ...]:
    checks: list[RuntimePreflightCheck] = []
    for status in order_statuses:
        open_ticket_count = len(status.order_snapshot.open_tickets)
        checks.append(
            RuntimePreflightCheck(
                "open_tickets",
                "warning" if open_ticket_count else "ok",
                metadata={"account_id": status.broker_account_id, "count": open_ticket_count},
            )
        )
        if status.unallocated_fill_count:
            checks.append(
                RuntimePreflightCheck(
                    "unallocated_fills",
                    "warning",
                    metadata={"account_id": status.broker_account_id, "count": status.unallocated_fill_count},
                )
            )
    return tuple(checks)


def _bootstrap_checks(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
) -> tuple[RuntimePreflightCheck, ...]:
    checks: list[RuntimePreflightCheck] = []
    provider = _PreflightMarketDataProvider()
    dependencies = RuntimeBootstrapDependencies(
        live_provider_factory=lambda universe, rate_limit_per_second: provider,
        history_provider_factory=lambda: provider,
        portfolio_provider=StaticPortfolioProvider(
            default_cash_by_sleeve={sleeve.sleeve_id: sleeve.cash for sleeve in snapshot.config.sleeves}
        ),
    )
    for sleeve_id in sleeve_ids:
        try:
            runtime = bootstrap_sleeve_runtime(
                snapshot,
                sleeve_id,
                dependencies=dependencies,
                refresh_fine=False,
                preselect_warmup=False,
            )
        except Exception as exc:  # noqa: BLE001 - preflight must report failures without raising.
            checks.append(
                RuntimePreflightCheck(
                    "bootstrap_sleeve",
                    "critical",
                    reason=str(exc),
                    metadata={"sleeve_id": sleeve_id},
                )
            )
            continue
        checks.append(
            RuntimePreflightCheck(
                "bootstrap_sleeve",
                "ok",
                metadata={
                    "sleeve_id": sleeve_id,
                    "coarse_universe_id": runtime.coarse_universe.id,
                    "active_universe_id": runtime.active_result.active_universe.id,
                    "alpha_model_count": len(runtime.alpha_runtime.active_alpha_ids()),
                    "selection_model_count": len(runtime.selection_models),
                },
            )
        )
    return tuple(checks)


@dataclass(frozen=True, slots=True)
class _PreflightMarketDataProvider:
    def get_latest_bar(self, symbol: Symbol) -> Bar:
        raise MarketDataError(f"preflight provider does not fetch latest bars: {symbol.key}")

    def get_history(self, symbol: Symbol, *, start=None, end=None) -> list[Bar]:
        return []

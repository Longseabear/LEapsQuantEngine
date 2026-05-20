from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from leaps_quant_engine.broker_routing import configured_account_ids_for_sleeve, currency_for_market_scope
from leaps_quant_engine.cycle_journal import CycleJournalEntry
from leaps_quant_engine.market_calendar import session_report_for_market_scope
from leaps_quant_engine.models import OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_bootstrap import resolve_runtime_path
from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot, load_runtime_config_snapshot
from leaps_quant_engine.virtual_account import (
    FillAllocation,
    FillAllocationStatus,
    IgnoredBrokerFill,
    VirtualFillEvent,
)


OPERATOR_DASHBOARD_SCHEMA_VERSION = "operator_dashboard_snapshot.v1"
_FAVICON_ICO = (
    b"\x00\x00\x01\x00\x01\x00\x01\x01\x00\x00\x01\x00 \x00(\x00\x00\x00"
    b"\x16\x00\x00\x00(\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x01\x00 "
    b"\x00\x00\x00\x00\x00\x08\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00/\\o\xed\xff\x00\x00\x00\x00"
)


@dataclass(frozen=True, slots=True)
class _OrderRuntimeRoute:
    account_id: str | None
    market_scope: str | None
    currency: str
    account_store_path: Path
    order_store_path: Path


class ReadOnlyVirtualSleeveAccountView:
    """Read virtual account state without creating or repairing account files."""

    def __init__(
        self,
        path: Path,
        *,
        default_cash_by_sleeve: Mapping[str, float] | None = None,
        default_currency: str = "KRW",
    ) -> None:
        self.path = path
        self.default_cash_by_sleeve = dict(default_cash_by_sleeve or {})
        self.default_currency = str(default_currency or "KRW").upper()

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        state = self._load_state()
        sleeve = state.get("sleeves", {}).get(sleeve_id)
        if isinstance(sleeve, Mapping):
            return _portfolio_from_payload(sleeve, default_currency=self.default_currency)
        cash = float(self.default_cash_by_sleeve.get(sleeve_id, 0.0))
        cash_by_currency = {self.default_currency: cash} if cash else {}
        return Portfolio(cash=cash, cash_by_currency=cash_by_currency)

    def fill_allocation_statuses(
        self,
        *,
        symbol: Symbol | None = None,
        side: OrderSide | None = None,
    ) -> tuple[FillAllocationStatus, ...]:
        state = self._load_state()
        allocations_by_fill: dict[str, list[FillAllocation]] = {}
        for raw in dict(state.get("fill_allocations") or {}).values():
            if isinstance(raw, dict):
                allocation = FillAllocation.from_dict(raw)
                allocations_by_fill.setdefault(allocation.fill_id, []).append(allocation)
        ignored_by_fill = {
            fill_id: IgnoredBrokerFill.from_dict(raw)
            for fill_id, raw in dict(state.get("ignored_broker_fills") or {}).items()
            if isinstance(raw, dict)
        }
        statuses: list[FillAllocationStatus] = []
        for raw in dict(state.get("broker_fills") or {}).values():
            if not isinstance(raw, dict):
                continue
            fill = VirtualFillEvent.from_dict(raw)
            if symbol is not None and fill.symbol != symbol:
                continue
            if side is not None and fill.side is not side:
                continue
            allocations = tuple(
                sorted(
                    allocations_by_fill.get(fill.fill_id, ()),
                    key=lambda allocation: allocation.resolved_allocation_id(),
                )
            )
            statuses.append(
                FillAllocationStatus(
                    fill=fill,
                    allocated_quantity=sum(allocation.quantity for allocation in allocations),
                    allocations=allocations,
                    ignored=ignored_by_fill.get(fill.fill_id),
                )
            )
        return tuple(sorted(statuses, key=lambda status: (status.fill.filled_at, status.fill.fill_id)))

    def _load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Virtual account state must be an object: {self.path}")
        return payload


def build_operator_dashboard_snapshot(
    config_path: str | Path,
    *,
    sleeve_ids: Sequence[str] = (),
    account_id: str | None = None,
    account_store_path: str | Path | None = None,
    order_store_path: str | Path | None = None,
    journal_path: str | Path | None = None,
    recent_events: int = 10,
    max_cycle_age_seconds: float = 300.0,
    max_open_ticket_age_seconds: float = 600.0,
    include_details: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or datetime.now()
    snapshot = load_runtime_config_snapshot(config_path)
    selected_sleeve_ids = _status_sleeve_ids(snapshot, sleeve_ids)
    routes = _resolve_order_runtime_routes(
        snapshot,
        account_id,
        Path(account_store_path) if account_store_path is not None else None,
        Path(order_store_path) if order_store_path is not None else None,
        selected_sleeve_ids,
    )
    journal = _runtime_journal_path(snapshot, Path(journal_path) if journal_path is not None else None)
    journal_entries = _read_recent_cycle_entries(journal, limit=200) if journal is not None else ()
    order_reports = tuple(
        _build_order_runtime_status_for_route(
            snapshot,
            route,
            selected_sleeve_ids,
            recent_events=recent_events,
            generated_at=generated_at,
        )
        for route in routes
    )
    health_payloads = [
        _build_lightweight_health_payload(
            runtime_id=snapshot.config.runtime_id,
            sleeve_ids=selected_sleeve_ids,
            journal_entries=journal_entries,
            order_status=report,
            journal_path=journal,
            max_cycle_age_seconds=max_cycle_age_seconds,
            max_open_ticket_age_seconds=max_open_ticket_age_seconds,
            generated_at=generated_at,
        )
        for report in order_reports
    ]
    recovery_payload = _build_lightweight_recovery_payload(
        runtime_id=snapshot.config.runtime_id,
        config_version=snapshot.version,
        sleeve_ids=selected_sleeve_ids,
        routes=routes,
        order_reports=order_reports,
        journal_entries=journal_entries,
        generated_at=generated_at,
    )
    market_scopes = tuple(sorted({"domestic", "overseas"} | {route.market_scope for route in routes if route.market_scope}))
    order_payloads = [_compact_order_route(report.to_dict(include_details=include_details)) for report in order_reports]
    warnings = tuple(
        dict.fromkeys(
            warning
            for report in order_reports
            for warning in report.warnings
        )
    )
    return {
        "schema_version": OPERATOR_DASHBOARD_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "source": {
            "snapshot_only": True,
            "kis_api": "not_called",
            "market_data_provider": "not_called",
            "writes_runtime_state": False,
        },
        "runtime": {
            "runtime_id": snapshot.config.runtime_id,
            "mode": snapshot.config.mode,
            "timezone": snapshot.config.timezone,
            "config_version": snapshot.version,
            "config_path": str(Path(config_path)),
            "sleeve_ids": list(selected_sleeve_ids),
            "route_count": len(routes),
            "journal_path": str(journal) if journal is not None else None,
        },
        "summary": _summary_payload(order_payloads, health_payloads, recovery_payload),
        "market_sessions": {
            scope: session_report_for_market_scope(scope, now=generated_at).to_dict()
            for scope in market_scopes
        },
        "health_routes": health_payloads,
        "recovery": recovery_payload,
        "order_routes": order_payloads,
        "cycle_journal": _cycle_journal_payload(journal, journal_entries, selected_sleeve_ids),
        "warnings": list(warnings),
    }


def serve_operator_ui(
    config_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    sleeve_ids: Sequence[str] = (),
    account_id: str | None = None,
    account_store_path: str | Path | None = None,
    order_store_path: str | Path | None = None,
    journal_path: str | Path | None = None,
    recent_events: int = 10,
) -> None:
    options = {
        "config_path": config_path,
        "sleeve_ids": tuple(sleeve_ids),
        "account_id": account_id,
        "account_store_path": account_store_path,
        "order_store_path": order_store_path,
        "journal_path": journal_path,
        "recent_events": recent_events,
    }

    class OperatorUIRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib hook name.
            path = urlsplit(self.path).path
            if path in {"", "/"}:
                self._write_text(INDEX_HTML, "text/html; charset=utf-8")
                return
            if path == "/assets/styles.css":
                self._write_text(STYLES_CSS, "text/css; charset=utf-8")
                return
            if path == "/assets/app.js":
                self._write_text(APP_JS, "text/javascript; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._write_bytes(_FAVICON_ICO, "image/x-icon")
                return
            if path == "/api/snapshot":
                self._write_json(build_operator_dashboard_snapshot(**options))
                return
            self.send_error(404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _write_text(self, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _write_bytes(self, body: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), OperatorUIRequestHandler)
    print(f"Operator UI serving snapshot-only dashboard at http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _build_order_runtime_status_for_route(
    snapshot: RuntimeConfigSnapshot,
    route: _OrderRuntimeRoute,
    sleeve_ids: tuple[str, ...],
    *,
    recent_events: int,
    generated_at: datetime,
):
    return build_order_runtime_status(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=sleeve_ids,
        order_state_store=FileOrderRuntimeStateStore(route.order_store_path),
        account_store=ReadOnlyVirtualSleeveAccountView(
            route.account_store_path,
            default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
            default_currency=route.currency,
        ),
        order_store_path=route.order_store_path,
        account_store_path=route.account_store_path,
        broker_account_id=route.account_id,
        market_scope=route.market_scope,
        currency=route.currency,
        recent_events=recent_events,
        generated_at=generated_at,
    )


def _status_sleeve_ids(snapshot: RuntimeConfigSnapshot, requested_sleeve_ids: Sequence[str]) -> tuple[str, ...]:
    if requested_sleeve_ids:
        for sleeve_id in requested_sleeve_ids:
            snapshot.config.sleeve(sleeve_id)
        return tuple(dict.fromkeys(requested_sleeve_ids))
    return tuple(sleeve.sleeve_id for sleeve in snapshot.config.sleeves)


def _default_cash_by_sleeve(
    snapshot: RuntimeConfigSnapshot,
    currency: str | None = None,
    account_id: str | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    code = str(currency or "").strip().upper()
    for sleeve in snapshot.config.sleeves:
        cash_by_currency = dict(getattr(sleeve, "cash_by_currency", {}) or {})
        if code and cash_by_currency:
            result[sleeve.sleeve_id] = float(cash_by_currency.get(code, 0.0) or 0.0)
            continue
        if code and dict(getattr(sleeve, "broker_account_routes", {}) or {}) and account_id != getattr(sleeve, "broker_account_id", None):
            result[sleeve.sleeve_id] = 0.0
            continue
        result[sleeve.sleeve_id] = float(sleeve.cash)
    return result


def _runtime_journal_path(snapshot: RuntimeConfigSnapshot, explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    if snapshot.config.journal_path is None:
        return None
    return resolve_runtime_path(snapshot, snapshot.config.journal_path).resolve()


def _read_recent_cycle_entries(path: Path, *, limit: int) -> tuple[CycleJournalEntry, ...]:
    if not path.exists() or limit <= 0:
        return ()
    entries: list[CycleJournalEntry] = []
    for line in _tail_lines(path, limit):
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                entries.append(CycleJournalEntry.from_dict(payload))
        except (json.JSONDecodeError, ValueError):
            continue
    return tuple(entries)


def _tail_lines(path: Path, limit: int, *, chunk_size: int = 65_536) -> list[str]:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        chunks: list[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def _latest_cycle_entry(
    entries: tuple[CycleJournalEntry, ...],
    *,
    sleeve_id: str,
    account_id: str | None = None,
) -> CycleJournalEntry | None:
    for entry in reversed(entries):
        if entry.sleeve_id != sleeve_id:
            continue
        if account_id is not None and entry.account_id != account_id:
            continue
        return entry
    return None


def _resolve_order_runtime_routes(
    snapshot: RuntimeConfigSnapshot,
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
    sleeve_ids: Sequence[str],
) -> tuple[_OrderRuntimeRoute, ...]:
    if account_id or explicit_account_store is not None:
        return (
            _resolve_order_runtime_route(
                snapshot,
                account_id,
                explicit_account_store,
                explicit_order_store,
                sleeve_ids,
            ),
        )
    account_ids: list[str] = []
    for sleeve_id in sleeve_ids:
        account_ids.extend(configured_account_ids_for_sleeve(snapshot.config.sleeve(sleeve_id)))
    unique_account_ids = tuple(dict.fromkeys(account_ids))
    if not unique_account_ids:
        return (
            _resolve_order_runtime_route(
                snapshot,
                account_id,
                explicit_account_store,
                explicit_order_store,
                sleeve_ids,
            ),
        )
    return tuple(
        _resolve_order_runtime_route(
            snapshot,
            route_account_id,
            None,
            explicit_order_store,
            sleeve_ids,
            strict_account_scope=False,
        )
        for route_account_id in unique_account_ids
    )


def _resolve_order_runtime_route(
    snapshot: RuntimeConfigSnapshot,
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
    sleeve_ids: Sequence[str],
    *,
    strict_account_scope: bool = True,
) -> _OrderRuntimeRoute:
    account = _broker_account_for_order_runtime(
        snapshot,
        account_id,
        sleeve_ids,
        strict_account_scope=strict_account_scope,
    )
    if explicit_account_store is not None:
        account_store = resolve_runtime_path(snapshot, explicit_account_store).resolve()
        return _OrderRuntimeRoute(
            account_id=account.account_id if account is not None else account_id,
            market_scope=account.market_scope if account is not None else None,
            currency=account.currency if account is not None else currency_for_market_scope(None),
            account_store_path=account_store,
            order_store_path=_resolve_status_order_store_path(snapshot, explicit_order_store, account_store),
        )
    if account is not None:
        account_store = resolve_runtime_path(snapshot, account.account_store_path).resolve()
        order_store = (
            resolve_runtime_path(snapshot, account.order_store_path).resolve()
            if explicit_order_store is None and account.order_store_path is not None
            else _resolve_status_order_store_path(snapshot, explicit_order_store, account_store)
        )
        return _OrderRuntimeRoute(
            account_id=account.account_id,
            market_scope=account.market_scope,
            currency=account.currency,
            account_store_path=account_store,
            order_store_path=order_store,
        )
    account_store = _resolve_status_account_store_path(snapshot, explicit_account_store, sleeve_ids)
    return _OrderRuntimeRoute(
        account_id=account_id,
        market_scope=None,
        currency=currency_for_market_scope(None),
        account_store_path=account_store,
        order_store_path=_resolve_status_order_store_path(snapshot, explicit_order_store, account_store),
    )


def _broker_account_for_order_runtime(
    snapshot: RuntimeConfigSnapshot,
    account_id: str | None,
    sleeve_ids: Sequence[str],
    *,
    strict_account_scope: bool = True,
):
    if account_id:
        try:
            account = snapshot.config.broker_account(account_id)
        except KeyError as exc:
            raise RuntimeError(f"Unknown broker_account_id: {account_id}") from exc
        valid_account_ids_by_sleeve = {
            sleeve_id: set(configured_account_ids_for_sleeve(snapshot.config.sleeve(sleeve_id)))
            for sleeve_id in sleeve_ids
        }
        if not strict_account_scope:
            allowed = set().union(*valid_account_ids_by_sleeve.values()) if valid_account_ids_by_sleeve else set()
            if allowed and account.account_id not in allowed:
                raise RuntimeError(
                    f"Selected sleeves route to broker_account_id values {sorted(allowed)}, not '{account.account_id}'."
                )
            return account
        for sleeve_id, valid_account_ids in valid_account_ids_by_sleeve.items():
            if valid_account_ids and account.account_id not in valid_account_ids:
                raise RuntimeError(
                    f"Sleeve '{sleeve_id}' routes to broker_account_id values {sorted(valid_account_ids)}, not '{account.account_id}'."
                )
        return account
    routed_account_ids = tuple(
        dict.fromkeys(
            routed_account_id
            for sleeve in (snapshot.config.sleeve(sleeve_id) for sleeve_id in sleeve_ids)
            for routed_account_id in configured_account_ids_for_sleeve(sleeve)
        )
    )
    if not routed_account_ids:
        return None
    if len(routed_account_ids) > 1:
        raise RuntimeError("Selected sleeves route to multiple broker accounts; pass --account-id and run one account at a time.")
    return snapshot.config.broker_account(routed_account_ids[0])


def _resolve_status_account_store_path(
    snapshot: RuntimeConfigSnapshot,
    explicit_path: Path | None,
    sleeve_ids: Sequence[str],
) -> Path:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    resolved_paths: list[Path] = []
    for sleeve_id in sleeve_ids:
        sleeve = snapshot.config.sleeve(sleeve_id)
        if sleeve.portfolio.account_store_path is not None:
            resolved_paths.append(resolve_runtime_path(snapshot, sleeve.portfolio.account_store_path).resolve())
    unique_paths = tuple(dict.fromkeys(resolved_paths))
    if not unique_paths:
        raise RuntimeError("portfolio.account_store_path or --account-store is required for operator UI.")
    if len(unique_paths) > 1:
        raise RuntimeError("Multiple account_store_path values are configured; pass --account-store explicitly.")
    return unique_paths[0]


def _resolve_status_order_store_path(
    snapshot: RuntimeConfigSnapshot,
    explicit_path: Path | None,
    account_store_path: Path,
) -> Path:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    return (account_store_path.parent.parent / "order-runtime" / f"{account_store_path.stem}.jsonl").resolve()


def _cycle_journal_payload(
    journal_path: Path | None,
    entries: tuple[CycleJournalEntry, ...],
    sleeve_ids: tuple[str, ...],
) -> dict[str, Any]:
    if journal_path is None:
        return {"path": None, "latest_by_sleeve": {}, "recent_entries": []}
    latest_by_sleeve = {
        sleeve_id: _compact_cycle_entry(entry.to_dict()) if entry is not None else None
        for sleeve_id in sleeve_ids
        for entry in (_latest_cycle_entry(entries, sleeve_id=sleeve_id),)
    }
    return {
        "path": str(journal_path),
        "exists": journal_path.exists(),
        "latest_by_sleeve": latest_by_sleeve,
        "recent_entries": [_compact_cycle_entry(entry.to_dict()) for entry in entries[-20:]],
    }


def _build_lightweight_health_payload(
    *,
    runtime_id: str,
    sleeve_ids: tuple[str, ...],
    journal_entries: tuple[CycleJournalEntry, ...],
    order_status,
    journal_path: Path | None,
    max_cycle_age_seconds: float,
    max_open_ticket_age_seconds: float,
    generated_at: datetime,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if journal_path is not None and not journal_path.exists():
        checks.append(_health_check("missing_journal_store", "warning", metadata={"path": str(journal_path)}))
    for sleeve_id in sleeve_ids:
        latest = _latest_cycle_entry(journal_entries, sleeve_id=sleeve_id)
        if latest is None:
            checks.append(_health_check("no_cycle_journal_entries", "warning", metadata={"sleeve_id": sleeve_id}))
            continue
        age_seconds = max(0.0, _elapsed_seconds(generated_at, latest.generated_at))
        checks.append(
            _health_check(
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
                _health_check(
                    "snapshot_quality",
                    "critical" if latest.snapshot_status == "invalid" else "warning",
                    reason=str(latest.snapshot_status),
                    metadata={"sleeve_id": sleeve_id, "entry_id": latest.entry_id},
                )
            )
    if order_status.market_scope:
        calendar_report = session_report_for_market_scope(order_status.market_scope, now=generated_at)
        checks.append(
            _health_check(
                "market_calendar",
                "warning" if calendar_report.quality.status == "degraded" else "ok",
                reason=";".join(calendar_report.quality.warnings),
                metadata=calendar_report.to_dict(),
            )
        )
    if order_status.unallocated_fill_count:
        checks.append(_health_check("unallocated_fills", "warning", metadata={"count": order_status.unallocated_fill_count}))
    open_tickets = order_status.order_snapshot.open_tickets
    checks.append(_health_check("open_tickets", "warning" if open_tickets else "ok", metadata={"count": len(open_tickets)}))
    old_tickets = [
        ticket
        for ticket in open_tickets
        if _elapsed_seconds(generated_at, ticket.created_at) > max_open_ticket_age_seconds
    ]
    if old_tickets:
        checks.append(
            _health_check(
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
            _health_check(
                "order_runtime_needs_attention",
                "warning",
                metadata={"broker_account_id": order_status.broker_account_id},
            )
        )
    if not checks:
        checks.append(_health_check("runtime_health_inputs", "ok"))
    status = _aggregate_health_status(checks)
    return {
        "status": status,
        "generated_at": generated_at.isoformat(),
        "runtime_id": runtime_id,
        "sleeve_ids": list(sleeve_ids),
        "checks": checks,
        "recommended_next_actions": _recommended_health_actions(checks),
    }


def _health_check(
    name: str,
    status: str,
    *,
    reason: str = "",
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "reason": reason,
        "metadata": dict(metadata or {}),
    }


def _aggregate_health_status(checks: Sequence[Mapping[str, Any]]) -> str:
    statuses = {str(check.get("status") or "") for check in checks}
    if "critical" in statuses:
        return "critical"
    if "warning" in statuses:
        return "needs_attention"
    return "ok"


def _recommended_health_actions(checks: Sequence[Mapping[str, Any]]) -> list[str]:
    actions: list[str] = []
    for check in checks:
        if check.get("status") == "ok":
            continue
        name = check.get("name")
        if name in {"open_ticket_age", "open_tickets"}:
            actions.append("run_order_runtime_supervise")
        elif name in {"unallocated_fills", "order_runtime_needs_attention"}:
            actions.append("review_virtual_account_allocations")
        elif name in {"missing_journal_store", "no_cycle_journal_entries", "last_cycle_age"}:
            actions.append("run_runtime_once_or_check_worker")
        elif name == "snapshot_quality":
            actions.append("refresh_snapshot_worker")
    return list(dict.fromkeys(actions))


def _elapsed_seconds(end: datetime, start: datetime) -> float:
    if end.tzinfo is None and start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    elif end.tzinfo is not None and start.tzinfo is None:
        end = end.replace(tzinfo=None)
    return (end - start).total_seconds()


def _build_lightweight_recovery_payload(
    *,
    runtime_id: str,
    config_version: str,
    sleeve_ids: tuple[str, ...],
    routes: tuple[_OrderRuntimeRoute, ...],
    order_reports: tuple[Any, ...],
    journal_entries: tuple[CycleJournalEntry, ...],
    generated_at: datetime,
) -> dict[str, Any]:
    accounts: list[dict[str, Any]] = []
    all_blocked: list[str] = []
    all_actions: list[str] = []
    for route, report in zip(routes, order_reports):
        last_cycles = [
            entry
            for sleeve_id in sleeve_ids
            for entry in (_latest_cycle_entry(journal_entries, sleeve_id=sleeve_id, account_id=route.account_id),)
            if entry is not None
        ]
        if not last_cycles:
            last_cycles = [
                entry
                for sleeve_id in sleeve_ids
                for entry in (_latest_cycle_entry(journal_entries, sleeve_id=sleeve_id),)
                if entry is not None
            ]
        last_cycle = last_cycles[-1] if last_cycles else None
        blocked_reasons: list[str] = []
        if route.order_store_path is not None and not route.order_store_path.exists():
            blocked_reasons.append("order_runtime_store_missing")
        actions: list[str] = []
        if report.order_snapshot.open_tickets:
            actions.append("poll_open_tickets")
        if report.unallocated_fill_count:
            actions.append("allocate_unassigned_fills")
        if last_cycle is None:
            actions.append("run_runtime_once")
        elif last_cycle.snapshot_status in {"stale", "invalid"}:
            actions.append("refresh_snapshots")
        if blocked_reasons:
            actions.append("review_blocked_reasons")
        all_blocked.extend(blocked_reasons)
        all_actions.extend(actions)
        accounts.append(
            {
                "broker_account_id": route.account_id,
                "market_scope": route.market_scope,
                "order_store_path": str(route.order_store_path),
                "account_store_path": str(route.account_store_path),
                "last_cycle": _compact_cycle_entry(last_cycle.to_dict()) if last_cycle is not None else None,
                "open_ticket_count": len(report.order_snapshot.open_tickets),
                "unallocated_fill_count": report.unallocated_fill_count,
                "account_reconciliation": {
                    "status": "not_checked",
                    "reason": "broker holdings are not fetched by recovery report",
                },
                "blocked_reasons": list(dict.fromkeys(blocked_reasons)),
                "recommended_next_actions": list(dict.fromkeys(actions)),
                "order_runtime": _compact_order_route(report.to_dict(include_details=False)),
            }
        )
    blocked = list(dict.fromkeys(all_blocked))
    actions = list(dict.fromkeys(all_actions))
    return {
        "status": "blocked" if blocked else "needs_attention" if actions else "ok",
        "generated_at": generated_at.isoformat(),
        "runtime_id": runtime_id,
        "config_version": config_version,
        "sleeve_ids": list(sleeve_ids),
        "blocked_reasons": blocked,
        "recommended_next_actions": actions,
        "accounts": accounts,
    }


def _summary_payload(
    order_routes: Sequence[Mapping[str, Any]],
    health_routes: Sequence[Mapping[str, Any]],
    recovery: Mapping[str, Any],
) -> dict[str, Any]:
    statuses = {str(route.get("status") or "") for route in health_routes}
    health_status = "critical" if "critical" in statuses else "needs_attention" if "needs_attention" in statuses else "ok"
    open_ticket_count = sum(int(route.get("order_runtime", {}).get("open_ticket_count") or 0) for route in order_routes)
    unallocated_fill_count = sum(int(route.get("virtual_account", {}).get("unallocated_fill_count") or 0) for route in order_routes)
    sleeve_count = len({sleeve.get("sleeve_id") for route in order_routes for sleeve in route.get("sleeves", [])})
    warning_count = sum(len(route.get("warnings") or []) for route in order_routes)
    return {
        "status": "blocked" if recovery.get("status") == "blocked" else health_status,
        "health_status": health_status,
        "recovery_status": recovery.get("status"),
        "sleeve_count": sleeve_count,
        "route_count": len(order_routes),
        "open_ticket_count": open_ticket_count,
        "unallocated_fill_count": unallocated_fill_count,
        "warning_count": warning_count,
        "recommended_next_actions": list(recovery.get("recommended_next_actions") or []),
    }


def _portfolio_from_payload(payload: Mapping[str, Any], *, default_currency: str) -> Portfolio:
    cash_by_currency = {
        str(currency or default_currency).upper(): float(amount or 0.0)
        for currency, amount in dict(payload.get("cash_by_currency") or {}).items()
        if abs(float(amount or 0.0)) > 1e-12
    }
    if not cash_by_currency:
        cash = float(payload.get("cash") or 0.0)
        cash_by_currency = {default_currency: cash} if cash else {}
    holdings: dict[str, Holding] = {}
    for symbol_key, raw in dict(payload.get("holdings") or {}).items():
        if not isinstance(raw, Mapping):
            continue
        symbol = _symbol_from_payload(raw.get("symbol"), str(symbol_key))
        holdings[symbol.key] = Holding(
            symbol=symbol,
            quantity=int(raw.get("quantity") or 0),
            average_price=float(raw.get("average_price") or 0.0),
        )
    return Portfolio(cash=sum(cash_by_currency.values()), holdings=holdings, cash_by_currency=cash_by_currency)


def _symbol_from_payload(payload: Any, fallback_key: str) -> Symbol:
    if isinstance(payload, Mapping):
        return Symbol(str(payload.get("ticker") or ""), str(payload.get("market") or "KR"))
    if ":" in fallback_key:
        market, ticker = fallback_key.split(":", 1)
        return Symbol(ticker, market)
    return Symbol(fallback_key, "KR")


def _compact_recovery_payload(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    accounts = []
    for account in compact.get("accounts") or []:
        row = dict(account)
        if isinstance(row.get("last_cycle"), dict):
            row["last_cycle"] = _compact_cycle_entry(row["last_cycle"])
        if isinstance(row.get("order_runtime"), dict):
            row["order_runtime"] = _compact_order_route(row["order_runtime"])
        accounts.append(row)
    compact["accounts"] = accounts
    return compact


def _compact_order_route(payload: dict[str, Any]) -> dict[str, Any]:
    compact = dict(payload)
    runtime = dict(compact.get("order_runtime") or {})
    runtime["recent_events"] = [_compact_order_event(event) for event in runtime.get("recent_events") or []]
    runtime["open_tickets"] = [_compact_order_ticket(ticket) for ticket in runtime.get("open_tickets") or []]
    runtime.pop("events", None)
    runtime.pop("tickets", None)
    compact["order_runtime"] = runtime
    compact["sleeves"] = [
        {
            **dict(sleeve),
            "recent_events": [_compact_order_event(event) for event in sleeve.get("recent_events") or []],
            "open_tickets": [_compact_order_ticket(ticket) for ticket in sleeve.get("open_tickets") or []],
        }
        for sleeve in compact.get("sleeves") or []
    ]
    return compact


def _compact_order_event(event: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "event_id",
        "ticket_id",
        "order_intent_id",
        "sleeve_id",
        "symbol",
        "side",
        "event_type",
        "occurred_at",
        "quantity",
        "fill_price",
        "notional",
        "reason",
    )
    return {key: event.get(key) for key in keys if key in event}


def _compact_order_ticket(ticket: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "ticket_id",
        "order_intent_id",
        "sleeve_id",
        "symbol",
        "side",
        "quantity",
        "filled_quantity",
        "remaining_quantity",
        "status",
        "order_type",
        "reference_price",
        "limit_price",
        "created_at",
        "updated_at",
        "remaining_notional",
    )
    return {key: ticket.get(key) for key in keys if key in ticket}


def _compact_cycle_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    compact = dict(entry)
    metadata = dict(compact.get("metadata") or {})
    compact["metadata"] = {
        key: metadata[key]
        for key in ("engine_source_hash", "runtime_fingerprint", "coarse_universe_id", "active_universe_id")
        if key in metadata
    }
    return compact


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LEaps Operator UI</title>
  <link rel="stylesheet" href="/assets/styles.css">
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div>
        <p class="eyebrow">LEapsQuantEngine</p>
        <h1 id="runtime-title">Operator Console</h1>
      </div>
      <div class="top-actions">
        <span id="snapshot-badge" class="badge neutral">Snapshot only</span>
        <button id="refresh-button" type="button">Refresh</button>
      </div>
    </header>

    <section id="status-strip" class="status-strip"></section>

    <section class="layout">
      <div class="main-column">
        <section class="panel">
          <div class="panel-head">
            <h2>Sleeves</h2>
            <span id="sleeve-count" class="muted"></span>
          </div>
          <div id="sleeves" class="sleeve-grid"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Open Tickets</h2>
            <span id="ticket-count" class="muted"></span>
          </div>
          <div id="open-tickets" class="table-wrap"></div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <h2>Cycle Journal</h2>
            <span id="journal-path" class="muted"></span>
          </div>
          <div id="cycle-journal" class="journal-list"></div>
        </section>
      </div>

      <aside class="side-column">
        <section class="panel">
          <h2>Market Sessions</h2>
          <div id="market-sessions" class="stack"></div>
        </section>

        <section class="panel">
          <h2>Warnings</h2>
          <div id="warnings" class="stack"></div>
        </section>

        <section class="panel">
          <h2>Recent Events</h2>
          <div id="recent-events" class="stack"></div>
        </section>
      </aside>
    </section>
  </main>
  <script src="/assets/app.js"></script>
</body>
</html>
"""


STYLES_CSS = """
:root {
  color-scheme: light;
  --bg: #f4f6f8;
  --surface: #ffffff;
  --ink: #19212a;
  --muted: #657282;
  --line: #d8dee6;
  --ok: #197a55;
  --warn: #a26200;
  --bad: #b42318;
  --accent: #2f6fed;
  --accent-soft: #e7efff;
  --green-soft: #e8f5ef;
  --amber-soft: #fff4df;
  --red-soft: #fdeceb;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Segoe UI", Arial, sans-serif;
}

.shell {
  width: min(1440px, 100%);
  margin: 0 auto;
  padding: 20px;
}

.topbar,
.panel,
.metric,
.sleeve-card,
.event-row {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
}

.eyebrow {
  margin: 0 0 4px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

h1,
h2,
h3,
p {
  margin: 0;
}

h1 {
  font-size: 28px;
  line-height: 1.2;
}

h2 {
  font-size: 16px;
  line-height: 1.3;
}

h3 {
  font-size: 15px;
  line-height: 1.35;
}

.top-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

button {
  min-height: 36px;
  border: 1px solid #1f58c7;
  border-radius: 6px;
  background: var(--accent);
  color: #fff;
  padding: 0 14px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
}

button:focus-visible {
  outline: 3px solid #9dbbff;
  outline-offset: 2px;
}

.badge {
  display: inline-flex;
  align-items: center;
  min-height: 28px;
  padding: 0 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.badge.ok {
  background: var(--green-soft);
  color: var(--ok);
}

.badge.warn {
  background: var(--amber-soft);
  color: var(--warn);
}

.badge.bad {
  background: var(--red-soft);
  color: var(--bad);
}

.badge.neutral {
  background: var(--accent-soft);
  color: #164aa8;
}

.muted {
  color: var(--muted);
  font-size: 13px;
}

.status-strip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin: 14px 0;
}

.metric {
  min-height: 86px;
  padding: 14px;
}

.metric-label {
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
}

.metric-value {
  margin-top: 8px;
  font-size: 24px;
  font-weight: 800;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 14px;
}

.main-column,
.side-column,
.stack,
.journal-list {
  display: grid;
  gap: 12px;
}

.panel {
  padding: 16px;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.sleeve-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.sleeve-card,
.event-row {
  padding: 14px;
}

.sleeve-meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.label {
  color: var(--muted);
  font-size: 12px;
}

.value {
  margin-top: 3px;
  font-weight: 800;
}

.table-wrap {
  overflow: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

th,
td {
  border-bottom: 1px solid var(--line);
  padding: 10px 8px;
  text-align: left;
  vertical-align: top;
}

th {
  color: var(--muted);
  font-size: 12px;
  text-transform: uppercase;
}

.empty {
  min-height: 48px;
  display: flex;
  align-items: center;
  color: var(--muted);
}

.kv {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 8px;
  align-items: center;
  border-bottom: 1px solid var(--line);
  padding: 10px 0;
}

.kv:last-child {
  border-bottom: 0;
}

.error-box {
  border: 1px solid #f1a39d;
  background: var(--red-soft);
  color: var(--bad);
  border-radius: 8px;
  padding: 14px;
}

@media (max-width: 1000px) {
  .status-strip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .layout {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 640px) {
  .shell {
    padding: 12px;
  }

  .topbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .status-strip,
  .sleeve-grid,
  .sleeve-meta {
    grid-template-columns: 1fr;
  }

  h1 {
    font-size: 22px;
  }
}
"""


APP_JS = """
const state = {
  snapshot: null
};

const $ = (id) => document.getElementById(id);

async function loadSnapshot() {
  setBusy(true);
  try {
    const response = await fetch('/api/snapshot', { cache: 'no-store' });
    if (!response.ok) {
      throw new Error(`Snapshot request failed: ${response.status}`);
    }
    state.snapshot = await response.json();
    render(state.snapshot);
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(false);
  }
}

function setBusy(isBusy) {
  const button = $('refresh-button');
  button.disabled = isBusy;
  button.textContent = isBusy ? 'Refreshing' : 'Refresh';
}

function render(snapshot) {
  const runtime = snapshot.runtime || {};
  $('runtime-title').textContent = `${runtime.runtime_id || 'Runtime'} / ${runtime.mode || 'unknown'}`;
  $('snapshot-badge').textContent = snapshot.source?.snapshot_only ? 'Snapshot only' : 'Live source';
  renderStatusStrip(snapshot);
  renderSleeves(snapshot);
  renderOpenTickets(snapshot);
  renderMarketSessions(snapshot);
  renderWarnings(snapshot);
  renderEvents(snapshot);
  renderJournal(snapshot);
}

function renderStatusStrip(snapshot) {
  const summary = snapshot.summary || {};
  const items = [
    ['Status', summary.status || 'unknown'],
    ['Open tickets', number(summary.open_ticket_count)],
    ['Unallocated fills', number(summary.unallocated_fill_count)],
    ['Sleeves', number(summary.sleeve_count)],
    ['Generated', formatTime(snapshot.generated_at)]
  ];
  $('status-strip').innerHTML = items.map(([label, value]) => `
    <article class="metric">
      <div class="metric-label">${escapeHtml(label)}</div>
      <div class="metric-value">${escapeHtml(String(value))}</div>
    </article>
  `).join('');
}

function renderSleeves(snapshot) {
  const sleeves = allSleeves(snapshot);
  $('sleeve-count').textContent = `${sleeves.length} route views`;
  if (!sleeves.length) {
    $('sleeves').innerHTML = empty('No sleeve snapshot');
    return;
  }
  $('sleeves').innerHTML = sleeves.map((sleeve) => {
    const portfolio = sleeve.portfolio || {};
    return `
      <article class="sleeve-card">
        <div class="panel-head">
          <div>
            <h3>${escapeHtml(sleeve.sleeve_id || 'unknown')}</h3>
            <div class="muted">${escapeHtml(sleeve.route_label || '')}</div>
          </div>
          ${statusBadge(sleeve.open_ticket_count ? 'warn' : 'ok', sleeve.open_ticket_count ? 'Open' : 'Clear')}
        </div>
        <div class="sleeve-meta">
          ${miniValue('Cash', formatMoneyMap(portfolio.cash_by_currency, portfolio.cash))}
          ${miniValue('Holdings', number(portfolio.holding_count))}
          ${miniValue('Pending buy', money(sleeve.route_currency || '', sleeve.pending_buy_notional || 0))}
        </div>
        <div class="sleeve-meta">
          ${miniValue('Open tickets', number(sleeve.open_ticket_count))}
          ${miniValue('Terminal', number(sleeve.terminal_ticket_count))}
          ${miniValue('Events', number(sleeve.recent_event_count))}
        </div>
      </article>
    `;
  }).join('');
}

function renderOpenTickets(snapshot) {
  const tickets = allOpenTickets(snapshot);
  $('ticket-count').textContent = `${tickets.length} open`;
  if (!tickets.length) {
    $('open-tickets').innerHTML = empty('No open tickets');
    return;
  }
  $('open-tickets').innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Sleeve</th>
          <th>Symbol</th>
          <th>Side</th>
          <th>Qty</th>
          <th>Status</th>
          <th>Created</th>
        </tr>
      </thead>
      <tbody>
        ${tickets.map((ticket) => `
          <tr>
            <td>${escapeHtml(ticket.sleeve_id || '')}</td>
            <td>${escapeHtml(symbolText(ticket.symbol))}</td>
            <td>${escapeHtml(ticket.side || '')}</td>
            <td>${number(ticket.remaining_quantity ?? ticket.quantity)}</td>
            <td>${escapeHtml(ticket.status || '')}</td>
            <td>${escapeHtml(formatTime(ticket.created_at))}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function renderMarketSessions(snapshot) {
  const sessions = snapshot.market_sessions || {};
  const rows = Object.entries(sessions).map(([scope, report]) => {
    const session = report.session || {};
    return `
      <div class="kv">
        <div>
          <strong>${escapeHtml(scope)}</strong>
          <div class="muted">${escapeHtml(session.session_phase || 'unknown')}</div>
        </div>
        ${statusBadge(session.is_orderable ? 'ok' : 'neutral', session.is_orderable ? 'Orderable' : 'Closed')}
      </div>
    `;
  });
  $('market-sessions').innerHTML = rows.join('') || empty('No session snapshot');
}

function renderWarnings(snapshot) {
  const warnings = [
    ...(snapshot.warnings || []),
    ...((snapshot.summary || {}).recommended_next_actions || []).map((item) => `next:${item}`),
    ...((snapshot.recovery || {}).blocked_reasons || []).map((item) => `blocked:${item}`)
  ];
  $('warnings').innerHTML = warnings.length
    ? warnings.map((warning) => `<div class="event-row">${escapeHtml(warning)}</div>`).join('')
    : empty('No warnings');
}

function renderEvents(snapshot) {
  const events = allRecentEvents(snapshot).slice(0, 8);
  $('recent-events').innerHTML = events.length
    ? events.map((event) => `
      <div class="event-row">
        <strong>${escapeHtml(event.event_type || 'event')}</strong>
        <div class="muted">${escapeHtml(event.sleeve_id || '')} ${escapeHtml(symbolText(event.symbol))}</div>
        <div class="muted">${escapeHtml(formatTime(event.occurred_at))}</div>
      </div>
    `).join('')
    : empty('No recent events');
}

function renderJournal(snapshot) {
  const journal = snapshot.cycle_journal || {};
  $('journal-path').textContent = displayPath(journal.path) || 'not configured';
  const latest = Object.entries(journal.latest_by_sleeve || {});
  $('cycle-journal').innerHTML = latest.length
    ? latest.map(([sleeveId, entry]) => {
      if (!entry) {
        return `<div class="event-row"><strong>${escapeHtml(sleeveId)}</strong><div class="muted">No journal entry</div></div>`;
      }
      return `
        <div class="event-row">
          <strong>${escapeHtml(sleeveId)} / ${escapeHtml(entry.status || 'unknown')}</strong>
          <div class="muted">${escapeHtml(formatTime(entry.generated_at))}</div>
          <div class="muted">snapshot ${escapeHtml(entry.snapshot_status || 'unknown')}</div>
        </div>
      `;
    }).join('')
    : empty('No journal configured');
}

function renderError(error) {
  $('status-strip').innerHTML = `<div class="error-box">${escapeHtml(error.message || String(error))}</div>`;
}

function allSleeves(snapshot) {
  return (snapshot.order_routes || []).flatMap((route) => {
    const label = route.broker_account_id || route.market_scope || route.currency || 'default route';
    return (route.sleeves || []).map((sleeve) => ({
      ...sleeve,
      route_label: label,
      route_currency: route.currency
    }));
  });
}

function allOpenTickets(snapshot) {
  return (snapshot.order_routes || []).flatMap((route) => route.order_runtime?.open_tickets || []);
}

function allRecentEvents(snapshot) {
  return (snapshot.order_routes || []).flatMap((route) => route.order_runtime?.recent_events || []);
}

function miniValue(label, value) {
  return `<div><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(String(value))}</div></div>`;
}

function statusBadge(kind, text) {
  return `<span class="badge ${kind}">${escapeHtml(text)}</span>`;
}

function empty(text) {
  return `<div class="empty">${escapeHtml(text)}</div>`;
}

function symbolText(symbol) {
  if (!symbol) return '';
  if (typeof symbol === 'string') return symbol;
  return `${symbol.market || ''}:${symbol.ticker || symbol.symbol || ''}`;
}

function formatMoneyMap(cashByCurrency, fallback) {
  const entries = Object.entries(cashByCurrency || {});
  if (!entries.length) return money('', fallback || 0);
  return entries.map(([currency, amount]) => money(currency, amount)).join(' / ');
}

function money(currency, value) {
  const amount = Number(value || 0);
  return `${currency ? `${currency} ` : ''}${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function number(value) {
  return Number(value || 0).toLocaleString();
}

function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleString();
}

function displayPath(value) {
  if (!value) return '';
  return String(value).split(/[\\\\/]/).pop();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

$('refresh-button').addEventListener('click', loadSnapshot);
loadSnapshot();
"""

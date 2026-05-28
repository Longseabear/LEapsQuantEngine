from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from leaps_quant_engine.broker_routing import configured_account_ids_for_sleeve, currency_for_market_scope
from leaps_quant_engine.cycle_journal import CycleJournalEntry
from leaps_quant_engine.market_data_snapshot import FileMarketDataSnapshotStore, QUOTE_SNAPSHOT_LANE
from leaps_quant_engine.market_calendar import session_report_for_market_scope
from leaps_quant_engine.models import OrderSide, Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.operator_status import CashAvailabilityRouteInput, build_cash_availability_report
from leaps_quant_engine.performance import build_sleeve_daily_performance_report
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_bootstrap import resolve_runtime_path
from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot, load_runtime_config_snapshot
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.virtual_account import (
    FillAllocation,
    FillAllocationStatus,
    IgnoredBrokerFill,
    PortfolioMutationRecord,
    VirtualFillEvent,
)


OPERATOR_DASHBOARD_SCHEMA_VERSION = "operator_dashboard_snapshot.v1"
OPERATOR_UI_ASSET_VERSION = "operator-ui-summary-visuals-v19-daily-return-chart"
COMMON_SYMBOL_DISPLAY_NAMES = {
    "KRX:005290": "동진쎄미켐",
    "KRX:005930": "삼성전자",
    "KRX:011070": "LG이노텍",
    "KRX:017670": "SK텔레콤",
    "KRX:036930": "주성엔지니어링",
    "KRX:055550": "신한지주",
    "KRX:095610": "테스",
    "KRX:100790": "미래에셋벤처투자",
    "KRX:105560": "KB금융",
    "KRX:178320": "서진시스템",
    "KRX:329180": "HD현대중공업",
    "KRX:353200": "대덕전자",
    "KRX:440110": "파두",
    "US:QQQ": "Invesco QQQ Trust",
    "US:SMH": "VanEck Semiconductor ETF",
    "US:XLE": "Energy Select Sector SPDR Fund",
    "US:XLK": "Technology Select Sector SPDR Fund",
}
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

    def portfolio_mutations(
        self,
        *,
        sleeve_id: str | None = None,
        symbol_key: str | None = None,
        limit: int | None = None,
    ) -> tuple[PortfolioMutationRecord, ...]:
        state = self._load_state()
        mutations = [
            PortfolioMutationRecord.from_dict(raw)
            for raw in dict(state.get("portfolio_mutations") or {}).values()
            if isinstance(raw, dict)
        ]
        if sleeve_id is not None:
            mutations = [mutation for mutation in mutations if mutation.sleeve_id == sleeve_id]
        if symbol_key is not None:
            mutations = [mutation for mutation in mutations if mutation.symbol.key == symbol_key]
        mutations.sort(key=lambda mutation: (mutation.applied_at or datetime.min, mutation.fill_id))
        if limit == 0:
            return ()
        if limit is not None and limit > 0:
            mutations = mutations[-limit:]
        return tuple(mutations)

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
    symbol_names = _symbol_display_names(snapshot, selected_sleeve_ids)
    sleeve_display_names = _sleeve_display_names(snapshot, selected_sleeve_ids)
    base_order_payloads = [
        _compact_order_route(
            report.to_dict(include_details=include_details),
            symbol_names=symbol_names,
            sleeve_display_names=sleeve_display_names,
        )
        for report in order_reports
    ]
    daily_performance = _daily_performance_payload(snapshot, selected_sleeve_ids)
    current_estimates = _current_estimates_payload(
        snapshot,
        selected_sleeve_ids,
        generated_at=generated_at,
        symbol_names=symbol_names,
        max_age_seconds=max_cycle_age_seconds,
        order_routes=base_order_payloads,
        daily_performance=daily_performance,
    )
    order_payloads = _attach_current_estimates_to_order_routes(base_order_payloads, current_estimates)
    cash_availability = _cash_availability_payload(
        snapshot,
        selected_sleeve_ids,
        routes=routes,
        generated_at=generated_at,
    )
    warnings = tuple(
        dict.fromkeys(
            [
                *[warning for report in order_reports for warning in report.warnings],
                *list(cash_availability.get("warnings") or []),
            ]
        )
    )
    summary = _summary_payload(order_payloads, health_payloads, recovery_payload)
    summary["warning_count"] = int(summary.get("warning_count") or 0) + len(cash_availability.get("warnings") or [])
    if cash_availability.get("needs_attention") and summary.get("status") == "ok":
        summary["status"] = "needs_attention"
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
        "symbol_names": symbol_names,
        "sleeve_display_names": sleeve_display_names,
        "summary": summary,
        "market_sessions": {
            scope: session_report_for_market_scope(scope, now=generated_at).to_dict()
            for scope in market_scopes
        },
        "health_routes": health_payloads,
        "recovery": recovery_payload,
        "order_routes": order_payloads,
        "current_estimates": current_estimates,
        "cash_availability": cash_availability,
        "daily_performance": daily_performance,
        "strategy_docs": _strategy_docs_payload(snapshot, selected_sleeve_ids),
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
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
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
    route_sleeve_ids = _sleeve_ids_for_order_route(snapshot, route, sleeve_ids)
    return build_order_runtime_status(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=route_sleeve_ids,
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


def _sleeve_ids_for_order_route(
    snapshot: RuntimeConfigSnapshot,
    route: _OrderRuntimeRoute,
    sleeve_ids: tuple[str, ...],
) -> tuple[str, ...]:
    if not route.account_id:
        return sleeve_ids
    return tuple(
        sleeve_id
        for sleeve_id in sleeve_ids
        if route.account_id in configured_account_ids_for_sleeve(snapshot.config.sleeve(sleeve_id))
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


def _cash_availability_payload(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    *,
    routes: Sequence[_OrderRuntimeRoute],
    generated_at: datetime,
) -> dict[str, Any]:
    return build_cash_availability_report(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=sleeve_ids,
        generated_at=generated_at,
        routes=tuple(
            CashAvailabilityRouteInput(
                account_id=route.account_id,
                market_scope=route.market_scope,
                currency=route.currency,
                account_store_path=route.account_store_path,
                default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
            )
            for route in routes
        ),
    )


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


def _daily_performance_payload(snapshot: RuntimeConfigSnapshot, sleeve_ids: tuple[str, ...]) -> dict[str, Any]:
    root = _resolve_operator_data_path(snapshot, Path("data/eod-snapshots"))
    try:
        report = build_sleeve_daily_performance_report(root, sleeve_ids=sleeve_ids)
    except Exception as exc:  # pragma: no cover - defensive UI path.
        return {
            "snapshot_root": str(root),
            "row_count": 0,
            "summary_count": 0,
            "summaries": [],
            "latest_by_sleeve_currency": {},
            "warnings": [f"daily_performance_unavailable:{exc}"],
        }

    summaries = list(report.summaries())
    summary_by_key = {
        (str(summary.get("sleeve_id") or ""), str(summary.get("currency") or "").upper()): summary
        for summary in summaries
    }
    latest_by_sleeve_currency: dict[str, dict[str, Any]] = {}
    history_by_sleeve_currency: dict[str, list[dict[str, Any]]] = {}
    for row in sorted(report.rows, key=lambda item: (item.snapshot.sleeve_id, item.snapshot.currency, item.snapshot.date, item.snapshot.as_of)):
        payload = row.to_dict(include_holdings=True)
        key = (row.snapshot.sleeve_id, row.snapshot.currency)
        summary = summary_by_key.get(key, {})
        payload["period_return"] = summary.get("period_return")
        payload["period_pnl"] = summary.get("period_pnl")
        key_text = f"{key[0]}:{key[1]}"
        latest_by_sleeve_currency[key_text] = payload
        history_by_sleeve_currency.setdefault(key_text, []).append(payload)
    return {
        "snapshot_root": str(root),
        "row_count": len(report.rows),
        "summary_count": len(summaries),
        "summaries": summaries,
        "latest_by_sleeve_currency": latest_by_sleeve_currency,
        "history_by_sleeve_currency": history_by_sleeve_currency,
        "warnings": list(report.warnings),
    }


def _current_estimates_payload(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    *,
    generated_at: datetime,
    symbol_names: Mapping[str, str],
    max_age_seconds: float,
    order_routes: Sequence[Mapping[str, Any]],
    daily_performance: Mapping[str, Any],
) -> dict[str, Any]:
    ledger_routes = _account_ledger_routes(snapshot, order_routes)
    realized_pnl_by_sleeve = _realized_pnl_by_sleeve_from_order_routes(ledger_routes)
    cash_flows_by_sleeve = _cash_flows_by_sleeve_currency(ledger_routes)
    quote_estimates = _current_estimates_from_quote_store(
        snapshot,
        sleeve_ids,
        order_routes=order_routes,
        cash_flows_by_sleeve=cash_flows_by_sleeve,
        daily_performance=daily_performance,
        generated_at=generated_at,
        symbol_names=symbol_names,
        max_age_seconds=max_age_seconds,
        realized_pnl_by_sleeve=realized_pnl_by_sleeve,
    )
    fallback_estimates = _current_estimates_from_latest_run_artifacts(
        snapshot,
        sleeve_ids,
        generated_at=generated_at,
        symbol_names=symbol_names,
        max_age_seconds=max_age_seconds,
        realized_pnl_by_sleeve=realized_pnl_by_sleeve,
    )
    if quote_estimates is None:
        return fallback_estimates

    fallback_by_sleeve = fallback_estimates.get("latest_by_sleeve", {})
    merged_by_sleeve: dict[str, dict[str, Any]] = {}
    for sleeve_id in sleeve_ids:
        quote = dict(quote_estimates.get("latest_by_sleeve", {}).get(sleeve_id) or {})
        fallback = dict(fallback_by_sleeve.get(sleeve_id) or {})
        if quote.get("status") == "unavailable" and fallback.get("status") != "unavailable":
            merged_by_sleeve[sleeve_id] = fallback
        else:
            merged_by_sleeve[sleeve_id] = quote
    quote_estimates["latest_by_sleeve"] = merged_by_sleeve
    quote_estimates["fallback_source"] = fallback_estimates.get("source")
    return quote_estimates


def _current_estimates_from_latest_run_artifacts(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    *,
    generated_at: datetime,
    symbol_names: Mapping[str, str],
    max_age_seconds: float,
    realized_pnl_by_sleeve: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    latest_by_sleeve_path = _resolve_operator_data_path(
        snapshot,
        Path("data/runtime/live-order-loop/multi_sleeve_runtime_run_latest_by_sleeve.json"),
    )
    latest_run_path = _resolve_operator_data_path(
        snapshot,
        Path("data/runtime/live-order-loop/multi_sleeve_runtime_run_latest.json"),
    )

    latest_by_sleeve_payload = _read_json_object(latest_by_sleeve_path)
    if latest_by_sleeve_payload is not None:
        return _current_estimates_from_latest_by_sleeve_payload(
            latest_by_sleeve_payload,
            path=latest_by_sleeve_path,
            sleeve_ids=sleeve_ids,
            generated_at=generated_at,
            symbol_names=symbol_names,
            max_age_seconds=max_age_seconds,
            realized_pnl_by_sleeve=realized_pnl_by_sleeve,
        )

    latest_run_payload = _read_json_object(latest_run_path)
    if latest_run_payload is not None:
        return _current_estimates_from_runtime_run_payload(
            latest_run_payload,
            path=latest_run_path,
            sleeve_ids=sleeve_ids,
            generated_at=generated_at,
            symbol_names=symbol_names,
            max_age_seconds=max_age_seconds,
            realized_pnl_by_sleeve=realized_pnl_by_sleeve,
        )

    source = _current_estimate_source_payload(
        name="multi_sleeve_runtime_run_latest_by_sleeve",
        path=latest_by_sleeve_path,
        exists=latest_by_sleeve_path.is_file(),
        max_age_seconds=max_age_seconds,
    )
    return {
        "source": source,
        "latest_by_sleeve": {
            sleeve_id: _unavailable_current_estimate(
                sleeve_id,
                path=latest_by_sleeve_path,
                source_name=str(source["name"]),
                reason="current_estimate_snapshot_missing",
            )
            for sleeve_id in sleeve_ids
        },
        "warnings": ["current_estimate_snapshot_missing"],
    }


def _current_estimates_from_latest_by_sleeve_payload(
    payload: Mapping[str, Any],
    *,
    path: Path,
    sleeve_ids: tuple[str, ...],
    generated_at: datetime,
    symbol_names: Mapping[str, str],
    max_age_seconds: float,
    realized_pnl_by_sleeve: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source_name = "multi_sleeve_runtime_run_latest_by_sleeve"
    entries = payload.get("latest_by_sleeve") if isinstance(payload.get("latest_by_sleeve"), Mapping) else {}
    latest_by_sleeve: dict[str, dict[str, Any]] = {}
    for sleeve_id in sleeve_ids:
        entry = entries.get(sleeve_id) if isinstance(entries, Mapping) else None
        if not isinstance(entry, Mapping):
            latest_by_sleeve[sleeve_id] = _unavailable_current_estimate(
                sleeve_id,
                path=path,
                source_name=source_name,
                reason="current_estimate_not_in_latest_by_sleeve",
            )
            continue
        report = entry.get("report") if isinstance(entry.get("report"), Mapping) else None
        current = _current_state_from_runtime_report(report or {})
        fallback_as_of = _parse_datetime_or_none(
            entry.get("updated_at")
            or entry.get("source_run_completed_at")
            or payload.get("generated_at")
        )
        if current is None:
            latest_by_sleeve[sleeve_id] = _unavailable_current_estimate(
                sleeve_id,
                path=path,
                source_name=source_name,
                reason="current_estimate_state_missing",
            )
            continue
        latest_by_sleeve[sleeve_id] = _current_estimate_from_state(
            sleeve_id,
            current,
            path=path,
            source_name=source_name,
            generated_at=generated_at,
            fallback_as_of=fallback_as_of,
            max_age_seconds=max_age_seconds,
            symbol_names=symbol_names,
            realized_pnl=realized_pnl_by_sleeve.get(sleeve_id, {}),
        )
    source = _current_estimate_source_from_estimates(
        name=source_name,
        path=path,
        exists=True,
        generated_at=generated_at,
        max_age_seconds=max_age_seconds,
        estimates=latest_by_sleeve.values(),
    )
    return {
        "source": source,
        "latest_by_sleeve": latest_by_sleeve,
        "warnings": [],
    }


def _current_estimates_from_runtime_run_payload(
    payload: Mapping[str, Any],
    *,
    path: Path,
    sleeve_ids: tuple[str, ...],
    generated_at: datetime,
    symbol_names: Mapping[str, str],
    max_age_seconds: float,
    realized_pnl_by_sleeve: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source_name = "multi_sleeve_runtime_run_latest"
    source: dict[str, Any] = {
        "name": source_name,
        "path": str(path),
        "exists": path.is_file(),
        "status": "unavailable",
        "as_of": None,
        "age_seconds": None,
        "freshness_threshold_seconds": max_age_seconds,
    }
    unavailable = {
            sleeve_id: _unavailable_current_estimate(
                sleeve_id,
                path=path,
                source_name=source_name,
                reason="current_estimate_snapshot_missing",
            )
            for sleeve_id in sleeve_ids
    }
    if not path.is_file():
        return {
            "source": source,
            "latest_by_sleeve": unavailable,
            "warnings": ["current_estimate_snapshot_missing"],
        }

    source_as_of = _parse_datetime_or_none(
        payload.get("completed_at") or payload.get("generated_at") or payload.get("started_at")
    )
    source_age = _elapsed_seconds(generated_at, source_as_of) if source_as_of is not None else None
    source_status = "stale" if source_age is not None and source_age > max_age_seconds else "fresh"
    source.update(
        {
            "status": source_status,
            "as_of": source_as_of.isoformat() if source_as_of is not None else None,
            "age_seconds": source_age,
        }
    )

    latest_by_sleeve: dict[str, dict[str, Any]] = {}
    for report in payload.get("reports") or []:
        if not isinstance(report, Mapping):
            continue
        sleeve_id = str(report.get("sleeve_id") or "").strip()
        if sleeve_id not in sleeve_ids:
            continue
        current = _current_state_from_runtime_report(report)
        if current is None:
            continue
        latest_by_sleeve[sleeve_id] = _current_estimate_from_state(
            sleeve_id,
            current,
            path=path,
            source_name=source_name,
            generated_at=generated_at,
            fallback_as_of=source_as_of,
            max_age_seconds=max_age_seconds,
            symbol_names=symbol_names,
            realized_pnl=realized_pnl_by_sleeve.get(sleeve_id, {}),
        )

    for sleeve_id in sleeve_ids:
        latest_by_sleeve.setdefault(
            sleeve_id,
            _unavailable_current_estimate(
                sleeve_id,
                path=path,
                source_name=source_name,
                reason="current_estimate_not_in_latest_cycle",
            ),
        )
    return {
        "source": source,
        "latest_by_sleeve": latest_by_sleeve,
        "warnings": [],
    }


def _current_estimates_from_quote_store(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: tuple[str, ...],
    *,
    order_routes: Sequence[Mapping[str, Any]],
    cash_flows_by_sleeve: Mapping[str, Mapping[str, float]],
    daily_performance: Mapping[str, Any],
    generated_at: datetime,
    symbol_names: Mapping[str, str],
    max_age_seconds: float,
    realized_pnl_by_sleeve: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any] | None:
    store_path = _market_data_snapshot_store_path(snapshot)
    records = FileMarketDataSnapshotStore(store_path).entries(limit=1000, lane=QUOTE_SNAPSHOT_LANE)
    if not records:
        return None
    latest_record = max(records, key=lambda item: item.snapshot.time)
    quote_snapshot = latest_record.snapshot
    source_name = "market_data_snapshot_store_quote"
    source_as_of = quote_snapshot.time
    source_age = _elapsed_seconds(generated_at, source_as_of)
    source_status = "stale" if source_age > max_age_seconds else "fresh"
    quote_by_symbol = _latest_quote_bars_by_symbol(
        records,
        generated_at=generated_at,
        max_age_seconds=max_age_seconds,
    )
    target_plans = _target_plans_by_sleeve(snapshot, sleeve_ids)
    latest_by_sleeve: dict[str, dict[str, Any]] = {}
    for sleeve_id in sleeve_ids:
        latest_by_sleeve[sleeve_id] = _current_estimate_from_order_routes_and_quotes(
            sleeve_id,
            order_routes=order_routes,
            quote_by_symbol=quote_by_symbol,
            source_path=store_path,
            source_name=source_name,
            source_as_of=source_as_of,
            generated_at=generated_at,
            max_age_seconds=max_age_seconds,
            symbol_names=symbol_names,
            target_plans=target_plans.get(sleeve_id, {}),
            cash_flow_by_currency=cash_flows_by_sleeve.get(sleeve_id, {}),
            realized_pnl=realized_pnl_by_sleeve.get(sleeve_id, {}),
            daily_performance=daily_performance,
            source_status=source_status,
        )
    source = _current_estimate_source_from_estimates(
        name=source_name,
        path=store_path,
        exists=True,
        generated_at=generated_at,
        max_age_seconds=max_age_seconds,
        estimates=latest_by_sleeve.values(),
    )
    source.update(
        {
            "status": source_status,
            "as_of": source_as_of.isoformat(),
            "age_seconds": source_age,
            "lane": quote_snapshot.lane,
            "snapshot_id": quote_snapshot.snapshot_id,
            "merged_record_count": len(records),
        }
    )
    return {
        "source": source,
        "latest_by_sleeve": latest_by_sleeve,
        "warnings": [],
    }


def _latest_quote_bars_by_symbol(
    records: Sequence[Any],
    *,
    generated_at: datetime,
    max_age_seconds: float,
) -> dict[str, dict[str, Any]]:
    quote_by_symbol: dict[str, dict[str, Any]] = {}
    for record in reversed(records):
        snapshot = record.snapshot
        for symbol_key, bar in snapshot.bars.items():
            if symbol_key in quote_by_symbol or float(bar.close or 0.0) <= 0:
                continue
            bar_as_of = bar.time or snapshot.time
            quote_by_symbol[symbol_key] = {
                "price": float(bar.close),
                "as_of": bar_as_of,
                "source": snapshot.source,
                "metadata": dict(bar.metadata or {}),
                "snapshot_id": snapshot.snapshot_id,
            }
    return quote_by_symbol


def _market_data_snapshot_store_path(snapshot: RuntimeConfigSnapshot) -> Path:
    configured = snapshot.config.market_data.snapshot_store_path
    if configured is not None:
        return resolve_runtime_path(snapshot, configured).resolve()
    return _resolve_operator_data_path(
        snapshot,
        Path("data/market-data-snapshots") / f"{snapshot.config.runtime_id}.jsonl",
    )


def _current_estimate_from_order_routes_and_quotes(
    sleeve_id: str,
    *,
    order_routes: Sequence[Mapping[str, Any]],
    quote_by_symbol: Mapping[str, Mapping[str, Any]],
    source_path: Path,
    source_name: str,
    source_as_of: datetime,
    generated_at: datetime,
    max_age_seconds: float,
    symbol_names: Mapping[str, str],
    target_plans: Mapping[str, Mapping[str, Any]],
    cash_flow_by_currency: Mapping[str, float],
    realized_pnl: Mapping[str, Any],
    daily_performance: Mapping[str, Any],
    source_status: str,
) -> dict[str, Any]:
    cash_by_currency: dict[str, float] = {}
    holdings_by_symbol: dict[str, dict[str, Any]] = {}
    route_currencies: set[str] = set()
    for route in order_routes:
        route_currency = str(route.get("currency") or "").upper()
        if route_currency:
            route_currencies.add(route_currency)
        for sleeve in route.get("sleeves") or []:
            if not isinstance(sleeve, Mapping) or str(sleeve.get("sleeve_id") or "") != sleeve_id:
                continue
            portfolio = sleeve.get("portfolio") if isinstance(sleeve.get("portfolio"), Mapping) else {}
            route_cash = _money_map(portfolio.get("cash_by_currency"))
            if not route_cash and route_currency:
                route_cash = {route_currency: _float_or_zero(portfolio.get("cash"))}
            for currency, amount in route_cash.items():
                cash_by_currency[currency] = cash_by_currency.get(currency, 0.0) + amount
            for holding in portfolio.get("holdings") or []:
                if not isinstance(holding, Mapping):
                    continue
                symbol = _symbol_key_from_holding(holding)
                if not symbol:
                    continue
                row = holdings_by_symbol.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "label": _symbol_display_label(symbol, symbol_names),
                        "currency": route_currency or _currency_for_symbol_or_maps(symbol, cash_by_currency),
                        "quantity": 0.0,
                        "average_price": _float_or_zero(holding.get("average_price")),
                    },
                )
                row["quantity"] = _float_or_zero(row.get("quantity")) + _float_or_zero(holding.get("quantity"))
                if _float_or_zero(holding.get("average_price")) > 0:
                    row["average_price"] = _float_or_zero(holding.get("average_price"))

    if not holdings_by_symbol and not any(abs(amount) > 1e-12 for amount in cash_by_currency.values()) and not target_plans:
        return _unavailable_current_estimate(
            sleeve_id,
            path=source_path,
            source_name=source_name,
            reason="current_estimate_no_portfolio_or_target_state",
        )

    holdings: list[dict[str, Any]] = []
    missing_quote_symbols: list[str] = []
    for symbol, row in sorted(holdings_by_symbol.items()):
        quote = quote_by_symbol.get(symbol)
        plan = target_plans.get(symbol, {})
        market_price = _float_or_zero(quote.get("price") if quote else None)
        price_source = "quote"
        if market_price <= 0:
            market_price = _float_or_zero(plan.get("current_price"))
            price_source = "target_plan"
        if market_price <= 0:
            market_price = _float_or_zero(row.get("average_price"))
            price_source = "average_price_fallback"
            missing_quote_symbols.append(symbol)
        quantity = _float_or_zero(row.get("quantity"))
        average_price = _float_or_zero(row.get("average_price"))
        market_value = abs(quantity) * market_price
        cost_basis = abs(quantity) * average_price
        unrealized_pnl = market_value - cost_basis
        holdings.append(
            {
                **row,
                "quantity": quantity,
                "average_price": average_price,
                "market_price": market_price,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": (unrealized_pnl / cost_basis) if cost_basis > 0 else None,
                "price_source": price_source,
                "price_as_of": quote.get("as_of").isoformat() if quote and quote.get("as_of") is not None else None,
                "target_percent": plan.get("target_percent"),
                "target_value": plan.get("desired_value"),
                "target_quantity": _target_quantity_from_operator_plan(plan),
            }
        )

    stock_market_value_by_currency = _sum_holdings_by_currency(holdings, "market_value")
    cost_basis_by_currency = _sum_holdings_by_currency(holdings, "cost_basis")
    unrealized_pnl_by_currency = _sum_holdings_by_currency(holdings, "unrealized_pnl")
    realized_pnl_by_currency = _money_map(realized_pnl.get("realized_pnl_by_currency"))
    realized_cost_basis_by_currency = _money_map(realized_pnl.get("realized_cost_basis_by_currency"))
    realized_pnl_by_symbol = _symbol_money_map(realized_pnl.get("realized_pnl_by_symbol"))
    realized_cost_basis_by_symbol = _symbol_money_map(realized_pnl.get("realized_cost_basis_by_symbol"))
    total_pnl_by_currency = _add_money_maps(realized_pnl_by_currency, unrealized_pnl_by_currency)
    currencies = set(route_currencies) | set(cash_by_currency) | set(stock_market_value_by_currency) | set(cash_flow_by_currency)
    currencies |= set(realized_pnl_by_currency) | set(realized_cost_basis_by_currency)
    for plan in target_plans.values():
        symbol = str(plan.get("symbol") or "")
        if symbol:
            currencies.add(_currency_for_symbol_or_maps(symbol, cash_by_currency))
    equity_by_currency = {
        currency: cash_by_currency.get(currency, 0.0) + stock_market_value_by_currency.get(currency, 0.0)
        for currency in sorted(currencies)
    }
    book_value_by_currency = {
        currency: cash_by_currency.get(currency, 0.0) + cost_basis_by_currency.get(currency, 0.0)
        for currency in sorted(currencies)
    }
    total_pnl_cost_basis_by_currency = _total_pnl_return_basis_by_currency(
        book_value_by_currency,
        realized_cost_basis_by_currency,
    )
    pnl_since_eod = _pnl_since_eod_by_currency(
        sleeve_id,
        equity_by_currency=equity_by_currency,
        cash_flow_by_currency=cash_flow_by_currency,
        daily_performance=daily_performance,
    )
    return_since_eod = _return_since_eod_by_currency(
        sleeve_id,
        equity_by_currency=equity_by_currency,
        cash_flow_by_currency=cash_flow_by_currency,
        daily_performance=daily_performance,
    )
    total_returns, total_return_basis = _total_return_by_currency(
        total_pnl_by_currency=total_pnl_by_currency,
        total_pnl_cost_basis_by_currency=total_pnl_cost_basis_by_currency,
    )
    positions = _current_target_positions(
        holdings,
        target_plans=target_plans,
        equity_by_currency=equity_by_currency,
        quote_by_symbol=quote_by_symbol,
        symbol_names=symbol_names,
        realized_pnl_by_symbol=realized_pnl_by_symbol,
        realized_cost_basis_by_symbol=realized_cost_basis_by_symbol,
        daily_performance=daily_performance,
        sleeve_id=sleeve_id,
    )
    status = "stale" if source_status == "stale" or missing_quote_symbols else "fresh"
    estimate = {
        "sleeve_id": sleeve_id,
        "status": status,
        "as_of": source_as_of.isoformat(),
        "age_seconds": _elapsed_seconds(generated_at, source_as_of),
        "freshness_threshold_seconds": max_age_seconds,
        "source_name": source_name,
        "source_path": str(source_path),
        "cash_by_currency": _nonzero_money_map(cash_by_currency),
        "stock_market_value_by_currency": stock_market_value_by_currency,
        "equity_by_currency": _nonzero_money_map(equity_by_currency),
        "cost_basis_by_currency": cost_basis_by_currency,
        "book_value_by_currency": _nonzero_money_map(book_value_by_currency),
        "unrealized_pnl_by_currency": unrealized_pnl_by_currency,
        "realized_pnl_by_currency": realized_pnl_by_currency,
        "total_pnl_by_currency": total_pnl_by_currency,
        "realized_cost_basis_by_currency": realized_cost_basis_by_currency,
        "total_pnl_cost_basis_by_currency": total_pnl_cost_basis_by_currency,
        "realized_pnl_by_symbol": realized_pnl_by_symbol,
        "realized_cost_basis_by_symbol": realized_cost_basis_by_symbol,
        "net_cash_flow_by_currency": _nonzero_money_map(dict(cash_flow_by_currency)),
        "cash_transfer_return_by_currency": _return_by_currency(equity_by_currency, cash_flow_by_currency),
        "total_return_by_currency": total_returns,
        "total_return_basis_by_currency": total_return_basis,
        "pnl_since_eod_by_currency": pnl_since_eod,
        "return_since_eod_by_currency": return_since_eod,
        "holding_count": len(holdings),
        "holdings": holdings,
        "positions": positions,
        "target_count": len([position for position in positions if abs(_float_or_zero(position.get("target_percent"))) > 1e-12]),
        "missing_quote_symbols": missing_quote_symbols,
        "reason": "missing_quote_for_held_symbol" if missing_quote_symbols else "",
    }
    return estimate


def _current_target_positions(
    holdings: Sequence[Mapping[str, Any]],
    *,
    target_plans: Mapping[str, Mapping[str, Any]],
    equity_by_currency: Mapping[str, float],
    quote_by_symbol: Mapping[str, Mapping[str, Any]],
    symbol_names: Mapping[str, str],
    realized_pnl_by_symbol: Mapping[str, float] | None = None,
    realized_cost_basis_by_symbol: Mapping[str, float] | None = None,
    daily_performance: Mapping[str, Any] | None = None,
    sleeve_id: str = "",
) -> list[dict[str, Any]]:
    by_symbol = {str(holding.get("symbol") or ""): dict(holding) for holding in holdings if holding.get("symbol")}
    rows: list[dict[str, Any]] = []
    realized_pnl_by_symbol = realized_pnl_by_symbol or {}
    realized_cost_basis_by_symbol = realized_cost_basis_by_symbol or {}
    eod_holdings = _latest_eod_holdings_by_symbol(daily_performance or {}, sleeve_id=sleeve_id)
    for symbol in sorted(set(by_symbol) | set(target_plans) | set(realized_pnl_by_symbol)):
        holding = by_symbol.get(symbol, {})
        plan = dict(target_plans.get(symbol) or {})
        currency = str(holding.get("currency") or _currency_for_symbol_or_maps(symbol, equity_by_currency)).upper()
        equity = _float_or_zero(equity_by_currency.get(currency))
        market_value = _float_or_zero(holding.get("market_value"))
        current_percent = market_value / equity if equity > 0 else 0.0
        quote = quote_by_symbol.get(symbol)
        price = _float_or_zero(holding.get("market_price")) or _float_or_zero(quote.get("price") if quote else None) or _float_or_zero(plan.get("current_price"))
        target_percent = _float_or_zero(plan.get("target_percent"))
        target_value = plan.get("desired_value")
        if target_value is None and equity > 0:
            target_value = target_percent * equity
        unrealized_pnl = _float_or_zero(holding.get("unrealized_pnl"))
        realized_symbol_pnl = _float_or_zero(realized_pnl_by_symbol.get(symbol))
        realized_symbol_cost_basis = _float_or_zero(realized_cost_basis_by_symbol.get(symbol))
        cost_basis = _float_or_zero(holding.get("cost_basis"))
        total_pnl = unrealized_pnl + realized_symbol_pnl
        total_pnl_basis = cost_basis + realized_symbol_cost_basis
        eod_holding = eod_holdings.get(symbol, {})
        eod_market_value = _float_or_zero(eod_holding.get("market_value"))
        today_pnl = market_value - eod_market_value if eod_market_value > 0 else None
        today_pnl_pct = today_pnl / eod_market_value if today_pnl is not None and eod_market_value > 0 else None
        rows.append(
            {
                "symbol": symbol,
                "label": str(holding.get("label") or _symbol_display_label(symbol, symbol_names)),
                "currency": currency,
                "quantity": _float_or_zero(holding.get("quantity")),
                "market_price": price,
                "market_value": market_value,
                "current_percent": current_percent,
                "target_percent": target_percent,
                "target_value": _float_or_zero(target_value),
                "target_quantity": _target_quantity_from_operator_plan(plan),
                "delta_percent": target_percent - current_percent,
                "unrealized_pnl": unrealized_pnl,
                "realized_pnl": realized_symbol_pnl,
                "total_pnl": total_pnl,
                "total_pnl_pct": total_pnl / total_pnl_basis if total_pnl_basis > 0 else None,
                "today_pnl": today_pnl,
                "today_pnl_pct": today_pnl_pct,
                "held": symbol in by_symbol and abs(_float_or_zero(holding.get("quantity"))) > 1e-12,
                "targeted": abs(target_percent) > 1e-12,
                "reason": str(plan.get("reason") or ""),
                "tag": str(plan.get("tag") or ""),
            }
        )
    return sorted(rows, key=lambda row: (not row["held"], -abs(float(row.get("target_percent") or 0.0)), row["symbol"]))


def _latest_eod_holdings_by_symbol(
    daily_performance: Mapping[str, Any],
    *,
    sleeve_id: str,
) -> dict[str, Mapping[str, Any]]:
    latest = daily_performance.get("latest_by_sleeve_currency") if isinstance(daily_performance.get("latest_by_sleeve_currency"), Mapping) else {}
    result: dict[str, Mapping[str, Any]] = {}
    for key, row in latest.items():
        if not isinstance(row, Mapping):
            continue
        if str(row.get("sleeve_id") or key).split(":", 1)[0] != sleeve_id:
            continue
        for holding in row.get("holdings") or []:
            if not isinstance(holding, Mapping):
                continue
            symbol = str(holding.get("symbol") or "")
            if symbol:
                result[symbol] = holding
    return result


def _target_plans_by_sleeve(snapshot: RuntimeConfigSnapshot, sleeve_ids: tuple[str, ...]) -> dict[str, dict[str, dict[str, Any]]]:
    root = _resolve_operator_data_path(snapshot, Path("data/runtime/framework-state/multi-sleeve"))
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for sleeve_id in sleeve_ids:
        path = root / f"{re.sub(r'[^A-Za-z0-9._-]', '_', sleeve_id)}.json"
        payload = _read_json_object(path) or {}
        batch = payload.get("last_portfolio_target_batch") if isinstance(payload.get("last_portfolio_target_batch"), Mapping) else {}
        plans: dict[str, dict[str, Any]] = {}
        for plan in batch.get("plans") or []:
            if not isinstance(plan, Mapping):
                continue
            symbol = str(plan.get("symbol") or "").strip()
            if symbol:
                plans[symbol] = dict(plan)
        result[sleeve_id] = plans
    return result


def _cash_flows_by_sleeve_currency(order_routes: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    flows: dict[str, dict[str, float]] = {}
    seen_transfer_ids: set[str] = set()
    for route in order_routes:
        path = Path(str(route.get("account_store_path") or ""))
        payload = _read_json_object(path) or {}
        for transfer in dict(payload.get("cash_transfers") or {}).values():
            if not isinstance(transfer, Mapping):
                continue
            transfer_id = str(transfer.get("transfer_id") or "").strip()
            if transfer_id:
                if transfer_id in seen_transfer_ids:
                    continue
                seen_transfer_ids.add(transfer_id)
            currency = str(transfer.get("currency") or route.get("currency") or "").upper()
            amount = _float_or_zero(transfer.get("amount"))
            to_sleeve = str(transfer.get("to_sleeve_id") or "").strip()
            from_sleeve = str(transfer.get("from_sleeve_id") or "").strip()
            if to_sleeve:
                flows.setdefault(to_sleeve, {})[currency] = flows.setdefault(to_sleeve, {}).get(currency, 0.0) + amount
            if from_sleeve:
                flows.setdefault(from_sleeve, {})[currency] = flows.setdefault(from_sleeve, {}).get(currency, 0.0) - amount
    return flows


def _account_ledger_routes(
    snapshot: RuntimeConfigSnapshot,
    order_routes: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    routes: list[Mapping[str, Any]] = []
    seen_paths: set[Path] = set()
    for account in snapshot.config.broker_accounts:
        path = resolve_runtime_path(snapshot, account.account_store_path).resolve()
        if path in seen_paths:
            continue
        seen_paths.add(path)
        routes.append(
            {
                "account_id": account.account_id,
                "currency": account.currency,
                "account_store_path": str(path),
            }
        )
    for route in order_routes:
        path_text = str(route.get("account_store_path") or "")
        if not path_text:
            continue
        path = Path(path_text).resolve()
        if path in seen_paths:
            continue
        seen_paths.add(path)
        routes.append(route)
    return tuple(routes)


def _realized_pnl_by_sleeve_from_order_routes(order_routes: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    realized: dict[str, dict[str, Any]] = {}
    seen_paths: set[Path] = set()
    for route in order_routes:
        path = Path(str(route.get("account_store_path") or ""))
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        payload = _read_json_object(path) or {}
        for sleeve_id, sleeve_realized in _realized_pnl_by_sleeve_from_account_payload(payload).items():
            target = realized.setdefault(sleeve_id, _empty_realized_pnl_payload())
            _merge_money_map_in_place(
                target["realized_pnl_by_currency"],
                sleeve_realized.get("realized_pnl_by_currency", {}),
            )
            _merge_money_map_in_place(
                target["realized_cost_basis_by_currency"],
                sleeve_realized.get("realized_cost_basis_by_currency", {}),
            )
            _merge_money_map_in_place(
                target["realized_pnl_by_symbol"],
                sleeve_realized.get("realized_pnl_by_symbol", {}),
            )
            _merge_money_map_in_place(
                target["realized_cost_basis_by_symbol"],
                sleeve_realized.get("realized_cost_basis_by_symbol", {}),
            )
    return realized


def _realized_pnl_by_sleeve_from_account_payload(account_payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    fills = sorted(
        (
            fill
            for fill in dict(account_payload.get("fills") or {}).values()
            if isinstance(fill, Mapping)
        ),
        key=lambda fill: (str(fill.get("filled_at") or ""), str(fill.get("fill_id") or "")),
    )
    lots: dict[tuple[str, str], list[list[float]]] = {}
    realized: dict[str, dict[str, Any]] = {}
    for fill in fills:
        sleeve_id = str(fill.get("sleeve_id") or "").strip()
        symbol = _symbol_key_from_holding(fill)
        side = str(fill.get("side") or "").lower()
        quantity = abs(_float_or_zero(fill.get("quantity")))
        price = _float_or_zero(fill.get("fill_price"))
        if not sleeve_id or not symbol or quantity <= 0 or price <= 0:
            continue
        key = (sleeve_id, symbol)
        if side == "buy":
            lots.setdefault(key, []).append([quantity, price])
            continue
        if side != "sell":
            continue
        remaining = quantity
        fee = max(0.0, _float_or_zero(fill.get("fee")))
        while remaining > 1e-12 and lots.get(key):
            lot_quantity, lot_price = lots[key][0]
            matched = min(lot_quantity, remaining)
            fee_share = fee * (matched / quantity) if quantity > 0 else 0.0
            pnl = (price - lot_price) * matched - fee_share
            cost_basis = lot_price * matched
            currency = _currency_for_symbol_or_maps(symbol, {})
            _add_realized_pnl(
                realized.setdefault(sleeve_id, _empty_realized_pnl_payload()),
                currency=currency,
                symbol=symbol,
                pnl=pnl,
                cost_basis=cost_basis,
            )
            lot_quantity -= matched
            remaining -= matched
            if lot_quantity <= 1e-12:
                lots[key].pop(0)
            else:
                lots[key][0][0] = lot_quantity

    mutation_realized = _realized_pnl_by_sleeve_from_portfolio_mutations(account_payload)
    for sleeve_id, sleeve_realized in mutation_realized.items():
        if _has_realized_values(realized.get(sleeve_id, {})):
            continue
        realized[sleeve_id] = sleeve_realized
    return realized


def _realized_pnl_by_sleeve_from_portfolio_mutations(account_payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    realized: dict[str, dict[str, Any]] = {}
    for mutation in dict(account_payload.get("portfolio_mutations") or {}).values():
        if not isinstance(mutation, Mapping):
            continue
        pnl = _float_or_zero(mutation.get("realized_pnl_estimate"))
        if abs(pnl) <= 1e-12:
            continue
        sleeve_id = str(mutation.get("sleeve_id") or "").strip()
        symbol = str(mutation.get("symbol") or "").strip()
        if not sleeve_id:
            continue
        currency = str(mutation.get("currency") or _currency_for_symbol_or_maps(symbol, {}) or "").upper()
        _add_realized_pnl(
            realized.setdefault(sleeve_id, _empty_realized_pnl_payload()),
            currency=currency,
            symbol=symbol,
            pnl=pnl,
            cost_basis=0.0,
        )
    return realized


def _empty_realized_pnl_payload() -> dict[str, dict[str, float]]:
    return {
        "realized_pnl_by_currency": {},
        "realized_cost_basis_by_currency": {},
        "realized_pnl_by_symbol": {},
        "realized_cost_basis_by_symbol": {},
    }


def _add_realized_pnl(
    target: dict[str, dict[str, float]],
    *,
    currency: str,
    symbol: str,
    pnl: float,
    cost_basis: float,
) -> None:
    currency = str(currency or "").upper()
    if currency:
        target["realized_pnl_by_currency"][currency] = target["realized_pnl_by_currency"].get(currency, 0.0) + pnl
        if abs(cost_basis) > 1e-12:
            target["realized_cost_basis_by_currency"][currency] = (
                target["realized_cost_basis_by_currency"].get(currency, 0.0) + cost_basis
            )
    if symbol:
        target["realized_pnl_by_symbol"][symbol] = target["realized_pnl_by_symbol"].get(symbol, 0.0) + pnl
        if abs(cost_basis) > 1e-12:
            target["realized_cost_basis_by_symbol"][symbol] = (
                target["realized_cost_basis_by_symbol"].get(symbol, 0.0) + cost_basis
            )


def _has_realized_values(payload: Mapping[str, Any]) -> bool:
    values = payload.get("realized_pnl_by_currency") if isinstance(payload.get("realized_pnl_by_currency"), Mapping) else {}
    return any(abs(_float_or_zero(value)) > 1e-12 for value in values.values())


def _merge_money_map_in_place(target: dict[str, float], source: Any) -> None:
    if not isinstance(source, Mapping):
        return
    for key, value in source.items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        target[key_text] = target.get(key_text, 0.0) + _float_or_zero(value)


def _add_money_maps(*money_maps: Mapping[str, float]) -> dict[str, float]:
    result: dict[str, float] = {}
    for money_map in money_maps:
        for currency, value in money_map.items():
            currency_text = str(currency or "").upper()
            if currency_text:
                result[currency_text] = result.get(currency_text, 0.0) + _float_or_zero(value)
    return {currency: value for currency, value in sorted(result.items()) if abs(value) > 1e-12}


def _total_pnl_return_basis_by_currency(
    book_value_by_currency: Mapping[str, float],
    realized_cost_basis_by_currency: Mapping[str, float],
) -> dict[str, float]:
    currencies = set(book_value_by_currency) | set(realized_cost_basis_by_currency)
    result: dict[str, float] = {}
    for currency in sorted(currencies):
        current_book_value = _float_or_zero(book_value_by_currency.get(currency))
        realized_cost_basis = _float_or_zero(realized_cost_basis_by_currency.get(currency))
        basis = current_book_value if current_book_value > 0 else realized_cost_basis
        if basis > 0:
            result[str(currency).upper()] = basis
    return result


def _symbol_money_map(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(symbol or ""): _float_or_zero(amount)
        for symbol, amount in value.items()
        if str(symbol or "").strip() and abs(_float_or_zero(amount)) > 1e-12
    }


def _return_by_currency(equity_by_currency: Mapping[str, float], cash_flow_by_currency: Mapping[str, float]) -> dict[str, float]:
    returns: dict[str, float] = {}
    for currency, equity in equity_by_currency.items():
        net_flow = _float_or_zero(cash_flow_by_currency.get(currency))
        if net_flow > 0:
            returns[currency] = (_float_or_zero(equity) - net_flow) / net_flow
    return returns


def _total_return_by_currency(
    *,
    total_pnl_by_currency: Mapping[str, float],
    total_pnl_cost_basis_by_currency: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, str]]:
    returns: dict[str, float] = {}
    basis: dict[str, str] = {}
    for currency, total_pnl in total_pnl_by_currency.items():
        cost_basis = _float_or_zero(total_pnl_cost_basis_by_currency.get(currency))
        if cost_basis > 0:
            returns[currency] = _float_or_zero(total_pnl) / cost_basis
            basis[currency] = "realized_plus_unrealized_book_value"
    return returns, basis


def _return_since_eod_by_currency(
    sleeve_id: str,
    *,
    equity_by_currency: Mapping[str, float],
    cash_flow_by_currency: Mapping[str, float],
    daily_performance: Mapping[str, Any],
) -> dict[str, float]:
    pnl_by_currency = _pnl_since_eod_by_currency(
        sleeve_id,
        equity_by_currency=equity_by_currency,
        cash_flow_by_currency=cash_flow_by_currency,
        daily_performance=daily_performance,
    )
    latest = daily_performance.get("latest_by_sleeve_currency") if isinstance(daily_performance.get("latest_by_sleeve_currency"), Mapping) else {}
    returns: dict[str, float] = {}
    for currency, pnl in pnl_by_currency.items():
        row = latest.get(f"{sleeve_id}:{currency}") if isinstance(latest, Mapping) else None
        if not isinstance(row, Mapping):
            continue
        current_flow = _float_or_zero(cash_flow_by_currency.get(currency))
        baseline_flow = _float_or_zero(row.get("cumulative_cash_flow"))
        if not _daily_performance_row_has_comparable_baseline(row, current_flow=current_flow, baseline_flow=baseline_flow):
            continue
        baseline = _float_or_zero(row.get("equity"))
        if baseline <= 0:
            continue
        net_cash_flow = current_flow - baseline_flow
        denominator = baseline + max(0.0, net_cash_flow)
        if denominator <= 0:
            continue
        returns[currency] = _float_or_zero(pnl) / denominator
    return returns


def _pnl_since_eod_by_currency(
    sleeve_id: str,
    *,
    equity_by_currency: Mapping[str, float],
    cash_flow_by_currency: Mapping[str, float],
    daily_performance: Mapping[str, Any],
) -> dict[str, float]:
    latest = daily_performance.get("latest_by_sleeve_currency") if isinstance(daily_performance.get("latest_by_sleeve_currency"), Mapping) else {}
    pnl_by_currency: dict[str, float] = {}
    for currency, equity in equity_by_currency.items():
        row = latest.get(f"{sleeve_id}:{currency}") if isinstance(latest, Mapping) else None
        if not isinstance(row, Mapping):
            continue
        current_flow = _float_or_zero(cash_flow_by_currency.get(currency))
        baseline_flow = _float_or_zero(row.get("cumulative_cash_flow"))
        if not _daily_performance_row_has_comparable_baseline(row, current_flow=current_flow, baseline_flow=baseline_flow):
            continue
        baseline = _float_or_zero(row.get("equity"))
        if baseline <= 0:
            continue
        pnl_by_currency[currency] = _float_or_zero(equity) - baseline - (current_flow - baseline_flow)
    return pnl_by_currency


def _daily_performance_row_has_comparable_baseline(
    row: Mapping[str, Any],
    *,
    current_flow: float,
    baseline_flow: float,
) -> bool:
    if row.get("previous_equity") is not None and row.get("daily_pnl") is not None:
        return True
    baseline = _float_or_zero(row.get("equity"))
    if baseline <= 0:
        return False
    # First EOD snapshots can still be a valid "since EOD" baseline, but only
    # when the copied store contains the sleeve's cumulative cash-flow ledger.
    # A zero baseline flow with a large current flow usually means the route
    # account store was missing from the EOD artifact, which would create fake
    # -90% style Today returns.
    if abs(baseline_flow) <= 1e-9 and abs(current_flow) > max(1.0, baseline * 0.25):
        return False
    return True


def _target_quantity_from_operator_plan(plan: Mapping[str, Any]) -> int | None:
    if not plan:
        return None
    if plan.get("target_quantity") is not None:
        try:
            return int(plan.get("target_quantity") or 0)
        except (TypeError, ValueError):
            return None
    price = _float_or_zero(plan.get("current_price"))
    desired = _float_or_zero(plan.get("desired_value"))
    if price > 0 and desired >= 0:
        return int(desired // price)
    return None


def _nonzero_money_map(values: Mapping[str, float]) -> dict[str, float]:
    return {str(currency).upper(): float(amount) for currency, amount in sorted(values.items()) if abs(float(amount or 0.0)) > 1e-12}


def _read_json_object(path: Path) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return payload if isinstance(payload, Mapping) else None


def _current_estimate_source_payload(
    *,
    name: str,
    path: Path,
    exists: bool,
    max_age_seconds: float,
) -> dict[str, Any]:
    return {
        "name": name,
        "path": str(path),
        "exists": exists,
        "status": "unavailable",
        "as_of": None,
        "age_seconds": None,
        "freshness_threshold_seconds": max_age_seconds,
    }


def _current_estimate_source_from_estimates(
    *,
    name: str,
    path: Path,
    exists: bool,
    generated_at: datetime,
    max_age_seconds: float,
    estimates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    available = [estimate for estimate in estimates if estimate.get("status") != "unavailable"]
    source = _current_estimate_source_payload(
        name=name,
        path=path,
        exists=exists,
        max_age_seconds=max_age_seconds,
    )
    if not available:
        return source
    as_of_values = [
        parsed
        for estimate in available
        for parsed in (_parse_datetime_or_none(estimate.get("as_of")),)
        if parsed is not None
    ]
    as_of = max(as_of_values) if as_of_values else None
    age_seconds = _elapsed_seconds(generated_at, as_of) if as_of is not None else None
    source.update(
        {
            "status": "fresh" if any(estimate.get("status") == "fresh" for estimate in available) else "stale",
            "as_of": as_of.isoformat() if as_of is not None else None,
            "age_seconds": age_seconds,
        }
    )
    return source


def _current_state_from_runtime_report(report: Mapping[str, Any]) -> Mapping[str, Any] | None:
    engine_status = report.get("engine_status") if isinstance(report.get("engine_status"), Mapping) else {}
    portfolio_engine_state = (
        engine_status.get("portfolio_engine_state")
        if isinstance(engine_status.get("portfolio_engine_state"), Mapping)
        else {}
    )
    portfolio_state = report.get("portfolio_state") if isinstance(report.get("portfolio_state"), Mapping) else {}
    candidates = (
        portfolio_engine_state.get("current"),
        portfolio_state.get("current"),
    )
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _current_estimate_from_state(
    sleeve_id: str,
    current: Mapping[str, Any],
    *,
    path: Path,
    source_name: str,
    generated_at: datetime,
    fallback_as_of: datetime | None,
    max_age_seconds: float,
    symbol_names: Mapping[str, str],
    realized_pnl: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    as_of = _parse_datetime_or_none(current.get("as_of")) or fallback_as_of
    age_seconds = _elapsed_seconds(generated_at, as_of) if as_of is not None else None
    status = "stale" if age_seconds is not None and age_seconds > max_age_seconds else "fresh"
    cash_by_currency = _money_map(current.get("cash_by_currency"))
    equity_by_currency = _money_map(current.get("equity_by_currency"))
    holdings = _current_estimate_holdings(current.get("holdings"), equity_by_currency, symbol_names)
    stock_market_value_by_currency = _sum_holdings_by_currency(holdings, "market_value")
    cost_basis_by_currency = _sum_holdings_by_currency(holdings, "cost_basis")
    unrealized_pnl_by_currency = _sum_holdings_by_currency(holdings, "unrealized_pnl")
    realized_pnl = realized_pnl or {}
    realized_pnl_by_currency = _money_map(realized_pnl.get("realized_pnl_by_currency"))
    realized_cost_basis_by_currency = _money_map(realized_pnl.get("realized_cost_basis_by_currency"))
    realized_pnl_by_symbol = _symbol_money_map(realized_pnl.get("realized_pnl_by_symbol"))
    realized_cost_basis_by_symbol = _symbol_money_map(realized_pnl.get("realized_cost_basis_by_symbol"))
    total_pnl_by_currency = _add_money_maps(realized_pnl_by_currency, unrealized_pnl_by_currency)

    fallback_currency = _single_currency(
        cash_by_currency,
        equity_by_currency,
        stock_market_value_by_currency,
        cost_basis_by_currency,
        unrealized_pnl_by_currency,
    )
    if not cash_by_currency and fallback_currency:
        cash_by_currency = {fallback_currency: _float_or_zero(current.get("cash"))}
    if not equity_by_currency and fallback_currency:
        equity_by_currency = {
            fallback_currency: cash_by_currency.get(fallback_currency, 0.0)
            + stock_market_value_by_currency.get(fallback_currency, 0.0)
        }
    book_value_by_currency = _add_money_maps(cash_by_currency, cost_basis_by_currency)
    total_pnl_cost_basis_by_currency = _total_pnl_return_basis_by_currency(
        book_value_by_currency,
        realized_cost_basis_by_currency,
    )
    total_returns, total_return_basis = _total_return_by_currency(
        total_pnl_by_currency=total_pnl_by_currency,
        total_pnl_cost_basis_by_currency=total_pnl_cost_basis_by_currency,
    )

    return {
        "sleeve_id": sleeve_id,
        "status": status,
        "as_of": as_of.isoformat() if as_of is not None else None,
        "age_seconds": age_seconds,
        "freshness_threshold_seconds": max_age_seconds,
        "source_name": source_name,
        "source_path": str(path),
        "cash_by_currency": cash_by_currency,
        "stock_market_value_by_currency": stock_market_value_by_currency,
        "equity_by_currency": equity_by_currency,
        "cost_basis_by_currency": cost_basis_by_currency,
        "book_value_by_currency": book_value_by_currency,
        "unrealized_pnl_by_currency": unrealized_pnl_by_currency,
        "realized_pnl_by_currency": realized_pnl_by_currency,
        "total_pnl_by_currency": total_pnl_by_currency,
        "realized_cost_basis_by_currency": realized_cost_basis_by_currency,
        "total_pnl_cost_basis_by_currency": total_pnl_cost_basis_by_currency,
        "realized_pnl_by_symbol": realized_pnl_by_symbol,
        "realized_cost_basis_by_symbol": realized_cost_basis_by_symbol,
        "total_return_by_currency": total_returns,
        "total_return_basis_by_currency": total_return_basis,
        "pnl_since_eod_by_currency": {},
        "return_since_eod_by_currency": {},
        "holding_count": len(holdings),
        "holdings": holdings,
        "reason": "",
    }


def _unavailable_current_estimate(sleeve_id: str, *, path: Path, source_name: str, reason: str) -> dict[str, Any]:
    return {
        "sleeve_id": sleeve_id,
        "status": "unavailable",
        "as_of": None,
        "age_seconds": None,
        "freshness_threshold_seconds": None,
        "source_name": source_name,
        "source_path": str(path),
        "cash_by_currency": {},
        "stock_market_value_by_currency": {},
        "equity_by_currency": {},
        "cost_basis_by_currency": {},
        "book_value_by_currency": {},
        "unrealized_pnl_by_currency": {},
        "realized_pnl_by_currency": {},
        "total_pnl_by_currency": {},
        "realized_cost_basis_by_currency": {},
        "total_pnl_cost_basis_by_currency": {},
        "realized_pnl_by_symbol": {},
        "realized_cost_basis_by_symbol": {},
        "net_cash_flow_by_currency": {},
        "cash_transfer_return_by_currency": {},
        "total_return_by_currency": {},
        "total_return_basis_by_currency": {},
        "pnl_since_eod_by_currency": {},
        "return_since_eod_by_currency": {},
        "holding_count": 0,
        "holdings": [],
        "positions": [],
        "target_count": 0,
        "reason": reason,
    }


def _current_estimate_holdings(
    holdings: Any,
    equity_by_currency: Mapping[str, float],
    symbol_names: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for holding in holdings or []:
        if not isinstance(holding, Mapping):
            continue
        symbol = _symbol_key_from_holding(holding)
        quantity = _float_or_zero(holding.get("quantity"))
        market_value = _float_or_zero(holding.get("market_value"))
        market_price = _float_or_zero(holding.get("market_price"))
        if market_value <= 0 and market_price > 0:
            market_value = abs(quantity) * market_price
        cost_basis = _float_or_zero(holding.get("cost_basis"))
        average_price = _float_or_zero(holding.get("average_price"))
        if cost_basis <= 0 and average_price > 0:
            cost_basis = abs(quantity) * average_price
        if "unrealized_pnl" in holding:
            unrealized_pnl = _float_or_zero(holding.get("unrealized_pnl"))
        else:
            unrealized_pnl = market_value - cost_basis
        rows.append(
            {
                "symbol": symbol,
                "label": _symbol_display_label(symbol, symbol_names),
                "currency": _currency_for_symbol_or_maps(symbol, equity_by_currency),
                "quantity": quantity,
                "average_price": average_price,
                "market_price": market_price,
                "market_value": market_value,
                "cost_basis": cost_basis,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": holding.get("unrealized_pnl_pct"),
            }
        )
    return rows


def _sum_holdings_by_currency(holdings: Sequence[Mapping[str, Any]], field: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for holding in holdings:
        currency = str(holding.get("currency") or "").upper()
        if not currency:
            continue
        totals[currency] = totals.get(currency, 0.0) + _float_or_zero(holding.get(field))
    return {currency: value for currency, value in sorted(totals.items()) if abs(value) > 1e-12}


def _money_map(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(currency or "").upper(): _float_or_zero(amount)
        for currency, amount in value.items()
        if str(currency or "").strip() and abs(_float_or_zero(amount)) > 1e-12
    }


def _single_currency(*maps: Mapping[str, float]) -> str:
    currencies = {currency for money_map in maps for currency in money_map}
    return next(iter(currencies)) if len(currencies) == 1 else ""


def _currency_for_symbol_or_maps(symbol: str, money_map: Mapping[str, float]) -> str:
    if len(money_map) == 1:
        return next(iter(money_map))
    prefix = symbol.split(":", 1)[0].upper() if ":" in symbol else ""
    if prefix in {"US", "NAS", "NYS", "AMS", "OTC"}:
        return "USD"
    return "KRW"


def _parse_datetime_or_none(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _strategy_docs_payload(snapshot: RuntimeConfigSnapshot, sleeve_ids: tuple[str, ...]) -> dict[str, Any]:
    docs: dict[str, dict[str, Any]] = {}
    for sleeve_id in sleeve_ids:
        docs[sleeve_id] = _strategy_doc_payload(snapshot, sleeve_id)
    return docs


def _strategy_doc_payload(snapshot: RuntimeConfigSnapshot, sleeve_id: str) -> dict[str, Any]:
    sleeve = snapshot.config.sleeve(sleeve_id)
    if sleeve.workspace_path is not None:
        workspace = resolve_runtime_path(snapshot, sleeve.workspace_path).resolve()
    else:
        workspace = resolve_runtime_path(snapshot, Path("sleeves") / sleeve_id).resolve()
    path = workspace / "STRATEGY.md"
    payload: dict[str, Any] = {
        "sleeve_id": sleeve_id,
        "path": str(path),
        "exists": path.is_file(),
        "title": "STRATEGY.md",
        "line_count": 0,
        "char_count": 0,
        "abstract": "",
        "recent_judgment_rationale": "",
        "content": "",
        "warnings": [],
    }
    if not path.is_file():
        payload["warnings"].append("strategy_doc_missing")
        return payload
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - defensive UI path.
        payload["warnings"].append(f"strategy_doc_unavailable:{exc}")
        return payload
    payload["title"] = _first_markdown_heading(content) or "STRATEGY.md"
    payload["line_count"] = len(content.splitlines())
    payload["char_count"] = len(content)
    payload["abstract"] = _markdown_section(content, "ABSTRACT")
    payload["recent_judgment_rationale"] = _markdown_section(content, "Recent Judgment Rationale")
    if not payload["abstract"]:
        payload["warnings"].append("strategy_doc_missing_abstract")
    payload["content"] = content
    return payload


def _first_markdown_heading(content: str) -> str | None:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title:
            return title
    return None


def _markdown_section(content: str, heading: str) -> str:
    target = heading.strip().casefold()
    lines = content.splitlines()
    start_index: int | None = None
    start_level = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        level = len(stripped) - len(stripped.lstrip("#"))
        title = stripped[level:].strip()
        if title.casefold() != target:
            continue
        start_index = index + 1
        start_level = level
        break
    if start_index is None:
        return ""
    body: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= start_level:
                break
        body.append(line)
    return "\n".join(body).strip()


def _resolve_operator_data_path(snapshot: RuntimeConfigSnapshot, path: Path) -> Path:
    if path.is_absolute():
        return path.resolve()
    start = snapshot.source_path.parent.resolve()
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "src" / "leaps_quant_engine").exists():
            return (candidate / path).resolve()
    return (start / path).resolve()


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


def _compact_order_route(
    payload: dict[str, Any],
    *,
    symbol_names: Mapping[str, str] | None = None,
    sleeve_display_names: Mapping[str, str] | None = None,
    current_estimates: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    compact = dict(payload)
    symbol_names = symbol_names or {}
    sleeve_display_names = sleeve_display_names or {}
    current_estimates = current_estimates or {}
    currency = str(compact.get("currency") or "").upper()
    runtime = dict(compact.get("order_runtime") or {})
    runtime["recent_events"] = [_compact_order_event(event) for event in runtime.get("recent_events") or []]
    runtime["open_tickets"] = [_compact_order_ticket(ticket) for ticket in runtime.get("open_tickets") or []]
    runtime.pop("events", None)
    runtime.pop("tickets", None)
    compact["order_runtime"] = runtime
    compact["sleeves"] = [
        {
            **dict(sleeve),
            "display_name": sleeve_display_names.get(str(sleeve.get("sleeve_id") or ""), ""),
            "allocation": _book_allocation_payload(sleeve, currency=currency, symbol_names=symbol_names),
            "current_estimate": _current_estimate_for_route(
                current_estimates.get(str(sleeve.get("sleeve_id") or "")),
                currency=currency,
            ),
            "recent_events": [_compact_order_event(event) for event in sleeve.get("recent_events") or []],
            "open_tickets": [_compact_order_ticket(ticket) for ticket in sleeve.get("open_tickets") or []],
        }
        for sleeve in compact.get("sleeves") or []
    ]
    return compact


def _attach_current_estimates_to_order_routes(
    order_routes: Sequence[Mapping[str, Any]],
    current_estimates: Mapping[str, Any],
) -> list[dict[str, Any]]:
    latest_by_sleeve = current_estimates.get("latest_by_sleeve") if isinstance(current_estimates.get("latest_by_sleeve"), Mapping) else {}
    routes: list[dict[str, Any]] = []
    for route in order_routes:
        row = dict(route)
        currency = str(row.get("currency") or "").upper()
        row["sleeves"] = [
            {
                **dict(sleeve),
                "current_estimate": _current_estimate_for_route(
                    latest_by_sleeve.get(str(sleeve.get("sleeve_id") or "")) if isinstance(latest_by_sleeve, Mapping) else None,
                    currency=currency,
                ),
            }
            for sleeve in row.get("sleeves") or []
            if isinstance(sleeve, Mapping)
        ]
        routes.append(row)
    return routes


def _current_estimate_for_route(
    estimate: Mapping[str, Any] | None,
    *,
    currency: str,
) -> dict[str, Any]:
    if not estimate:
        return {}
    payload = dict(estimate)
    route_currency = str(currency or "").upper()
    if payload.get("status") == "unavailable" or not route_currency:
        return payload
    money_maps = (
        payload.get("cash_by_currency"),
        payload.get("stock_market_value_by_currency"),
        payload.get("equity_by_currency"),
        payload.get("cost_basis_by_currency"),
        payload.get("book_value_by_currency"),
        payload.get("unrealized_pnl_by_currency"),
        payload.get("realized_pnl_by_currency"),
        payload.get("total_pnl_by_currency"),
        payload.get("realized_cost_basis_by_currency"),
        payload.get("total_pnl_cost_basis_by_currency"),
        payload.get("net_cash_flow_by_currency"),
        payload.get("cash_transfer_return_by_currency"),
        payload.get("total_return_by_currency"),
        payload.get("pnl_since_eod_by_currency"),
        payload.get("return_since_eod_by_currency"),
    )
    holding_currencies = {
        str(holding.get("currency") or "").upper()
        for holding in payload.get("holdings") or []
        if isinstance(holding, Mapping)
    }
    has_currency = route_currency in holding_currencies or any(
        isinstance(money_map, Mapping) and route_currency in money_map
        for money_map in money_maps
    )
    if has_currency:
        filtered = dict(payload)
        for key in (
            "cash_by_currency",
            "stock_market_value_by_currency",
            "equity_by_currency",
            "cost_basis_by_currency",
            "book_value_by_currency",
            "unrealized_pnl_by_currency",
            "realized_pnl_by_currency",
            "total_pnl_by_currency",
            "realized_cost_basis_by_currency",
            "total_pnl_cost_basis_by_currency",
            "net_cash_flow_by_currency",
            "cash_transfer_return_by_currency",
            "total_return_by_currency",
            "total_return_basis_by_currency",
            "pnl_since_eod_by_currency",
            "return_since_eod_by_currency",
        ):
            value = filtered.get(key)
            if isinstance(value, Mapping):
                filtered[key] = {
                    str(currency).upper(): amount
                    for currency, amount in value.items()
                    if str(currency).upper() == route_currency
                }
        filtered["holdings"] = [
            dict(holding)
            for holding in filtered.get("holdings") or []
            if str(holding.get("currency") or "").upper() == route_currency
        ]
        filtered["positions"] = [
            dict(position)
            for position in filtered.get("positions") or []
            if str(position.get("currency") or "").upper() == route_currency
        ]
        filtered["holding_count"] = len(filtered["holdings"])
        filtered["target_count"] = len([position for position in filtered["positions"] if abs(_float_or_zero(position.get("target_percent"))) > 1e-12])
        return filtered
    return {
        **payload,
        "status": "unavailable",
        "cash_by_currency": {},
        "stock_market_value_by_currency": {},
        "equity_by_currency": {},
        "cost_basis_by_currency": {},
        "book_value_by_currency": {},
        "unrealized_pnl_by_currency": {},
        "realized_pnl_by_currency": {},
        "total_pnl_by_currency": {},
        "realized_cost_basis_by_currency": {},
        "total_pnl_cost_basis_by_currency": {},
        "realized_pnl_by_symbol": {},
        "realized_cost_basis_by_symbol": {},
        "net_cash_flow_by_currency": {},
        "cash_transfer_return_by_currency": {},
        "total_return_by_currency": {},
        "total_return_basis_by_currency": {},
        "pnl_since_eod_by_currency": {},
        "return_since_eod_by_currency": {},
        "holding_count": 0,
        "holdings": [],
        "positions": [],
        "target_count": 0,
        "reason": f"current_estimate_currency_unavailable:{route_currency}",
    }


def _book_allocation_payload(
    sleeve: Mapping[str, Any],
    *,
    currency: str,
    symbol_names: Mapping[str, str],
) -> dict[str, Any]:
    portfolio = sleeve.get("portfolio") if isinstance(sleeve.get("portfolio"), Mapping) else {}
    cash_by_currency = portfolio.get("cash_by_currency") if isinstance(portfolio.get("cash_by_currency"), Mapping) else {}
    cash = _cash_for_currency(cash_by_currency, currency)
    if cash is None:
        cash = _float_or_zero(portfolio.get("cash"))

    segments: list[dict[str, Any]] = []
    if cash > 0:
        segments.append({"kind": "cash", "label": "Cash", "value": cash})

    for holding in portfolio.get("holdings") or []:
        if not isinstance(holding, Mapping):
            continue
        quantity = _float_or_zero(holding.get("quantity"))
        if abs(quantity) <= 1e-12:
            continue
        value = _float_or_zero(holding.get("market_value"))
        if value <= 0:
            value = abs(quantity) * _float_or_zero(holding.get("average_price"))
        if value <= 0:
            continue
        symbol = _symbol_key_from_holding(holding)
        segments.append(
            {
                "kind": "holding",
                "label": _symbol_display_label(symbol, symbol_names),
                "symbol": symbol,
                "quantity": quantity,
                "value": value,
            }
        )

    total = sum(float(segment["value"]) for segment in segments)
    if total > 0:
        segments = [
            {
                **segment,
                "weight": float(segment["value"]) / total,
            }
            for segment in segments
        ]
    return {
        "basis": "book_cost",
        "currency": currency,
        "total_value": total,
        "segments": segments,
    }


def _cash_for_currency(cash_by_currency: Mapping[str, Any], currency: str) -> float | None:
    if not currency:
        return None
    for key, value in cash_by_currency.items():
        if str(key).upper() == currency:
            return _float_or_zero(value)
    return None


def _symbol_key_from_holding(holding: Mapping[str, Any]) -> str:
    symbol = holding.get("symbol")
    if isinstance(symbol, Mapping):
        market = str(symbol.get("market") or "").strip()
        ticker = str(symbol.get("ticker") or symbol.get("symbol") or "").strip()
        return f"{market}:{ticker}" if market and ticker else ticker or market
    market = str(holding.get("market") or "").strip()
    ticker = str(symbol or "").strip()
    return f"{market}:{ticker}" if market and ticker else ticker or market


def _symbol_display_label(symbol_key: str, symbol_names: Mapping[str, str]) -> str:
    name = str(symbol_names.get(symbol_key) or "").strip()
    if not name:
        return symbol_key
    if ":" not in symbol_key:
        return f"{name} ({symbol_key})"
    market, ticker = symbol_key.split(":", 1)
    if market.upper() == "KRX":
        return f"{name} ({ticker})"
    return f"{ticker} {name}"


def _symbol_display_names(snapshot: RuntimeConfigSnapshot, sleeve_ids: tuple[str, ...]) -> dict[str, str]:
    names = dict(COMMON_SYMBOL_DISPLAY_NAMES)
    for sleeve_id in sleeve_ids:
        try:
            sleeve = snapshot.config.sleeve(sleeve_id)
            universe_path = resolve_runtime_path(snapshot, sleeve.universe.coarse_path)
            universe = load_universe_definition(universe_path)
        except Exception:
            continue
        for symbol in universe.symbols:
            name = _symbol_name_from_metadata(universe.properties_for(symbol))
            if name:
                names[symbol.key] = name
    return dict(sorted(names.items()))


def _sleeve_display_names(snapshot: RuntimeConfigSnapshot, sleeve_ids: tuple[str, ...]) -> dict[str, str]:
    names: dict[str, str] = {}
    for sleeve_id in sleeve_ids:
        try:
            sleeve = snapshot.config.sleeve(sleeve_id)
        except KeyError:
            continue
        display_name = str(getattr(sleeve, "display_name", "") or "").strip()
        if display_name:
            names[sleeve_id] = display_name
    return dict(sorted(names.items()))


def _symbol_name_from_metadata(metadata: Mapping[str, Any]) -> str:
    for key in ("display_name", "name", "company_name", "security_name", "kor_name", "english_name"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


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


INDEX_HTML = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LEaps Operator UI</title>
  <link rel="stylesheet" href="/assets/styles.css?v={OPERATOR_UI_ASSET_VERSION}">
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
        <section class="panel overview-panel">
          <div class="panel-head">
            <h2>Sleeve Summary</h2>
            <span class="muted">Sorted by return</span>
          </div>
          <div id="portfolio-overview" class="portfolio-overview"></div>
          <div id="sleeve-overview" class="sleeve-overview"></div>
        </section>

        <section id="sleeves-panel" class="panel">
          <div class="panel-head">
            <h2>Sleeves</h2>
            <span id="sleeve-count" class="muted"></span>
          </div>
          <div id="sleeves" class="sleeve-sections"></div>
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
  <script src="/assets/app.js?v={OPERATOR_UI_ASSET_VERSION}"></script>
</body>
</html>
"""


STYLES_CSS = """
:root {
  color-scheme: dark;
  --bg: #111318;
  --bg-lift: #171a21;
  --surface: #1c2028;
  --surface-2: #242a34;
  --surface-3: #2c3440;
  --ink: #f2f5f8;
  --muted: #9aa6b2;
  --line: #343c49;
  --ok: #28c76f;
  --warn: #f7b955;
  --bad: #ff6b6b;
  --profit: #ff5f6d;
  --loss: #4d8dff;
  --accent: #6bc5ff;
  --accent-2: #9bdb6d;
  --accent-soft: #17384d;
  --green-soft: #183a2a;
  --amber-soft: #473419;
  --red-soft: #4a2026;
  --blue-soft: #1b2d55;
  --shadow: rgba(0, 0, 0, 0.28);
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  min-height: 100vh;
  background:
    linear-gradient(180deg, #171a21 0%, #111318 42%, #0f1116 100%);
  color: var(--ink);
  font-family: "Segoe UI", Arial, sans-serif;
}

.shell {
  width: min(1520px, 100%);
  margin: 0 auto;
  padding: 20px;
}

.topbar,
.panel,
.metric,
.sleeve-card,
.sleeve-section,
.route-panel,
.micro-metric,
.ticket-mini,
.event-row {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: 0 10px 28px var(--shadow);
}

.topbar {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 18px 20px;
  background:
    linear-gradient(135deg, #202833 0%, #171a21 52%, #241a22 100%);
  border-color: #3c4655;
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
  border: 1px solid #75ccff;
  border-radius: 999px;
  background: #2c8dcc;
  color: #f8fbff;
  padding: 0 14px;
  font: inherit;
  font-weight: 700;
  cursor: pointer;
  transition: transform 120ms ease, background 120ms ease;
}

button:hover:not(:disabled) {
  transform: translateY(-1px);
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
  color: #5bf09a;
}

.badge.warn {
  background: var(--amber-soft);
  color: #ffd27c;
}

.badge.bad {
  background: var(--red-soft);
  color: #ff9ba0;
}

.badge.neutral {
  background: var(--accent-soft);
  color: #95dcff;
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
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent),
    var(--surface);
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
  font-variant-numeric: tabular-nums;
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
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
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent 160px),
    var(--surface);
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.overview-panel {
  background:
    linear-gradient(135deg, rgba(107, 197, 255, 0.08), transparent 42%),
    var(--surface);
}

.portfolio-overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.portfolio-summary-item {
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 8px;
  padding: 9px 10px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.04), transparent),
    rgba(10, 12, 16, 0.24);
}

.portfolio-summary-item .label {
  color: var(--muted);
  font-size: 10px;
  font-weight: 850;
  letter-spacing: 0;
  text-transform: uppercase;
}

.portfolio-summary-item .value {
  margin-top: 4px;
  overflow: hidden;
  color: var(--ink);
  font-size: 13px;
  font-weight: 900;
  font-variant-numeric: tabular-nums;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.portfolio-summary-item .value.positive {
  color: var(--profit);
}

.portfolio-summary-item .value.negative {
  color: var(--loss);
}

.portfolio-summary-item .value.warning {
  color: var(--warn);
}

.sleeve-overview {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.summary-card {
  position: relative;
  min-width: 0;
}

.summary-link {
  position: relative;
  display: block;
  height: 100%;
  min-width: 0;
  min-height: 214px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-left: 5px solid #6bc5ff;
  border-radius: 8px;
  padding: 14px 14px 46px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.055), transparent 45%),
    var(--surface-2);
  color: var(--ink);
  text-decoration: none;
  box-shadow: 0 8px 22px var(--shadow);
}

.summary-link:hover {
  border-color: #718296;
  transform: translateY(-1px);
}

.summary-link.profit {
  border-left-color: var(--profit);
}

.summary-link.loss {
  border-left-color: var(--loss);
}

.summary-link.warning {
  border-left-color: var(--warn);
}

.summary-link.quiet {
  border-left-color: var(--ok);
}

.sleeve-order-controls {
  position: absolute;
  right: 10px;
  bottom: 10px;
  z-index: 2;
  display: flex;
  gap: 6px;
}

.sleeve-order-button {
  appearance: none;
  display: inline-grid;
  place-items: center;
  width: 30px;
  height: 28px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  padding: 0;
  background: rgba(10, 14, 21, 0.72);
  color: var(--ink);
  cursor: pointer;
  font-size: 15px;
  font-weight: 850;
  line-height: 1;
}

.sleeve-order-button:hover:not(:disabled) {
  border-color: rgba(114, 167, 255, 0.72);
  background: rgba(77, 141, 255, 0.18);
}

.sleeve-order-button:disabled {
  cursor: default;
  opacity: 0.35;
}

.summary-top {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: start;
}

.summary-name {
  min-width: 0;
  overflow: hidden;
  font-size: 17px;
  font-weight: 850;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-meta {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
}

.summary-return {
  margin-top: 12px;
  display: grid;
  gap: 2px;
  font-variant-numeric: tabular-nums;
}

.summary-return-label {
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

.summary-return-value {
  font-size: 24px;
  font-weight: 900;
}

.summary-return-value-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
  align-items: baseline;
  min-width: 0;
}

.summary-return-money {
  font-size: 13px;
  font-weight: 850;
  font-variant-numeric: tabular-nums;
}

.summary-today-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  align-items: baseline;
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  font-variant-numeric: tabular-nums;
}

.summary-today-row.positive {
  color: var(--profit);
}

.summary-today-row.negative {
  color: var(--loss);
}

.summary-today-label {
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
}

.summary-return-equity {
  color: var(--muted);
  font-size: 11px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
}

.summary-return.positive,
.value.positive {
  color: var(--profit);
}

.summary-return.negative,
.value.negative {
  color: var(--loss);
}

.summary-return.neutral {
  color: var(--muted);
}

.summary-mini-grid {
  display: grid;
  gap: 7px;
  margin-top: 12px;
}

.summary-mini {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 8px;
  padding: 7px 8px;
  background: rgba(10, 12, 16, 0.28);
}

.summary-mini .label,
.summary-mini .value {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.summary-mini .value {
  text-align: right;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.summary-visuals {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}

.summary-chart {
  display: grid;
  gap: 7px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  border-radius: 8px;
  padding: 9px;
  background: rgba(10, 12, 16, 0.25);
}

.summary-chart-title {
  color: var(--muted);
  font-size: 11px;
  font-weight: 850;
  letter-spacing: 0;
  text-transform: uppercase;
}

.chart-row {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr) 104px;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.chart-label,
.chart-value {
  overflow: hidden;
  color: var(--muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chart-value {
  color: var(--ink);
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.chart-track {
  position: relative;
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
}

.chart-zero {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 50%;
  width: 1px;
  background: rgba(255, 255, 255, 0.23);
}

.chart-fill {
  position: absolute;
  top: 0;
  bottom: 0;
  min-width: 2px;
  border-radius: 999px;
}

.chart-fill.positive {
  background: linear-gradient(90deg, rgba(40, 199, 111, 0.55), rgba(40, 199, 111, 0.95));
}

.chart-fill.negative {
  background: linear-gradient(90deg, rgba(77, 141, 255, 0.95), rgba(77, 141, 255, 0.55));
}

.asset-bar {
  display: flex;
  height: 10px;
  overflow: hidden;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
}

.asset-stock {
  background: linear-gradient(90deg, #4d8dff, #46d7d1);
}

.asset-cash {
  background: linear-gradient(90deg, #6c7888, #9aa7b6);
}

.asset-donut-layout {
  display: grid;
  grid-template-columns: 66px minmax(0, 1fr);
  gap: 10px;
  align-items: center;
  min-width: 0;
}

.asset-donut {
  position: relative;
  display: grid;
  place-items: center;
  width: 62px;
  height: 62px;
  border-radius: 50%;
  background:
    radial-gradient(circle at center, rgba(11, 15, 22, 0.94) 0 47%, transparent 49%),
    conic-gradient(from -90deg, #4d8dff 0deg var(--stock-sweep), #8c98a8 var(--stock-sweep) 360deg);
  box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.08), 0 8px 24px rgba(0, 0, 0, 0.18);
}

.asset-donut-label {
  color: var(--ink);
  font-size: 11px;
  font-weight: 850;
  font-variant-numeric: tabular-nums;
}

.asset-breakdown {
  display: grid;
  gap: 7px;
  min-width: 0;
}

.asset-holding-list {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.asset-holding-row {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  gap: 7px;
  align-items: center;
  min-width: 0;
  color: var(--muted);
  font-size: 11px;
}

.asset-swatch {
  width: 10px;
  height: 10px;
  border-radius: 3px;
}

.asset-holding-label,
.asset-holding-value {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.asset-holding-label {
  color: var(--ink);
  font-weight: 750;
}

.asset-holding-value {
  font-variant-numeric: tabular-nums;
  text-align: right;
}

.asset-legend {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.asset-legend span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sleeve-sections {
  display: grid;
  gap: 12px;
}

.sleeve-tabs {
  display: flex;
  gap: 8px;
  min-width: 0;
  overflow-x: auto;
  padding-bottom: 2px;
}

.sleeve-tab-item {
  display: grid;
  grid-template-columns: 28px minmax(208px, 1fr) 28px;
  gap: 4px;
  align-items: stretch;
  min-width: 288px;
}

.sleeve-tab-item .sleeve-order-button {
  width: 28px;
  height: auto;
  min-height: 100%;
}

.sleeve-tab {
  appearance: none;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 7px 12px;
  align-items: center;
  width: 100%;
  min-width: 0;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 8px;
  padding: 10px 12px;
  background: rgba(255, 255, 255, 0.035);
  color: var(--ink);
  cursor: pointer;
  text-align: left;
}

.sleeve-tab:hover {
  border-color: rgba(114, 167, 255, 0.52);
  background: rgba(77, 141, 255, 0.1);
}

.sleeve-tab.active {
  border-color: rgba(77, 141, 255, 0.9);
  background: linear-gradient(135deg, rgba(77, 141, 255, 0.24), rgba(70, 215, 209, 0.08));
  box-shadow: inset 0 0 0 1px rgba(77, 141, 255, 0.24);
}

.sleeve-tab.profit.active {
  border-color: rgba(40, 199, 111, 0.72);
  background: linear-gradient(135deg, rgba(40, 199, 111, 0.2), rgba(77, 141, 255, 0.08));
}

.sleeve-tab.loss.active {
  border-color: rgba(255, 107, 107, 0.72);
  background: linear-gradient(135deg, rgba(255, 107, 107, 0.18), rgba(77, 141, 255, 0.08));
}

.sleeve-tab-name,
.sleeve-tab-meta {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sleeve-tab-name {
  font-size: 13px;
  font-weight: 850;
}

.sleeve-tab-meta {
  grid-column: 1 / -1;
  color: var(--muted);
  font-size: 11px;
}

.sleeve-tab-return {
  font-size: 13px;
  font-weight: 900;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.sleeve-tab-return-stack {
  display: grid;
  gap: 2px;
  justify-items: end;
}

.sleeve-tab-pnl {
  color: var(--muted);
  font-size: 11px;
  font-weight: 750;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.sleeve-detail-nav {
  display: grid;
  grid-template-columns: 40px minmax(0, 1fr) 40px;
  gap: 8px;
  align-items: center;
}

.sleeve-tab-panel {
  touch-action: pan-y;
}

.sleeve-slide-button {
  appearance: none;
  display: inline-grid;
  place-items: center;
  width: 40px;
  height: 40px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  padding: 0;
  background: rgba(10, 14, 21, 0.72);
  color: var(--ink);
  cursor: pointer;
  font-size: 22px;
  font-weight: 900;
  line-height: 1;
}

.sleeve-slide-button:hover:not(:disabled) {
  border-color: rgba(114, 167, 255, 0.72);
  background: rgba(77, 141, 255, 0.18);
}

.sleeve-slide-button:disabled {
  cursor: default;
  opacity: 0.35;
}

.sleeve-slide-status {
  min-width: 0;
  color: var(--muted);
  font-size: 12px;
  font-weight: 800;
  text-align: center;
}

.sleeve-tab-return.positive {
  color: var(--green);
}

.sleeve-tab-return.negative {
  color: var(--red);
}

.sleeve-card,
.event-row {
  padding: 14px;
}

.sleeve-section {
  padding: 16px;
  scroll-margin-top: 250px;
}

.sleeve-section.profit {
  border-color: rgba(255, 95, 109, 0.38);
}

.sleeve-section.loss {
  border-color: rgba(77, 141, 255, 0.42);
}

.sleeve-section.warning {
  border-color: rgba(247, 185, 85, 0.42);
}

.section-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 16px;
  align-items: start;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--line);
}

.section-kicker {
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

.section-title {
  margin: 3px 0 0;
  font-size: 22px;
}

.chip-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.chip {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 0 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface-3);
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  white-space: nowrap;
}

.section-metrics {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 10px;
  margin-top: 14px;
}

.strategy-panel {
  margin-top: 14px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent),
    rgba(24, 28, 36, 0.78);
}

.strategy-brief {
  padding: 12px;
}

.strategy-brief-head {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
}

.strategy-abstract {
  margin: 10px 0 0;
  color: #d9e0ea;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.strategy-rationale {
  margin: 12px 0 0;
  padding: 10px 12px;
  border-left: 3px solid var(--accent);
  background: rgba(77, 141, 255, 0.08);
}

.strategy-rationale-label {
  color: var(--muted);
  font-size: 11px;
  font-weight: 850;
  letter-spacing: 0;
  text-transform: uppercase;
}

.strategy-rationale-text {
  margin: 6px 0 0;
  color: #edf3ff;
  font-size: 13px;
  line-height: 1.6;
  white-space: pre-wrap;
}

.strategy-details {
  border-top: 1px solid var(--line);
}

.strategy-details summary {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-height: 46px;
  padding: 10px 12px;
  cursor: pointer;
  list-style: none;
}

.strategy-details summary::-webkit-details-marker {
  display: none;
}

.strategy-toggle-mark {
  display: inline-grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.05);
  color: var(--accent);
  font-size: 16px;
  font-weight: 900;
  line-height: 1;
}

.strategy-details[open] .strategy-toggle-mark {
  color: var(--profit);
}

.strategy-details[open] .strategy-toggle-mark::before {
  content: "-";
}

.strategy-details:not([open]) .strategy-toggle-mark::before {
  content: "+";
}

.strategy-label {
  color: var(--ink);
  font-size: 13px;
  font-weight: 850;
}

.strategy-title {
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  font-size: 12px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.strategy-meta {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.strategy-doc {
  max-height: 380px;
  margin: 0;
  padding: 14px;
  overflow: auto;
  border-top: 1px solid var(--line);
  background: rgba(8, 10, 14, 0.5);
  color: #d9e0ea;
  font-family: "Cascadia Mono", "Consolas", monospace;
  font-size: 12px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.micro-metric {
  min-width: 0;
  padding: 12px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent),
    var(--surface-2);
}

.micro-metric .value {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.route-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 14px;
}

.route-panel {
  min-width: 0;
  padding: 14px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.035), transparent),
    var(--surface-2);
}

.route-title {
  min-width: 0;
  overflow: hidden;
  font-weight: 800;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.route-subtitle {
  margin-top: 2px;
  color: var(--muted);
  font-size: 12px;
}

.ticket-mini-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.ticket-mini {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  padding: 10px;
  background: var(--surface);
}

.allocation {
  display: grid;
  grid-template-columns: 122px minmax(0, 1fr);
  gap: 14px;
  align-items: center;
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(10, 12, 16, 0.2);
}

.donut {
  width: 112px;
  aspect-ratio: 1;
  border-radius: 50%;
  display: grid;
  place-items: center;
  box-shadow: inset 0 0 0 1px rgba(25, 33, 42, 0.1);
}

.donut-hole {
  width: 52px;
  aspect-ratio: 1;
  border-radius: 50%;
  display: grid;
  place-items: center;
  background: var(--surface);
  border: 1px solid var(--line);
  text-align: center;
}

.donut-hole strong {
  display: block;
  font-size: 16px;
  line-height: 1.1;
}

.donut-hole span {
  display: block;
  margin-top: 2px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
}

.allocation-legend {
  display: grid;
  gap: 7px;
  min-width: 0;
}

.estimate-panel {
  display: grid;
  gap: 10px;
  margin-top: 12px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.035);
}

.estimate-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.estimate-title {
  overflow: hidden;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.estimate-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.estimate-meta {
  color: var(--muted);
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.daily-return-panel {
  margin-top: 14px;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.035);
}

.daily-return-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: baseline;
}

.daily-return-title {
  font-size: 12px;
  font-weight: 850;
  letter-spacing: 0;
  text-transform: uppercase;
}

.daily-return-meta {
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.daily-return-chart {
  width: 100%;
  height: 116px;
  margin-top: 8px;
}

.daily-return-axis {
  stroke: rgba(255, 255, 255, 0.18);
  stroke-width: 1;
}

.daily-return-line {
  fill: none;
  stroke: var(--accent);
  stroke-width: 2.5;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.daily-return-point.positive {
  fill: var(--profit);
}

.daily-return-point.negative {
  fill: var(--loss);
}

.daily-return-point.neutral {
  fill: var(--muted);
}

.daily-return-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 14px;
  color: var(--muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.daily-return-footer .positive {
  color: var(--profit);
}

.daily-return-footer .negative {
  color: var(--loss);
}

.daily-return-footer .neutral {
  color: var(--muted);
}

.legend-row {
  display: grid;
  grid-template-columns: 10px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.legend-swatch {
  width: 11px;
  height: 11px;
  border-radius: 3px;
}

.legend-label {
  min-width: 0;
  overflow: hidden;
  color: var(--ink);
  font-size: 12px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.legend-value {
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.sleeve-meta {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
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

.position-symbol-cell {
  min-width: 150px;
  font-weight: 800;
}

.position-number-cell {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.position-pnl-cell {
  font-weight: 850;
}

.position-pnl-cell.positive {
  color: var(--profit);
}

.position-pnl-cell.negative {
  color: var(--loss);
}

.position-pnl-rate {
  font-weight: 850;
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
  color: #ff9ba0;
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

  .overview-panel {
    position: static;
  }

  .sleeve-overview {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .section-metrics,
  .estimate-grid,
  .route-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 640px) {
  .shell {
    padding: 12px;
  }

  .topbar {
    position: static;
    align-items: flex-start;
    flex-direction: column;
  }

  .status-strip,
  .portfolio-overview,
  .sleeve-overview,
  .sleeve-sections,
  .section-hero,
  .section-metrics,
  .estimate-grid,
  .route-grid,
  .allocation,
  .sleeve-meta {
    grid-template-columns: 1fr;
  }

  h1 {
    font-size: 22px;
  }
}
"""


APP_JS = """
const sleeveOrderStorageKey = 'leaps.operatorUi.sleeveOrder.v1';

const state = {
  snapshot: null,
  selectedSleeveId: null,
  sleeveOrder: loadSleeveOrder(),
  sleeveSwipeStart: null,
  refreshInFlight: false
};

const autoRefreshMs = 30000;
const sleeveSwipeMinDelta = 60;
const sleeveSwipeMaxVertical = 80;

const $ = (id) => document.getElementById(id);

async function loadSnapshot() {
  if (state.refreshInFlight) return;
  state.refreshInFlight = true;
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
    state.refreshInFlight = false;
    setBusy(false);
  }
}

function startAutoRefresh() {
  window.setInterval(() => {
    if (!document.hidden) loadSnapshot();
  }, autoRefreshMs);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadSnapshot();
  });
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
  renderSleeveOverview(snapshot);
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

function renderSleeveOverview(snapshot) {
  const sections = sleeveSections(snapshot);
  if (!sections.length) {
    $('portfolio-overview').innerHTML = '';
    $('sleeve-overview').innerHTML = empty('No sleeve summary');
    return;
  }
  $('portfolio-overview').innerHTML = renderPortfolioOverview(snapshot, sections);
  const summarySections = summarySectionsByReturn(sections);
  $('sleeve-overview').innerHTML = summarySections.map((section, index) => (
    renderSleeveSummaryCard(section, index, summarySections.length)
  )).join('');
}

function renderPortfolioOverview(snapshot, sections) {
  return [
    portfolioSummaryItem('Equity', aggregateMoneyField(sections, 'equity_by_currency')),
    portfolioSummaryItem('Total P&L', aggregateMoneyField(sections, 'total_pnl_by_currency'), aggregateTone(sections, 'total_pnl_by_currency')),
    portfolioSummaryItem('Today', aggregateTodayField(sections), aggregateTone(sections, 'pnl_since_eod_by_currency')),
    portfolioSummaryItem('Cash check', cashAvailabilityField(snapshot.cash_availability), cashAvailabilityTone(snapshot.cash_availability)),
    portfolioSummaryItem('Sleeves', number(sections.length))
  ].join('');
}

function cashAvailabilityField(report) {
  if (!report) return 'Unavailable';
  const available = moneyMapText(report.available_cash_by_currency || {}, '-');
  if (report.needs_attention) return `Attention - ${available}`;
  if (report.sync_recommended) return `Sync recommended - ${available}`;
  return `Available ${available}`;
}

function cashAvailabilityTone(report) {
  if (!report) return '';
  if (report.needs_attention) return 'negative';
  if (report.sync_recommended) return 'warning';
  return 'positive';
}

function portfolioSummaryItem(label, value, tone = '') {
  const toneClass = tone ? ` ${tone}` : '';
  return `
    <div class="portfolio-summary-item">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value${toneClass}">${escapeHtml(String(value))}</div>
    </div>
  `;
}

function summarySectionsByReturn(sections) {
  return sections
    .map((section, index) => ({ section, index, value: sectionReturnSortValue(section) }))
    .sort((left, right) => {
      if (right.value !== left.value) return right.value - left.value;
      return left.index - right.index;
    })
    .map((row) => row.section);
}

function sectionReturnSortValue(section) {
  const rows = currentReturnValues(section.routes, 'total_return_by_currency');
  if (!rows.length) return Number.NEGATIVE_INFINITY;
  return rows.reduce((sum, row) => sum + Number(row.value || 0), 0) / rows.length;
}

function renderSleeveSummaryCard(section, index, total) {
  const tone = sectionTone(section);
  const hasCurrentReturn = currentReturnAvailable(section.routes, 'total_return_by_currency');
  const summaryReturn = hasCurrentReturn
    ? currentReturnField(section.routes, 'total_return_by_currency')
    : 'Unavailable';
  const summaryReturnLabel = 'Total return';
  const returnTone = hasCurrentReturn
    ? currentReturnTone(section.routes, 'total_return_by_currency') || 'neutral'
    : 'neutral';
  const summaryPnl = currentMoneyField(section.routes, 'total_pnl_by_currency');
  const summaryEquity = currentEquityField(section.routes);
  const todayPnl = currentMoneyField(section.routes, 'pnl_since_eod_by_currency', 'No EOD');
  const todayReturn = currentReturnField(section.routes, 'return_since_eod_by_currency', 'No EOD');
  const todayTone = currentReturnTone(section.routes, 'pnl_since_eod_by_currency');
  const routeText = section.route_labels.join(' / ') || 'route snapshot';
  const displayName = section.display_name || section.sleeve_id || 'unknown';
  return `
    <div class="summary-card" data-sleeve-id="${escapeHtml(section.sleeve_id || 'unknown')}">
      <a class="summary-link ${tone}" href="#${escapeHtml(sectionDomId(section))}" data-sleeve-id="${escapeHtml(section.sleeve_id || 'unknown')}" draggable="false">
        <div class="summary-top">
          <div>
            <div class="summary-name">${escapeHtml(displayName)}</div>
            <div class="summary-meta">${escapeHtml(routeText)}</div>
          </div>
          ${statusBadge(section.open_ticket_count ? 'warn' : 'ok', section.open_ticket_count ? 'Open' : 'Clear')}
        </div>
        <div class="summary-return ${returnTone}">
          <span class="summary-return-label">${escapeHtml(summaryReturnLabel)}</span>
          <span class="summary-return-value-row">
            <span class="summary-return-value">${escapeHtml(summaryReturn)}</span>
            <span class="summary-return-money">${escapeHtml(summaryPnl)}</span>
          </span>
          <span class="summary-today-row ${todayTone || 'neutral'}">
            <span class="summary-today-label">Today</span>
            <span>${escapeHtml(todayPnl)}</span>
            <span>${escapeHtml(todayReturn)}</span>
          </span>
          <span class="summary-return-equity">Equity ${escapeHtml(summaryEquity)}</span>
        </div>
        ${summaryVisuals(section)}
        <div class="summary-mini-grid">
          ${summaryMini('Stock value', currentMoneyField(section.routes, 'stock_market_value_by_currency'))}
          ${summaryMini('Cash', currentMoneyField(section.routes, 'cash_by_currency'))}
          ${summaryMini('Hold/Open', `${number(section.holding_count)} / ${number(section.open_ticket_count)}`)}
        </div>
      </a>
    </div>
  `;
}

function renderSleeves(snapshot) {
  const sections = sleeveSections(snapshot);
  $('sleeve-count').textContent = `${sections.length} sleeves`;
  if (!sections.length) {
    $('sleeves').innerHTML = empty('No sleeve snapshot');
    return;
  }
  const selected = selectedSleeveSection(sections);
  $('sleeves').innerHTML = `
    <div class="sleeve-tabs" role="tablist" aria-label="Sleeves">
      ${sections.map((section, index) => `
        <div class="sleeve-tab-item" data-sleeve-id="${escapeHtml(section.sleeve_id || 'unknown')}">
          ${sleeveOrderButton(section, 'previous', index === 0, '&larr;', 'Move earlier')}
          ${renderSleeveTab(section, section.sleeve_id === selected.sleeve_id)}
          ${sleeveOrderButton(section, 'next', index === sections.length - 1, '&rarr;', 'Move later')}
        </div>
      `).join('')}
    </div>
    <div class="sleeve-detail-nav">
      ${sleeveSlideButton('previous', selected.sleeve_id === sections[0].sleeve_id, '&larr;', 'Previous sleeve')}
      <div class="sleeve-slide-status">${escapeHtml(selected.display_name || selected.sleeve_id || 'unknown')} / ${number(sections.findIndex((section) => section.sleeve_id === selected.sleeve_id) + 1)} of ${number(sections.length)}</div>
      ${sleeveSlideButton('next', selected.sleeve_id === sections[sections.length - 1].sleeve_id, '&rarr;', 'Next sleeve')}
    </div>
    <div class="sleeve-tab-panel" role="tabpanel" aria-labelledby="${escapeHtml(tabDomId(selected))}" data-selected-sleeve-id="${escapeHtml(selected.sleeve_id || 'unknown')}">
      ${renderSleeveSection(selected)}
    </div>
  `;
}

function selectedSleeveSection(sections) {
  if (!state.selectedSleeveId) {
    state.selectedSleeveId = sleeveIdFromHash(sections) || sections[0].sleeve_id;
  }
  const selected = sections.find((section) => section.sleeve_id === state.selectedSleeveId);
  if (selected) return selected;
  state.selectedSleeveId = sections[0].sleeve_id;
  return sections[0];
}

function sleeveIdFromHash(sections) {
  const hash = decodeURIComponent(String(window.location.hash || '').replace(/^#/, ''));
  if (!hash) return null;
  return sections.find((section) => sectionDomId(section) === hash)?.sleeve_id || null;
}

function renderSleeveTab(section, isSelected) {
  const routeText = section.route_labels.join(' / ') || 'route snapshot';
  const displayName = section.display_name || section.sleeve_id || 'unknown';
  const hasCurrentReturn = currentReturnAvailable(section.routes, 'total_return_by_currency');
  const summaryReturn = hasCurrentReturn
    ? currentReturnField(section.routes, 'total_return_by_currency')
    : 'Unavailable';
  const returnTone = hasCurrentReturn
    ? currentReturnTone(section.routes, 'total_return_by_currency') || 'neutral'
    : 'neutral';
  const summaryPnl = currentMoneyField(section.routes, 'total_pnl_by_currency');
  return `
    <button
      id="${escapeHtml(tabDomId(section))}"
      class="sleeve-tab ${sectionTone(section)} ${isSelected ? 'active' : ''}"
      type="button"
      role="tab"
      aria-selected="${isSelected ? 'true' : 'false'}"
      aria-controls="${escapeHtml(sectionDomId(section))}"
      data-sleeve-id="${escapeHtml(section.sleeve_id || 'unknown')}"
    >
      <span class="sleeve-tab-name">${escapeHtml(displayName)}</span>
      <span class="sleeve-tab-return-stack">
        <span class="sleeve-tab-return ${returnTone}">${escapeHtml(summaryReturn)}</span>
        <span class="sleeve-tab-pnl">${escapeHtml(summaryPnl)}</span>
      </span>
      <span class="sleeve-tab-meta">${escapeHtml(routeText)}</span>
    </button>
  `;
}

function sleeveOrderControls(section, index, total) {
  return `
    <div class="sleeve-order-controls">
      ${sleeveOrderButton(section, 'previous', index === 0, '&larr;', 'Move earlier')}
      ${sleeveOrderButton(section, 'next', index === total - 1, '&rarr;', 'Move later')}
    </div>
  `;
}

function sleeveOrderButton(section, direction, disabled, label, title) {
  const sleeveId = section.sleeve_id || 'unknown';
  return `
    <button
      class="sleeve-order-button"
      type="button"
      data-sleeve-id="${escapeHtml(sleeveId)}"
      data-sleeve-order="${escapeHtml(direction)}"
      aria-label="${escapeHtml(`${title} ${sleeveId}`)}"
      title="${escapeHtml(title)}"
      ${disabled ? 'disabled' : ''}
    >${label}</button>
  `;
}

function sleeveSlideButton(direction, disabled, label, title) {
  return `
    <button
      class="sleeve-slide-button"
      type="button"
      data-sleeve-slide="${escapeHtml(direction)}"
      aria-label="${escapeHtml(title)}"
      title="${escapeHtml(title)}"
      ${disabled ? 'disabled' : ''}
    >${label}</button>
  `;
}

function tabDomId(section) {
  return `${sectionDomId(section)}-tab`;
}

function selectAdjacentSleeve(direction) {
  if (!state.snapshot) return;
  const sections = sleeveSections(state.snapshot);
  if (!sections.length) return;
  const current = selectedSleeveSection(sections);
  const index = sections.findIndex((section) => section.sleeve_id === current.sleeve_id);
  const step = direction === 'previous' ? -1 : 1;
  const nextIndex = Math.max(0, Math.min(sections.length - 1, index + step));
  const next = sections[nextIndex];
  if (!next || next.sleeve_id === current.sleeve_id) return;
  selectSleeveTab(next.sleeve_id);
}

function beginSleeveSwipe(event) {
  const panel = event.target.closest?.('.sleeve-tab-panel');
  if (!panel) return;
  if (event.pointerType && !['touch', 'pen'].includes(event.pointerType)) return;
  state.sleeveSwipeStart = {
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY
  };
}

function finishSleeveSwipe(event) {
  const start = state.sleeveSwipeStart;
  if (!start) return;
  if (start.pointerId !== undefined && event.pointerId !== start.pointerId) return;
  state.sleeveSwipeStart = null;
  const dx = event.clientX - start.x;
  const dy = event.clientY - start.y;
  if (Math.abs(dx) < sleeveSwipeMinDelta || Math.abs(dy) > sleeveSwipeMaxVertical) return;
  selectAdjacentSleeve(dx < 0 ? 'next' : 'previous');
}

function selectSleeveTab(sleeveId, options = {}) {
  if (!sleeveId) return;
  state.selectedSleeveId = sleeveId;
  if (state.snapshot) renderSleeves(state.snapshot);
  if (options.scroll) {
    requestAnimationFrame(() => {
      $('sleeves-panel')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

function renderSleeveSection(section) {
  const health = section.open_ticket_count ? ['warn', 'Open'] : ['ok', 'Clear'];
  const displayName = section.display_name || section.sleeve_id || 'unknown';
  return `
    <article id="${escapeHtml(sectionDomId(section))}" class="sleeve-section ${sectionTone(section)}">
      <div class="section-hero">
        <div>
          <div class="section-kicker">Sleeve</div>
          <h3 class="section-title">${escapeHtml(displayName)}</h3>
          <div class="chip-row">
            ${section.route_labels.map((label) => `<span class="chip">${escapeHtml(label)}</span>`).join('')}
            ${section.currencies.map((currency) => `<span class="chip">${escapeHtml(currency)}</span>`).join('')}
          </div>
        </div>
        ${statusBadge(health[0], health[1])}
      </div>
      <div class="section-metrics">
        ${sectionMetric('Total return', currentReturnField(section.routes, 'total_return_by_currency'), currentReturnTone(section.routes, 'total_return_by_currency'))}
        ${sectionMetric('Total P&L', currentMoneyField(section.routes, 'total_pnl_by_currency'), currentReturnTone(section.routes, 'total_pnl_by_currency'))}
        ${sectionMetric('Today +/-', currentMoneyField(section.routes, 'pnl_since_eod_by_currency', 'No EOD'), currentReturnTone(section.routes, 'pnl_since_eod_by_currency'))}
        ${sectionMetric('Today %', currentReturnField(section.routes, 'return_since_eod_by_currency', 'No EOD'), currentReturnTone(section.routes, 'return_since_eod_by_currency'))}
        ${sectionMetric('Realized P&L', currentMoneyField(section.routes, 'realized_pnl_by_currency'), currentReturnTone(section.routes, 'realized_pnl_by_currency'))}
        ${sectionMetric('Unrealized P&L', currentMoneyField(section.routes, 'unrealized_pnl_by_currency'), currentReturnTone(section.routes, 'unrealized_pnl_by_currency'))}
        ${sectionMetric('Current equity', routesMoneyField(section.routes, (route) => currentEstimateCurrencyValue(route.current_estimate, 'equity_by_currency', route.route_currency)), '')}
        ${sectionMetric('Open tickets', number(section.open_ticket_count), section.open_ticket_count ? 'negative' : 'positive')}
      </div>
      ${dailyReturnChart(section)}
      ${strategyDocPanel(section)}
      <div class="route-grid">
        ${section.routes.map(renderRoutePanel).join('')}
      </div>
    </article>
  `;
}

function strategyDocPanel(section) {
  const doc = section.strategy_doc || {};
  const exists = Boolean(doc.exists);
  const title = doc.title || 'STRATEGY.md';
  const meta = exists
    ? `${number(doc.line_count)} lines / ${number(doc.char_count)} chars`
    : 'missing';
  const abstract = exists
    ? (doc.abstract || 'No ABSTRACT section found in STRATEGY.md')
    : 'STRATEGY.md not found in sleeve workspace';
  const rationale = exists ? String(doc.recent_judgment_rationale || '').trim() : '';
  const body = exists
    ? `<pre class="strategy-doc">${escapeHtml(doc.content || '')}</pre>`
    : `<div class="empty">STRATEGY.md not found in sleeve workspace</div>`;
  return `
    <div class="strategy-panel">
      <div class="strategy-brief">
        <div class="strategy-brief-head">
          <span class="strategy-label">Abstract</span>
          <span class="strategy-title">${escapeHtml(title)}</span>
          <span class="strategy-meta">${escapeHtml(meta)}</span>
        </div>
        <p class="strategy-abstract">${escapeHtml(abstract)}</p>
        ${rationale ? `
          <div class="strategy-rationale">
            <div class="strategy-rationale-label">Recent Judgment Rationale</div>
            <p class="strategy-rationale-text">${escapeHtml(rationale)}</p>
          </div>
        ` : ''}
      </div>
      <details class="strategy-details">
        <summary>
          <span class="strategy-toggle-mark" aria-hidden="true"></span>
          <span class="strategy-label">Full STRATEGY.md</span>
          <span class="strategy-meta">${escapeHtml(meta)}</span>
        </summary>
        ${body}
      </details>
    </div>
  `;
}

function renderRoutePanel(sleeve) {
  const portfolio = sleeve.portfolio || {};
  return `
    <div class="route-panel">
      <div class="panel-head">
        <div>
          <div class="route-title">${escapeHtml(sleeve.route_label || 'default route')}</div>
          <div class="route-subtitle">${escapeHtml([sleeve.route_market_scope, sleeve.route_currency].filter(Boolean).join(' / ') || 'route snapshot')}</div>
        </div>
        ${statusBadge(sleeve.open_ticket_count ? 'warn' : 'neutral', sleeve.open_ticket_count ? `${number(sleeve.open_ticket_count)} open` : 'No open')}
      </div>
      ${allocationChart(sleeve.allocation || {}, sleeve.route_currency || '')}
      ${currentEstimatePanel(sleeve)}
      <div class="sleeve-meta">
        ${miniValue('Cash', formatMoneyMap(portfolio.cash_by_currency, portfolio.cash))}
        ${miniValue('Holdings', number(portfolio.holding_count))}
        ${miniValue('Cost basis', money(sleeve.route_currency || '', sleeve.allocation?.total_value || 0))}
        ${miniValue('Pending buy', money(sleeve.route_currency || '', sleeve.pending_buy_notional || 0))}
      </div>
      ${routeTicketList(sleeve)}
    </div>
  `;
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

function sleeveSections(snapshot) {
  const sections = new Map();
  const strategyDocs = snapshot.strategy_docs || {};
  routeSleeves(snapshot).forEach((sleeve) => {
    const sleeveId = sleeve.sleeve_id || 'unknown';
    if (!sections.has(sleeveId)) {
      sections.set(sleeveId, {
        sleeve_id: sleeveId,
        display_name: sleeve.display_name || sleeveId,
        routes: [],
        route_label_set: new Set(),
        currency_set: new Set(),
        open_ticket_count: 0,
        terminal_ticket_count: 0,
        recent_event_count: 0,
        holding_count: 0,
        performance_history: []
      });
    }
    const section = sections.get(sleeveId);
    if (sleeve.display_name) section.display_name = sleeve.display_name;
    section.routes.push(sleeve);
    section.route_label_set.add(sleeve.route_label || 'default route');
    if (sleeve.route_currency) section.currency_set.add(sleeve.route_currency);
    section.open_ticket_count += Number(sleeve.open_ticket_count || 0);
    section.terminal_ticket_count += Number(sleeve.terminal_ticket_count || 0);
    section.recent_event_count += Number(sleeve.recent_event_count || 0);
    section.holding_count += Number(sleeve.portfolio?.holding_count || 0);
  });
  const builtSections = Array.from(sections.values()).map((section) => {
    const currencies = Array.from(section.currency_set).sort();
    return {
      ...section,
      currencies,
      route_labels: Array.from(section.route_label_set).sort(),
      strategy_doc: strategyDocs[section.sleeve_id] || null,
      performance_entries: performanceEntriesForSleeve(snapshot, section.sleeve_id, currencies),
      performance_history: performanceHistoryForSleeve(snapshot, section.sleeve_id, currencies)
    };
  });
  return orderedSleeveSections(builtSections);
}

function orderedSleeveSections(sections) {
  const byId = new Map(sections.map((section) => [section.sleeve_id, section]));
  const ordered = [];
  state.sleeveOrder.forEach((sleeveId) => {
    if (!byId.has(sleeveId)) return;
    ordered.push(byId.get(sleeveId));
    byId.delete(sleeveId);
  });
  return [...ordered, ...byId.values()];
}

function loadSleeveOrder() {
  try {
    const raw = window.localStorage.getItem(sleeveOrderStorageKey);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function saveSleeveOrder(order) {
  state.sleeveOrder = order;
  try {
    window.localStorage.setItem(sleeveOrderStorageKey, JSON.stringify(order));
  } catch {
    // The display order is a local preference; failing to persist should not break the dashboard.
  }
}

function moveSleeveOrder(sleeveId, direction) {
  if (!state.snapshot || !sleeveId) return;
  const sections = sleeveSections(state.snapshot);
  const order = sections.map((section) => section.sleeve_id);
  const index = order.indexOf(sleeveId);
  if (index < 0) return;
  const step = direction === 'previous' ? -1 : 1;
  const nextIndex = Math.max(0, Math.min(order.length - 1, index + step));
  if (nextIndex === index) return;
  const [moved] = order.splice(index, 1);
  order.splice(nextIndex, 0, moved);
  saveSleeveOrder(order);
  renderSleeveOverview(state.snapshot);
  renderSleeves(state.snapshot);
}

function routeSleeves(snapshot) {
  return (snapshot.order_routes || []).flatMap((route) => {
    const label = [route.broker_account_id, route.market_scope, route.currency].filter(Boolean).join(' / ') || 'default route';
    return (route.sleeves || []).map((sleeve) => ({
      ...sleeve,
      performance: performanceForSleeveCurrency(snapshot, sleeve.sleeve_id, route.currency),
      route_label: label,
      route_market_scope: route.market_scope,
      route_broker_account_id: route.broker_account_id,
      route_currency: route.currency
    }));
  }).filter(hasRouteState);
}

function hasRouteState(sleeve) {
  const portfolio = sleeve.portfolio || {};
  const cashValues = Object.values(portfolio.cash_by_currency || {}).map((value) => Number(value || 0));
  const hasCash = Math.abs(Number(portfolio.cash || 0)) > 0 || cashValues.some((value) => Math.abs(value) > 0);
  const hasHoldings = Number(portfolio.holding_count || 0) > 0;
  const hasAllocation = Number(sleeve.allocation?.total_value || 0) > 0;
  const hasOpenTickets = Number(sleeve.open_ticket_count || 0) > 0;
  const hasPendingBuy = Number(sleeve.pending_buy_notional || 0) > 0;
  const hasPendingSell = Object.values(sleeve.pending_sell_quantities || {}).some((value) => Number(value || 0) > 0);
  const estimate = sleeve.current_estimate || {};
  const hasCurrentEstimate = estimate.status !== 'unavailable'
    && (Number(estimate.holding_count || 0) > 0 || currentEstimateCurrencyValue(estimate, 'equity_by_currency', sleeve.route_currency) > 0);
  return hasCash || hasHoldings || hasAllocation || hasCurrentEstimate || hasOpenTickets || hasPendingBuy || hasPendingSell;
}

function performanceForRoute(sleeve) {
  return sleeve.performance || null;
}

function currentEstimatePanel(sleeve) {
  const estimate = sleeve.current_estimate || {};
  const currency = sleeve.route_currency || estimate.primary_currency || '';
  const status = estimate.status || 'unavailable';
  if (status === 'unavailable') {
    return `
      <div class="estimate-panel unavailable">
        <div class="estimate-head">
          <span class="estimate-title">Current estimate</span>
          ${statusBadge('neutral', 'Unavailable')}
        </div>
        <div class="estimate-meta">${escapeHtml(estimate.reason || 'No local snapshot')}</div>
      </div>
    `;
  }
  const badgeTone = status === 'fresh' ? 'ok' : 'warn';
  const age = estimate.age_seconds === null || estimate.age_seconds === undefined
    ? ''
    : `${number(Math.round(Number(estimate.age_seconds || 0)))}s old`;
  return `
    <div class="estimate-panel ${escapeHtml(status)}">
      <div class="estimate-head">
        <span class="estimate-title">Current estimate</span>
        ${statusBadge(badgeTone, status === 'fresh' ? 'Fresh' : 'Stale')}
      </div>
      <div class="estimate-grid">
        ${miniValue('Est. equity', money(currency, currentEstimateCurrencyValue(estimate, 'equity_by_currency', currency)))}
        ${miniValue('Total return', percent(currentEstimateCurrencyValue(estimate, 'total_return_by_currency', currency)))}
        ${miniValue('Today +/-', currentEstimateMoneyField(estimate, 'pnl_since_eod_by_currency', currency, 'No EOD'))}
        ${miniValue('Today %', currentEstimatePercentField(estimate, 'return_since_eod_by_currency', currency, 'No EOD'))}
        ${miniValue('Total P&L', money(currency, currentEstimateCurrencyValue(estimate, 'total_pnl_by_currency', currency)))}
        ${miniValue('Realized P&L', money(currency, currentEstimateCurrencyValue(estimate, 'realized_pnl_by_currency', currency)))}
        ${miniValue('Unrealized P&L', money(currency, currentEstimateCurrencyValue(estimate, 'unrealized_pnl_by_currency', currency)))}
        ${miniValue('Stock value', money(currency, currentEstimateCurrencyValue(estimate, 'stock_market_value_by_currency', currency)))}
        ${miniValue('Cash', money(currency, currentEstimateCurrencyValue(estimate, 'cash_by_currency', currency)))}
      </div>
      <div class="estimate-meta">
        ${escapeHtml([formatTime(estimate.as_of), age, displayPath(estimate.source_path)].filter(Boolean).join(' / '))}
      </div>
      ${positionTargetList(sleeve)}
    </div>
  `;
}

function positionTargetList(sleeve) {
  const estimate = sleeve.current_estimate || {};
  const positions = (estimate.positions || []).filter(isVisiblePortfolioPosition);
  if (!positions.length) return '';
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Symbol</th>
            <th>Qty</th>
            <th>Held %</th>
            <th>Target %</th>
            <th>Delta</th>
            <th>Today %</th>
            <th>P&L %</th>
            <th>Total P&L</th>
            <th>Realized</th>
            <th>Unrealized</th>
          </tr>
        </thead>
        <tbody>
          ${positions.map((position) => `
            <tr>
              <td class="position-symbol-cell">${escapeHtml(position.label || position.symbol || '')}</td>
              <td class="position-number-cell">${number(position.quantity || 0)}</td>
              <td class="position-number-cell">${percent(position.current_percent)}</td>
              <td class="position-number-cell">${percent(position.target_percent)}</td>
              <td class="position-number-cell ${toneForNumber(position.delta_percent)}">${signedPercent(position.delta_percent)}</td>
              <td class="position-number-cell position-pnl-rate ${toneForNumber(position.today_pnl_pct)}">${signedPercent(position.today_pnl_pct)}</td>
              <td class="position-number-cell position-pnl-rate ${toneForNumber(position.total_pnl_pct)}">${signedPercent(position.total_pnl_pct)}</td>
              <td class="position-number-cell position-pnl-cell ${toneForNumber(position.total_pnl)}">${money(position.currency || sleeve.route_currency || '', position.total_pnl || 0)}</td>
              <td class="position-number-cell position-pnl-cell ${toneForNumber(position.realized_pnl)}">${money(position.currency || sleeve.route_currency || '', position.realized_pnl || 0)}</td>
              <td class="position-number-cell position-pnl-cell ${toneForNumber(position.unrealized_pnl)}">${money(position.currency || sleeve.route_currency || '', position.unrealized_pnl || 0)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function isVisiblePortfolioPosition(position) {
  const currentPercent = Math.abs(Number(position.current_percent || 0));
  const quantity = Math.abs(Number(position.quantity || 0));
  return currentPercent >= 0.00005 || quantity > 1e-12;
}

function currentEstimateCurrencyValue(estimate, field, currency) {
  const value = currentEstimateCurrencyRawValue(estimate, field, currency);
  return value === null ? 0 : value;
}

function currentEstimateMoneyField(estimate, field, currency, missingLabel = 'Unavailable') {
  const value = currentEstimateCurrencyRawValue(estimate, field, currency);
  return value === null ? missingLabel : money(currency, value);
}

function currentEstimatePercentField(estimate, field, currency, missingLabel = 'Unavailable') {
  const value = currentEstimateCurrencyRawValue(estimate, field, currency);
  return value === null ? missingLabel : percent(value);
}

function currentEstimateCurrencyRawValue(estimate, field, currency) {
  const values = estimate?.[field] || {};
  if (currency && Object.prototype.hasOwnProperty.call(values, currency)) return Number(values[currency] || 0);
  if (currency) return null;
  const entries = Object.entries(values);
  if (entries.length === 1) return Number(entries[0][1] || 0);
  if (!entries.length) return null;
  return entries.reduce((sum, [, value]) => sum + Number(value || 0), 0);
}

function performanceForSleeveCurrency(snapshot, sleeveId, currency) {
  const latest = snapshot.daily_performance?.latest_by_sleeve_currency || {};
  const entry = latest[`${sleeveId}:${currency || ''}`];
  return entry ? { ...entry, currency: entry.currency || currency || '' } : null;
}

function performanceEntriesForSleeve(snapshot, sleeveId, currencies) {
  const latest = snapshot.daily_performance?.latest_by_sleeve_currency || {};
  const routed = currencies
    .map((currency) => performanceForSleeveCurrency(snapshot, sleeveId, currency))
    .filter(Boolean);
  if (routed.length) return routed;
  return Object.entries(latest)
    .filter(([key]) => key.startsWith(`${sleeveId}:`))
    .map(([key, entry]) => ({ ...entry, currency: entry.currency || key.split(':').pop() || '' }))
    .sort((left, right) => String(left.currency || '').localeCompare(String(right.currency || '')));
}

function performanceHistoryForSleeve(snapshot, sleeveId, currencies) {
  const history = snapshot.daily_performance?.history_by_sleeve_currency || {};
  const routed = currencies
    .map((currency) => history[`${sleeveId}:${currency || ''}`] || [])
    .filter((rows) => rows.length);
  if (routed.length) return routed[0].map((entry) => ({ ...entry, currency: entry.currency || currencies[0] || '' }));
  const first = Object.entries(history)
    .filter(([key]) => key.startsWith(`${sleeveId}:`))
    .sort(([left], [right]) => left.localeCompare(right))[0];
  return first ? first[1].map((entry) => ({ ...entry, currency: entry.currency || first[0].split(':').pop() || '' })) : [];
}

function dailyReturnChart(section) {
  const rows = (section.performance_history || [])
    .filter((row) => row.daily_return !== null && row.daily_return !== undefined && !Number.isNaN(Number(row.daily_return)))
    .slice(-20);
  if (rows.length < 2) return '';
  const values = rows.map((row) => Number(row.daily_return || 0));
  const maxAbs = Math.max(0.001, ...values.map((value) => Math.abs(value)));
  const width = 320;
  const height = 110;
  const padX = 12;
  const baseline = 56;
  const scale = 42 / maxAbs;
  const xStep = rows.length > 1 ? (width - padX * 2) / (rows.length - 1) : 0;
  const points = values.map((value, index) => {
    const x = padX + index * xStep;
    const y = Math.max(10, Math.min(100, baseline - value * scale));
    return { x, y, value, row: rows[index] };
  });
  const path = points.map((point) => `${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(' ');
  const latest = points[points.length - 1];
  const best = Math.max(...values);
  const worst = Math.min(...values);
  return `
    <div class="daily-return-panel">
      <div class="daily-return-head">
        <span class="daily-return-title">Daily return</span>
        <span class="daily-return-meta">${escapeHtml(rows[0].date || '')} - ${escapeHtml(rows[rows.length - 1].date || '')} / ${escapeHtml(latest.row.currency || '')}</span>
      </div>
      <svg class="daily-return-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(`Daily return chart for ${section.sleeve_id}`)}">
        <line class="daily-return-axis" x1="${padX}" y1="${baseline}" x2="${width - padX}" y2="${baseline}"></line>
        <polyline class="daily-return-line" points="${escapeHtml(path)}"></polyline>
        ${points.map((point) => `
          <circle class="daily-return-point ${toneForNumber(point.value)}" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="3">
            <title>${escapeHtml(`${point.row.date || ''} ${signedPercent(point.value)}`)}</title>
          </circle>
        `).join('')}
      </svg>
      <div class="daily-return-footer">
        <span>Latest <strong class="${toneForNumber(latest.value)}">${escapeHtml(signedPercent(latest.value))}</strong></span>
        <span>Best <strong class="${toneForNumber(best)}">${escapeHtml(signedPercent(best))}</strong></span>
        <span>Worst <strong class="${toneForNumber(worst)}">${escapeHtml(signedPercent(worst))}</strong></span>
      </div>
    </div>
  `;
}

function routeTicketList(sleeve) {
  const tickets = (sleeve.open_tickets || []).slice(0, 3);
  if (!tickets.length) return '';
  const hiddenCount = Math.max(0, (sleeve.open_tickets || []).length - tickets.length);
  return `
    <div class="ticket-mini-list">
      ${tickets.map((ticket) => `
        <div class="ticket-mini">
          <div>
            <strong>${escapeHtml(symbolText(ticket.symbol) || 'ticket')}</strong>
            <div class="muted">${escapeHtml(ticket.side || '')} ${number(ticket.remaining_quantity ?? ticket.quantity)} / ${escapeHtml(ticket.status || '')}</div>
          </div>
          ${statusBadge('warn', 'Open')}
        </div>
      `).join('')}
      ${hiddenCount ? `<div class="muted">+${number(hiddenCount)} more open</div>` : ''}
    </div>
  `;
}

function sectionMetric(label, value, tone) {
  const toneClass = tone ? ` ${tone}` : '';
  return `
    <div class="micro-metric">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value${toneClass}">${escapeHtml(String(value))}</div>
    </div>
  `;
}

function performanceField(entries, field, formatter) {
  if (!entries.length) return 'No EOD';
  return entries.map((entry) => {
    const value = entry[field];
    const rendered = value === null || value === undefined ? 'No prior EOD' : formatter(value, entry);
    return entries.length > 1 ? `${entry.currency || ''} ${rendered}`.trim() : rendered;
  }).join(' / ');
}

function performanceMoneyField(entries, field) {
  if (!entries.length) return 'No EOD';
  return entries
    .map((entry) => (entry[field] === null || entry[field] === undefined ? 'No prior EOD' : money(entry.currency || '', entry[field] || 0)))
    .join(' / ');
}

function performanceDateField(entries) {
  if (!entries.length) return 'No EOD';
  return entries.map((entry) => entry.date || 'unknown').join(' / ');
}

function performanceTone(entries, field) {
  const total = entries.reduce((sum, entry) => sum + Number(entry[field] || 0), 0);
  if (total > 0) return 'positive';
  if (total < 0) return 'negative';
  return '';
}

function sectionTone(section) {
  if (section.open_ticket_count > 0) return 'warning';
  const currentTone = currentReturnTone(section.routes, 'total_return_by_currency');
  if (currentTone === 'positive') return 'profit';
  if (currentTone === 'negative') return 'loss';
  if (!currentReturnAvailable(section.routes, 'total_return_by_currency')) return 'warning';
  return 'quiet';
}

function sectionDomId(section) {
  return `sleeve-${slugify(section.sleeve_id || 'unknown')}`;
}

function slugify(value) {
  return String(value)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'unknown';
}

function summaryVisuals(section) {
  const pnlChart = summaryPnlChart(section);
  const assetChart = summaryAssetChart(section);
  if (!pnlChart && !assetChart) return '';
  return `
    <div class="summary-visuals">
      ${pnlChart}
      ${assetChart}
    </div>
  `;
}

function summaryPnlChart(section) {
  const total = currentMoneyDatum(section.routes, 'total_pnl_by_currency');
  const realized = currentMoneyDatum(section.routes, 'realized_pnl_by_currency');
  const unrealized = currentMoneyDatum(section.routes, 'unrealized_pnl_by_currency');
  if (!total && !realized && !unrealized) return '';
  const rows = [
    ['Total', total],
    ['Realized', realized],
    ['Unrealized', unrealized]
  ].filter(([, datum]) => Boolean(datum));
  const max = Math.max(1, ...rows.map(([, datum]) => Math.abs(Number(datum.value || 0))));
  return `
    <div class="summary-chart">
      <div class="summary-chart-title">P&L split</div>
      ${rows.map(([label, datum]) => chartRow(label, datum, max)).join('')}
    </div>
  `;
}

function chartRow(label, datum, max) {
  const value = Number(datum.value || 0);
  const width = Math.min(50, Math.abs(value) / max * 50);
  const sideStyle = value >= 0
    ? `left:50%;width:${width}%`
    : `right:50%;width:${width}%`;
  const tone = value >= 0 ? 'positive' : 'negative';
  return `
    <div class="chart-row">
      <span class="chart-label">${escapeHtml(label)}</span>
      <span class="chart-track">
        <span class="chart-zero"></span>
        <span class="chart-fill ${tone}" style="${sideStyle}"></span>
      </span>
      <span class="chart-value">${escapeHtml(money(datum.currency || '', value))}</span>
    </div>
  `;
}

function summaryAssetChart(section) {
  const stock = currentMoneyDatum(section.routes, 'stock_market_value_by_currency');
  const cash = currentMoneyDatum(section.routes, 'cash_by_currency');
  if (!stock && !cash) return '';
  const currency = stock?.currency || cash?.currency || '';
  const stockValue = Math.max(0, Number(stock?.value || 0));
  const cashValue = Math.max(0, Number(cash?.value || 0));
  const total = stockValue + cashValue;
  if (total <= 0) return '';
  const stockPct = percent(stockValue / total);
  const segments = compactAssetSegments(summaryAssetSegments(section, currency, stockValue, cashValue, total), total);
  const assetLabel = segments.map((segment) => `${segment.label} ${percent(segment.weight)}`).join(', ');
  return `
    <div class="summary-chart">
      <div class="summary-chart-title">Assets</div>
      <div class="asset-donut-layout">
        <div class="asset-donut" style="background:${escapeHtml(assetDonutBackground(segments))}" aria-label="${escapeHtml(assetLabel)}">
          <span class="asset-donut-label">${escapeHtml(stockPct)}</span>
        </div>
        <div class="asset-breakdown">
          <div class="asset-bar">
            ${segments.map((segment, index) => `
              <span style="width:${Math.max(0, segment.weight * 100)}%;background:${colorForSegment(segment, index)}"></span>
            `).join('')}
          </div>
          <div class="asset-holding-list">
            ${segments.map((segment, index) => assetHoldingRow(segment, index, currency)).join('')}
          </div>
        </div>
      </div>
    </div>
  `;
}

function summaryAssetSegments(section, currency, stockValue, cashValue, total) {
  const bySymbol = new Map();
  section.routes.forEach((route) => {
    const estimate = route.current_estimate || {};
    if (!estimate || estimate.status === 'unavailable') return;
    const routeCurrency = route.route_currency || estimate.primary_currency || currency || '';
    if (currency && routeCurrency && routeCurrency !== currency) return;
    (estimate.positions || []).filter(isVisiblePortfolioPosition).forEach((position) => {
      const value = Math.max(0, Number(position.market_value || 0));
      if (!value) return;
      const key = position.symbol || position.label || 'unknown';
      const existing = bySymbol.get(key) || {
        kind: 'holding',
        label: position.label || position.symbol || 'Unknown',
        value: 0
      };
      existing.value += value;
      bySymbol.set(key, existing);
    });
  });
  const holdings = Array.from(bySymbol.values()).sort((left, right) => right.value - left.value);
  const listedStockValue = holdings.reduce((sum, segment) => sum + segment.value, 0);
  const unlistedStockValue = Math.max(0, stockValue - listedStockValue);
  if (unlistedStockValue > Math.max(1, total * 0.001)) {
    holdings.push({ kind: 'holding', label: 'Other stocks', value: unlistedStockValue });
  }
  if (cashValue > 0) {
    holdings.push({ kind: 'cash', label: 'Cash', value: cashValue });
  }
  return holdings.map((segment) => ({ ...segment, weight: segment.value / total }));
}

function compactAssetSegments(segments, total) {
  const stockSegments = segments.filter((segment) => segment.kind !== 'cash');
  const cashSegment = segments.find((segment) => segment.kind === 'cash');
  const visibleStocks = stockSegments.slice(0, 4);
  const hiddenStocks = stockSegments.slice(4);
  const compact = [...visibleStocks];
  const hiddenValue = hiddenStocks.reduce((sum, segment) => sum + Number(segment.value || 0), 0);
  if (hiddenValue > 0) {
    compact.push({
      kind: 'holding',
      label: `Other stocks (${hiddenStocks.length})`,
      value: hiddenValue,
      weight: hiddenValue / total
    });
  }
  if (cashSegment) compact.push(cashSegment);
  return compact;
}

function assetHoldingRow(segment, index, currency) {
  return `
    <div class="asset-holding-row">
      <span class="asset-swatch" style="background:${colorForSegment(segment, index)}"></span>
      <span class="asset-holding-label">${escapeHtml(segment.label || 'Unknown')}</span>
      <span class="asset-holding-value">${escapeHtml(percent(segment.weight))} - ${escapeHtml(money(currency, segment.value))}</span>
    </div>
  `;
}

function assetDonutBackground(segments) {
  return `radial-gradient(circle at center, rgba(11, 15, 22, 0.94) 0 47%, transparent 49%), ${donutBackground(segments)}`;
}

function summaryMini(label, value, tone = '') {
  const toneClass = tone ? ` ${tone}` : '';
  return `
    <div class="summary-mini">
      <div class="label">${escapeHtml(label)}</div>
      <div class="value${toneClass}">${escapeHtml(String(value))}</div>
    </div>
  `;
}

function routesMoneyField(routes, selector) {
  const totals = {};
  routes.forEach((route) => {
    const currency = route.route_currency || route.allocation?.currency || '';
    const value = Number(selector(route) || 0);
    if (!value) return;
    totals[currency] = (totals[currency] || 0) + value;
  });
  const entries = Object.entries(totals);
  if (!entries.length) return '-';
  return entries.map(([currency, value]) => money(currency, value)).join(' / ');
}

function aggregateMoneyField(sections, field, missingLabel = 'Unavailable') {
  const totals = {};
  sections.forEach((section) => {
    section.routes.forEach((route) => {
      const estimate = route.current_estimate || {};
      if (!estimate || estimate.status === 'unavailable') return;
      const currency = route.route_currency || estimate.primary_currency || '';
      let value = currentEstimateCurrencyRawValue(estimate, field, currency);
      if ((value === null || value === undefined) && isPnlMoneyField(field)) value = 0;
      if (value === null || value === undefined || Number.isNaN(Number(value))) return;
      totals[currency] = (totals[currency] || 0) + Number(value || 0);
    });
  });
  const entries = Object.entries(totals);
  if (!entries.length) return missingLabel;
  return entries.map(([currency, value]) => money(currency, value)).join(' / ');
}

function aggregateTodayField(sections, missingLabel = 'No EOD') {
  const totals = {};
  sections.forEach((section) => {
    section.routes.forEach((route) => {
      const estimate = route.current_estimate || {};
      if (!estimate || estimate.status === 'unavailable') return;
      const currency = route.route_currency || estimate.primary_currency || '';
      const pnl = currentEstimateCurrencyRawValue(estimate, 'pnl_since_eod_by_currency', currency);
      const returnValue = currentEstimateCurrencyRawValue(estimate, 'return_since_eod_by_currency', currency);
      if (pnl === null || pnl === undefined || Number.isNaN(Number(pnl))) return;
      const row = totals[currency] || { pnl: 0, basis: 0, hasReturn: false };
      row.pnl += Number(pnl || 0);
      if (returnValue !== null && returnValue !== undefined && !Number.isNaN(Number(returnValue))) {
        const numericReturn = Number(returnValue);
        row.hasReturn = true;
        if (numericReturn !== 0) {
          row.basis += Number(pnl || 0) / numericReturn;
        }
      }
      totals[currency] = row;
    });
  });
  const entries = Object.entries(totals);
  if (!entries.length) return missingLabel;
  return entries.map(([currency, row]) => {
    const moneyText = money(currency, row.pnl);
    if (row.basis) return `${moneyText} (${percent(row.pnl / row.basis)})`;
    if (row.hasReturn && row.pnl === 0) return `${moneyText} (${percent(0)})`;
    return moneyText;
  }).join(' / ');
}

function moneyMapText(values, missingLabel = 'Unavailable') {
  const entries = Object.entries(values || {});
  if (!entries.length) return missingLabel;
  return entries.map(([currency, value]) => money(currency, Number(value || 0))).join(' / ');
}

function aggregateTone(sections, field) {
  let total = 0;
  sections.forEach((section) => {
    section.routes.forEach((route) => {
      const estimate = route.current_estimate || {};
      if (!estimate || estimate.status === 'unavailable') return;
      const currency = route.route_currency || estimate.primary_currency || '';
      const value = currentEstimateCurrencyRawValue(estimate, field, currency);
      if (value === null || value === undefined || Number.isNaN(Number(value))) return;
      total += Number(value || 0);
    });
  });
  if (total > 0) return 'positive';
  if (total < 0) return 'negative';
  return '';
}

function currentReturnValues(routes, field) {
  return routes
    .map((route) => {
      const estimate = route.current_estimate || {};
      if (!estimate || estimate.status === 'unavailable') return null;
      const currency = route.route_currency || estimate.primary_currency || '';
      const value = currentEstimateCurrencyRawValue(estimate, field, currency);
      if (value === null || value === undefined || Number.isNaN(Number(value))) return null;
      return { currency, value: Number(value) };
    })
    .filter(Boolean);
}

function currentReturnAvailable(routes, field) {
  return currentReturnValues(routes, field).length > 0;
}

function currentReturnField(routes, field, missingLabel = 'Unavailable') {
  const rows = currentReturnValues(routes, field);
  if (!rows.length) return missingLabel;
  return rows
    .map((row) => (rows.length > 1 ? `${row.currency || ''} ${percent(row.value)}`.trim() : percent(row.value)))
    .join(' / ');
}

function currentReturnTone(routes, field) {
  const total = currentReturnValues(routes, field).reduce((sum, row) => sum + row.value, 0);
  if (total > 0) return 'positive';
  if (total < 0) return 'negative';
  return '';
}

function currentMoneyField(routes, field, missingLabel = 'Unavailable') {
  const totals = {};
  routes.forEach((route) => {
    const estimate = route.current_estimate || {};
    if (!estimate || estimate.status === 'unavailable') return;
    const currency = route.route_currency || estimate.primary_currency || '';
    let value = currentEstimateCurrencyRawValue(estimate, field, currency);
    if ((value === null || value === undefined) && isPnlMoneyField(field)) value = 0;
    if (value === null || value === undefined || Number.isNaN(Number(value))) return;
    totals[currency] = (totals[currency] || 0) + Number(value || 0);
  });
  const entries = Object.entries(totals);
  if (!entries.length) return missingLabel;
  return entries.map(([currency, value]) => money(currency, value)).join(' / ');
}

function currentEquityField(routes) {
  return routesMoneyField(routes, (route) => (
    currentEstimateCurrencyValue(route.current_estimate, 'equity_by_currency', route.route_currency)
  ));
}

function currentMoneyDatum(routes, field) {
  const totals = {};
  routes.forEach((route) => {
    const estimate = route.current_estimate || {};
    if (!estimate || estimate.status === 'unavailable') return;
    const currency = route.route_currency || estimate.primary_currency || '';
    let value = currentEstimateCurrencyRawValue(estimate, field, currency);
    if ((value === null || value === undefined) && isPnlMoneyField(field)) value = 0;
    if (value === null || value === undefined || Number.isNaN(Number(value))) return;
    totals[currency] = (totals[currency] || 0) + Number(value || 0);
  });
  const entries = Object.entries(totals);
  if (entries.length !== 1) return null;
  const [currency, value] = entries[0];
  return { currency, value: Number(value || 0) };
}

function isPnlMoneyField(field) {
  return ['realized_pnl_by_currency', 'unrealized_pnl_by_currency', 'total_pnl_by_currency'].includes(field);
}

const cashChartColor = '#6c7888';
const holdingChartColors = ['#ff5f6d', '#4d8dff', '#28c76f', '#f7b955', '#b182ff', '#46d7d1', '#d980fa'];

function allocationChart(allocation, currency) {
  const segments = allocationSegments(allocation);
  if (!segments.length) {
    return `
      <div class="allocation">
        <div class="donut" style="background:#e7ebf0"><div class="donut-hole"><strong>-</strong><span>Cost</span></div></div>
        <div class="allocation-legend">${empty('No allocation')}</div>
      </div>
    `;
  }
  const holdingsWeight = segments
    .filter((segment) => segment.kind !== 'cash')
    .reduce((total, segment) => total + segment.weight, 0);
  return `
    <div class="allocation">
      <div class="donut" style="background:${escapeHtml(donutBackground(segments))}">
        <div class="donut-hole"><strong>${escapeHtml(percent(holdingsWeight))}</strong><span>Cost</span></div>
      </div>
      <div class="allocation-legend">
        ${segments.map((segment, index) => `
          <div class="legend-row">
            <span class="legend-swatch" style="background:${colorForSegment(segment, index)}"></span>
            <span class="legend-label">${escapeHtml(segment.label || 'Unknown')}</span>
            <span class="legend-value">${escapeHtml(percent(segment.weight))} &middot; ${escapeHtml(money(currency || allocation.currency || '', segment.value))}</span>
          </div>
        `).join('')}
      </div>
    </div>
  `;
}

function allocationSegments(allocation) {
  const raw = (allocation.segments || [])
    .map((segment) => ({
      ...segment,
      value: Number(segment.value || 0)
    }))
    .filter((segment) => segment.value > 0);
  const total = raw.reduce((sum, segment) => sum + segment.value, 0);
  if (!total) return [];
  return raw
    .map((segment) => ({ ...segment, weight: Number(segment.weight || segment.value / total) }))
    .sort((left, right) => right.value - left.value);
}

function donutBackground(segments) {
  let start = 0;
  const parts = segments.map((segment, index) => {
    const end = Math.min(100, start + (segment.weight * 100));
    const color = colorForSegment(segment, index);
    const part = `${color} ${start.toFixed(3)}% ${end.toFixed(3)}%`;
    start = end;
    return part;
  });
  return `conic-gradient(${parts.join(', ')})`;
}

function colorForSegment(segment, index) {
  if (String(segment.kind || '').toLowerCase() === 'cash') {
    return cashChartColor;
  }
  return holdingChartColors[index % holdingChartColors.length];
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
  return `${currency ? `${currency} ` : ''}${amount.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function number(value) {
  return Number(value || 0).toLocaleString();
}

function percent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  return `${(Number(value) * 100).toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
}

function signedPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '-';
  const numeric = Number(value);
  const prefix = numeric > 0 ? '+' : '';
  return `${prefix}${percent(numeric)}`;
}

function toneForNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'neutral';
  const numeric = Number(value);
  if (numeric > 0) return 'positive';
  if (numeric < 0) return 'negative';
  return 'neutral';
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

document.addEventListener('click', (event) => {
  const slideButton = event.target.closest('[data-sleeve-slide]');
  if (slideButton && slideButton.closest('#sleeves')) {
    event.preventDefault();
    selectAdjacentSleeve(slideButton.dataset.sleeveSlide);
    return;
  }
  const orderButton = event.target.closest('[data-sleeve-order]');
  if (orderButton && orderButton.closest('#sleeve-overview, #sleeves')) {
    event.preventDefault();
    event.stopPropagation();
    moveSleeveOrder(orderButton.dataset.sleeveId, orderButton.dataset.sleeveOrder);
    return;
  }
  const trigger = event.target.closest('[data-sleeve-id]');
  if (!trigger || !trigger.closest('#sleeve-overview, #sleeves')) return;
  event.preventDefault();
  selectSleeveTab(trigger.dataset.sleeveId, { scroll: trigger.classList.contains('summary-link') });
});

document.addEventListener('pointerdown', beginSleeveSwipe);
document.addEventListener('pointerup', finishSleeveSwipe);
document.addEventListener('pointercancel', () => {
  state.sleeveSwipeStart = null;
});

$('refresh-button').addEventListener('click', loadSnapshot);
startAutoRefresh();
loadSnapshot();
"""

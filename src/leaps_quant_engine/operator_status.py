from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


DEFAULT_EOD_SCHEDULES = (
    "18:05|krx-after-hours",
    "06:10|us-after-hours",
)


@dataclass(frozen=True, slots=True)
class CashAvailabilityRouteInput:
    account_id: str | None
    market_scope: str | None
    currency: str
    account_store_path: Path
    default_cash_by_sleeve: Mapping[str, float]


def build_cash_availability_report(
    *,
    runtime_id: str,
    sleeve_ids: Sequence[str],
    routes: Sequence[CashAvailabilityRouteInput],
    residual_sleeve_id: str = "default sleeve",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    generated = generated_at or datetime.now().astimezone()
    route_payloads: list[dict[str, Any]] = []
    totals_by_currency: dict[str, float] = {}
    warnings: list[str] = []
    needs_attention = False
    sync_recommended = False

    for route in routes:
        account_id = route.account_id or "default"
        store = VirtualSleeveAccountStore(
            route.account_store_path,
            default_cash_by_sleeve=dict(route.default_cash_by_sleeve),
            default_currency=route.currency,
        )
        report = store.cash_reconciliation_report(
            account_id=account_id,
            currency=route.currency,
            residual_sleeve_id=residual_sleeve_id,
        )
        snapshot_payload = _account_cash_snapshot_payload(route.account_store_path, account_id, route.currency)
        available_cash = float(report.residual_cash)
        totals_by_currency[route.currency] = totals_by_currency.get(route.currency, 0.0) + available_cash
        if snapshot_payload is None:
            warnings.append(f"missing_cash_snapshot:{account_id}:{route.currency}")
            needs_attention = True
        if report.difference > 1e-6:
            warnings.append(f"virtual_cash_exceeds_broker_snapshot:{account_id}:{route.currency}:{report.difference}")
            needs_attention = True
        elif report.difference < -1e-6:
            warnings.append(f"broker_snapshot_above_virtual_cash_sync_recommended:{account_id}:{route.currency}:{report.difference}")
            sync_recommended = True

        route_payloads.append(
            {
                "account_id": account_id,
                "market_scope": route.market_scope,
                "currency": route.currency,
                "account_store_path": str(route.account_store_path),
                "broker_cash_balance": report.broker_cash_balance,
                "virtual_cash_total": report.virtual_cash_total,
                "difference": report.difference,
                "residual_sleeve_id": residual_sleeve_id,
                "available_cash": available_cash,
                "sleeve_cash": {
                    sleeve_id: float(report.sleeve_cash.get(sleeve_id, 0.0))
                    for sleeve_id in sleeve_ids
                },
                "all_sleeve_cash": dict(report.sleeve_cash),
                "cash_snapshot_synced_at": (
                    snapshot_payload.get("synced_at")
                    if isinstance(snapshot_payload, Mapping)
                    else None
                ),
            }
        )

    return {
        "generated_at": generated.isoformat(),
        "runtime_id": runtime_id,
        "sleeve_ids": list(sleeve_ids),
        "residual_sleeve_id": residual_sleeve_id,
        "route_count": len(route_payloads),
        "available_cash_by_currency": totals_by_currency,
        "sync_recommended": sync_recommended,
        "needs_attention": needs_attention,
        "warnings": warnings,
        "routes": route_payloads,
    }


def build_eod_snapshot_status(
    *,
    snapshot_root: Path | str = Path("data/eod-snapshots"),
    state_dir: Path | str = Path("data/runtime/eod-snapshots"),
    schedules: Sequence[str] = DEFAULT_EOD_SCHEDULES,
    now: datetime | None = None,
) -> dict[str, Any]:
    root = Path(snapshot_root)
    state = Path(state_dir)
    generated = now or datetime.now().astimezone()
    today = generated.date().isoformat()
    schedule_by_label = _parse_schedules(schedules)
    markers = _load_eod_markers(state)
    manifests = _load_eod_manifests(root)
    labels = tuple(sorted(set(schedule_by_label) | set(markers) | set(manifests)))
    entries = [
        _eod_label_status(
            label,
            today=today,
            now=generated,
            schedule_time=schedule_by_label.get(label),
            markers=markers.get(label, ()),
            manifests=manifests.get(label, ()),
        )
        for label in labels
    ]
    needs_attention = any(entry["status"] in {"failed_today", "missing_today"} for entry in entries)
    return {
        "generated_at": generated.isoformat(),
        "snapshot_root": str(root),
        "state_dir": str(state),
        "today": today,
        "needs_attention": needs_attention,
        "label_count": len(entries),
        "labels": entries,
    }


def _account_cash_snapshot_payload(account_store_path: Path, account_id: str, currency: str) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(account_store_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    snapshots = payload.get("account_cash_snapshots") if isinstance(payload, Mapping) else None
    if not isinstance(snapshots, Mapping):
        return None
    code = str(currency or "KRW").upper()
    for key in (f"{account_id}:{code}", account_id if code == "KRW" else "", f"default:{code}", "default"):
        if key and isinstance(snapshots.get(key), Mapping):
            return snapshots[key]
    return None


def _parse_schedules(schedules: Sequence[str]) -> dict[str, time]:
    parsed: dict[str, time] = {}
    for raw in schedules:
        parts = str(raw).split("|")
        if len(parts) < 2:
            continue
        hour_text, minute_text = parts[0].split(":", 1)
        parsed[parts[1]] = time(hour=int(hour_text), minute=int(minute_text))
    return parsed


def _load_eod_markers(state_dir: Path) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not state_dir.exists():
        return {}
    for path in sorted(state_dir.glob("*.done")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        date = str(payload.get("date") or path.name.split("_", 1)[0])
        label = str(payload.get("label") or path.stem.split("_", 1)[-1])
        grouped.setdefault(label, []).append(
            {
                "date": date,
                "label": label,
                "exit_code": payload.get("exit_code"),
                "attempted_at": payload.get("attempted_at"),
                "target": payload.get("target"),
                "path": str(path),
            }
        )
    return {label: tuple(items) for label, items in grouped.items()}


def _load_eod_manifests(snapshot_root: Path) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not snapshot_root.exists():
        return {}
    for path in sorted(snapshot_root.glob("*/*/manifest_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        label = str(payload.get("label") or path.parent.name)
        grouped.setdefault(label, []).append(
            {
                "date": str(payload.get("snapshot_date") or path.parent.parent.name),
                "label": label,
                "generated_at": payload.get("generated_at"),
                "path": str(path),
                "target_count": len(payload.get("targets") or []) if isinstance(payload.get("targets"), list) else None,
            }
        )
    return {label: tuple(items) for label, items in grouped.items()}


def _eod_label_status(
    label: str,
    *,
    today: str,
    now: datetime,
    schedule_time: time | None,
    markers: tuple[dict[str, Any], ...],
    manifests: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    latest_marker = _latest_by_date(markers)
    latest_manifest = _latest_by_date(manifests)
    today_markers = [marker for marker in markers if marker.get("date") == today]
    today_marker = today_markers[-1] if today_markers else None
    if today_marker is not None:
        status = "ok_today" if int(today_marker.get("exit_code") or 0) == 0 else "failed_today"
    elif schedule_time is not None and now.time() < schedule_time:
        status = "scheduled"
    elif schedule_time is not None:
        status = "missing_today"
    else:
        status = "no_today_marker"
    return {
        "label": label,
        "schedule_time": schedule_time.strftime("%H:%M") if schedule_time is not None else None,
        "status": status,
        "today_marker": today_marker,
        "latest_marker": latest_marker,
        "latest_manifest": latest_manifest,
    }


def _latest_by_date(items: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not items:
        return None
    return sorted(items, key=lambda item: (str(item.get("date") or ""), str(item.get("attempted_at") or item.get("generated_at") or "")))[-1]

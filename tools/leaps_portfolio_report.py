from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unicodedata
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "runtime" / "live_multi_sleeve.json"
DEFAULT_OUT_DIR = ROOT / "data" / "runtime" / "portfolio-reports"
DEFAULT_ACCOUNT_STORE = ROOT / "data" / "virtual-accounts" / "kis_domestic.json"
DEFAULT_FRAMEWORK_STATE_DIR = ROOT / "data" / "runtime" / "framework-state"
DEFAULT_MULTI_FRAMEWORK_STATE_DIR = DEFAULT_FRAMEWORK_STATE_DIR / "multi-sleeve"
DEFAULT_LIVE_ORDER_BATCH = ROOT / "data" / "runtime" / "live-order-loop" / "multi_sleeve_candidate_orders.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a LEaps current-vs-target portfolio report.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--sleeve-id", default="LEaps")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--account-store")
    parser.add_argument("--framework-state")
    parser.add_argument("--order-batch", default=str(DEFAULT_LIVE_ORDER_BATCH))
    parser.add_argument("--order-status-json")
    parser.add_argument(
        "--mode",
        choices=("latest-target", "fast-current", "recompute"),
        default="latest-target",
        help=(
            "latest-target reads the latest live-cycle artifacts, fast-current reads "
            "account/order state only, and recompute runs runtime-run-once."
        ),
    )
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--title", default="LEaps Portfolio Report")
    parser.add_argument("--layout", choices=("mobile", "table"), default="mobile")
    args = parser.parse_args()

    config_path = Path(args.config)
    account_store_path = _account_store_path(config_path, args.sleeve_id, args.account_store)
    framework_state_path = _framework_state_path(args.framework_state, args.sleeve_id)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"{args.sleeve_id}_runtime_{timestamp}.json"
    order_batch_path = out_dir / f"{args.sleeve_id}_orders_{timestamp}.json"
    message_path = out_dir / f"{args.sleeve_id}_message_{timestamp}.txt"

    if args.mode == "recompute":
        payload = _run_runtime_once(
            config=config_path,
            sleeve_id=args.sleeve_id,
            order_batch_path=order_batch_path,
            framework_state_path=framework_state_path,
        )
        payload.setdefault("report_source", {})["mode"] = "recompute"
    else:
        order_status_payload = _load_order_status_payload(
            config=config_path,
            sleeve_id=args.sleeve_id,
            order_status_json=Path(args.order_status_json) if args.order_status_json else None,
        )
        payload = _build_fast_report_payload(
            config=config_path,
            sleeve_id=args.sleeve_id,
            mode=args.mode,
            account_store_path=account_store_path,
            framework_state_path=framework_state_path,
            order_batch_path=Path(args.order_batch),
            order_status_payload=order_status_payload,
        )
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    symbol_names = _symbol_names_from_config(config_path, args.sleeve_id)
    account_payload = _read_json(account_store_path)
    realized = _realized_pnl_from_account_store(account_payload, sleeve_id=args.sleeve_id)
    message = _format_report(
        payload,
        sleeve_id=args.sleeve_id,
        symbol_names=symbol_names,
        realized_pnl=realized["total"],
        realized_pnl_by_symbol=realized["by_symbol"],
        layout=args.layout,
    )
    message_path.write_text(message, encoding="utf-8")
    print(message)

    if args.notify:
        _send_notification(title=args.title, message_path=message_path)
    return 0


def _run_runtime_once(
    *,
    config: Path,
    sleeve_id: str,
    order_batch_path: Path,
    framework_state_path: Path | None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [
        sys.executable,
        "-m",
        "leaps_quant_engine.cli",
        "runtime-run-once",
        str(config),
        "--sleeve-id",
        sleeve_id,
        "--order-batch-output",
        str(order_batch_path),
    ]
    if framework_state_path is not None and framework_state_path.exists():
        command.extend(
            [
                "--framework-state",
                str(framework_state_path),
                "--framework-state-read-only",
            ]
        )
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError(f"runtime-run-once produced no JSON output. stderr={completed.stderr.strip()}")
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "runtime-run-once output was not valid JSON. "
            f"returncode={completed.returncode}, stderr={completed.stderr.strip()}, stdout_prefix={stdout[:500]}"
        ) from exc


def _framework_state_path(value: str | None, sleeve_id: str) -> Path | None:
    if value:
        return Path(value)
    safe_sleeve_id = "".join(char if char.isalnum() or char in "._-" else "_" for char in sleeve_id)
    multi_sleeve_path = DEFAULT_MULTI_FRAMEWORK_STATE_DIR / f"{safe_sleeve_id}.json"
    if multi_sleeve_path.exists():
        return multi_sleeve_path
    return DEFAULT_FRAMEWORK_STATE_DIR / f"{safe_sleeve_id}.json"


def _account_store_path(config: Path, sleeve_id: str, value: str | None) -> Path:
    if value:
        return Path(value)
    config_payload = _read_json(config)
    sleeve = _sleeve_payload(config_payload, sleeve_id)
    account_id = None
    if sleeve:
        routes = sleeve.get("broker_account_routes")
        if isinstance(routes, Mapping) and routes:
            account_id = str(next(iter(routes.values())))
        account_id = account_id or str(sleeve.get("broker_account_id") or "")
    for account in config_payload.get("broker_accounts", []) or []:
        if not isinstance(account, Mapping) or str(account.get("account_id") or "") != account_id:
            continue
        raw_path = account.get("account_store_path")
        if raw_path:
            return _resolve_config_path(config, Path(str(raw_path)))
    return DEFAULT_ACCOUNT_STORE


def _load_order_status_payload(
    *,
    config: Path,
    sleeve_id: str,
    order_status_json: Path | None,
) -> dict[str, Any]:
    if order_status_json is not None:
        return _read_json(order_status_json)
    try:
        return _run_order_runtime_status(config=config, sleeve_id=sleeve_id)
    except RuntimeError as exc:
        return {"error": str(exc), "needs_attention": True, "routes": []}


def _run_order_runtime_status(*, config: Path, sleeve_id: str) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [
        sys.executable,
        "-m",
        "leaps_quant_engine.cli",
        "order-runtime-status",
        str(config),
        "--sleeve-id",
        sleeve_id,
        "--recent-events",
        "5",
        "--summary-only",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout.strip()
    if completed.returncode != 0 or not stdout:
        raise RuntimeError(
            "order-runtime-status failed. "
            f"returncode={completed.returncode}, stderr={completed.stderr.strip()}, stdout={stdout[:500]}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "order-runtime-status output was not valid JSON. "
            f"returncode={completed.returncode}, stderr={completed.stderr.strip()}, stdout_prefix={stdout[:500]}"
        ) from exc


def _build_fast_report_payload(
    *,
    config: Path,
    sleeve_id: str,
    mode: str,
    account_store_path: Path,
    framework_state_path: Path | None,
    order_batch_path: Path,
    order_status_payload: Mapping[str, Any],
) -> dict[str, Any]:
    config_payload = _read_json(config)
    account_payload = _read_json(account_store_path)
    framework_state = _read_json(framework_state_path) if framework_state_path is not None else {}
    order_batch = _read_json(order_batch_path)
    orders = _orders_for_sleeve(order_batch, sleeve_id)
    price_by_symbol = _latest_price_by_symbol(framework_state, orders)
    order_runtime_summary = _order_runtime_summary_for_sleeve(order_status_payload, sleeve_id)
    currency = _default_currency_for_sleeve(config_payload, sleeve_id, order_runtime_summary)
    current = _current_portfolio_snapshot(
        sleeve_id=sleeve_id,
        account_payload=account_payload,
        order_runtime_summary=order_runtime_summary,
        price_by_symbol=price_by_symbol,
        currency=currency,
    )
    cycle_quality = _latest_cycle_snapshot_quality(config, config_payload, sleeve_id)
    warnings = _fast_report_warnings(
        mode=mode,
        framework_state_path=framework_state_path,
        framework_state=framework_state,
        order_batch_path=order_batch_path,
        order_batch=order_batch,
        order_status_payload=order_status_payload,
    )
    framework_payload = _framework_payload_from_artifacts(
        mode=mode,
        framework_state=framework_state,
        orders=orders,
        current=current,
    )
    return {
        "report_source": {
            "mode": mode,
            "generated_at": datetime.now().isoformat(),
            "config": str(config),
            "account_store_path": str(account_store_path),
            "framework_state_path": str(framework_state_path) if framework_state_path is not None else None,
            "framework_state_updated_at": framework_state.get("updated_at"),
            "order_batch_path": str(order_batch_path),
            "order_batch_generated_at": order_batch.get("generated_at"),
            "read_only": True,
        },
        "engine_status": {
            "framework": {
                "active_insight_count": len(framework_state.get("active_insights", []) or []),
            },
            "snapshot": {
                "status": cycle_quality.get("status"),
                "updated_symbol_count": cycle_quality.get("collected_symbol_count"),
            },
        },
        "worker": {"cycles": [{"snapshot_quality": cycle_quality}]},
        "portfolio_state": {"current": current},
        "framework": framework_payload,
        "order_runtime_status": order_runtime_summary,
        "warnings": warnings,
    }


def _fast_report_warnings(
    *,
    mode: str,
    framework_state_path: Path | None,
    framework_state: Mapping[str, Any],
    order_batch_path: Path,
    order_batch: Mapping[str, Any],
    order_status_payload: Mapping[str, Any],
) -> list[str]:
    warnings: list[str] = []
    if order_status_payload.get("error"):
        warnings.append(f"order-runtime-status read failed: {order_status_payload.get('error')}")
    if mode == "latest-target":
        if framework_state_path is None or not framework_state:
            warnings.append("latest framework-state artifact missing; target view may be incomplete")
        if not order_batch:
            warnings.append("latest order-batch artifact missing; candidate order view may be incomplete")
    return warnings


def _framework_payload_from_artifacts(
    *,
    mode: str,
    framework_state: Mapping[str, Any],
    orders: list[Mapping[str, Any]],
    current: Mapping[str, Any],
) -> dict[str, Any]:
    if mode == "fast-current":
        return {
            "risk": {"decisions": []},
            "execution": {"order_count": 0},
            "order_intents": [],
        }
    order_plans = _target_plans_from_framework_state(framework_state)
    order_plans_by_symbol = {str(plan.get("symbol") or ""): dict(plan) for plan in order_plans if plan.get("symbol")}
    current_quantities = {
        str(item.get("symbol")): int(item.get("quantity") or 0)
        for item in current.get("holdings", []) or []
        if item.get("symbol")
    }
    for order in orders:
        symbol = str(order.get("symbol") or "")
        if not symbol:
            continue
        metadata = order.get("metadata") if isinstance(order.get("metadata"), Mapping) else {}
        target_quantity = metadata.get("target_quantity")
        if target_quantity is None:
            current_quantity = int(metadata.get("current_quantity") or current_quantities.get(symbol, 0))
            quantity = int(order.get("quantity") or 0)
            side = str(order.get("side") or "").lower()
            target_quantity = current_quantity + quantity if side == "buy" else current_quantity - quantity
        order_plans_by_symbol[symbol] = {
            "symbol": symbol,
            "target_quantity": int(target_quantity or 0),
            "risk_status": "approved",
            "risk_reason": "",
            "reason": "latest_candidate_order",
        }
    return {
        "portfolio_target_batch": framework_state.get("last_portfolio_target_batch") or {},
        "order_sizing": {"plans": list(order_plans_by_symbol.values())},
        "risk": {"decisions": []},
        "execution": {"order_count": len(orders)},
        "order_intents": [dict(order) for order in orders],
    }


def _target_plans_from_framework_state(framework_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    batch = framework_state.get("last_portfolio_target_batch")
    if not isinstance(batch, Mapping):
        return []
    plans: list[dict[str, Any]] = []
    for plan in batch.get("plans", []) or []:
        if not isinstance(plan, Mapping):
            continue
        symbol = str(plan.get("symbol") or "")
        if not symbol:
            continue
        target_quantity = _target_quantity_from_plan(plan)
        plans.append(
            {
                "symbol": symbol,
                "target_quantity": target_quantity,
                "risk_status": "latest_target",
                "risk_reason": "",
                "reason": "latest_live_target",
                "target_percent": plan.get("target_percent"),
            }
        )
    return plans


def _target_quantity_from_plan(plan: Mapping[str, Any]) -> int:
    price = _float_or_none(plan.get("current_price"))
    desired_value = _float_or_none(plan.get("desired_value"))
    if price is not None and price > 0 and desired_value is not None:
        return max(int(desired_value // price), 0)
    return max(int(plan.get("current_quantity") or 0), 0)


def _orders_for_sleeve(order_batch: Mapping[str, Any], sleeve_id: str) -> list[Mapping[str, Any]]:
    orders: list[Mapping[str, Any]] = []
    for batch in order_batch.get("batches", []) or []:
        if not isinstance(batch, Mapping) or str(batch.get("sleeve_id") or "") != sleeve_id:
            continue
        for order in batch.get("orders", []) or []:
            if isinstance(order, Mapping):
                orders.append(order)
    return orders


def _latest_price_by_symbol(
    framework_state: Mapping[str, Any],
    orders: list[Mapping[str, Any]],
) -> dict[str, float]:
    prices: dict[str, float] = {}
    batch = framework_state.get("last_portfolio_target_batch")
    if isinstance(batch, Mapping):
        for plan in batch.get("plans", []) or []:
            if not isinstance(plan, Mapping):
                continue
            symbol = str(plan.get("symbol") or "")
            price = _float_or_none(plan.get("current_price"))
            if symbol and price is not None and price > 0:
                prices[symbol] = price
    for order in orders:
        symbol = str(order.get("symbol") or "")
        price = _float_or_none(order.get("reference_price")) or _float_or_none(order.get("limit_price"))
        if symbol and price is not None and price > 0:
            prices.setdefault(symbol, price)
    return prices


def _order_runtime_summary_for_sleeve(
    order_status_payload: Mapping[str, Any],
    sleeve_id: str,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "needs_attention": bool(order_status_payload.get("needs_attention")),
        "open_ticket_count": 0,
        "pending_buy_notional": 0.0,
        "pending_sell_quantities": {},
        "open_tickets": [],
        "sleeve_portfolios": [],
    }
    for route in order_status_payload.get("routes", []) or []:
        if not isinstance(route, Mapping):
            continue
        currency = str(route.get("currency") or "").upper()
        runtime = route.get("order_runtime") if isinstance(route.get("order_runtime"), Mapping) else {}
        for ticket in runtime.get("open_tickets", []) or []:
            if not isinstance(ticket, Mapping) or str(ticket.get("sleeve_id") or "") != sleeve_id:
                continue
            enriched = dict(ticket)
            if currency:
                enriched.setdefault("currency", currency)
            enriched.setdefault("broker_account_id", route.get("broker_account_id"))
            summary["open_tickets"].append(enriched)
        for sleeve in route.get("sleeves", []) or []:
            if not isinstance(sleeve, Mapping) or str(sleeve.get("sleeve_id") or "") != sleeve_id:
                continue
            sleeve_payload = dict(sleeve)
            sleeve_payload.setdefault("currency", currency)
            sleeve_payload.setdefault("market_scope", route.get("market_scope"))
            summary["sleeve_portfolios"].append(sleeve_payload)
            summary["open_ticket_count"] += int(sleeve.get("open_ticket_count") or 0)
            summary["pending_buy_notional"] += float(sleeve.get("pending_buy_notional") or 0.0)
            pending_sells = sleeve.get("pending_sell_quantities") if isinstance(sleeve.get("pending_sell_quantities"), Mapping) else {}
            for symbol, quantity in pending_sells.items():
                summary["pending_sell_quantities"][str(symbol)] = (
                    float(summary["pending_sell_quantities"].get(str(symbol), 0.0)) + float(quantity or 0.0)
                )
    if not summary["open_ticket_count"]:
        summary["open_ticket_count"] = len(summary["open_tickets"])
    return summary


def _default_currency_for_sleeve(
    config_payload: Mapping[str, Any],
    sleeve_id: str,
    order_runtime_summary: Mapping[str, Any],
) -> str:
    portfolios = order_runtime_summary.get("sleeve_portfolios", []) or []
    for portfolio in portfolios:
        currency = str(portfolio.get("currency") or "").upper() if isinstance(portfolio, Mapping) else ""
        if currency:
            return currency
    sleeve = _sleeve_payload(config_payload, sleeve_id)
    account_id = None
    if sleeve:
        routes = sleeve.get("broker_account_routes")
        if isinstance(routes, Mapping) and routes:
            account_id = str(next(iter(routes.values())))
        account_id = account_id or str(sleeve.get("broker_account_id") or "")
    for account in config_payload.get("broker_accounts", []) or []:
        if isinstance(account, Mapping) and str(account.get("account_id") or "") == account_id:
            currency = str(account.get("currency") or "").upper()
            if currency:
                return currency
    return "KRW"


def _current_portfolio_snapshot(
    *,
    sleeve_id: str,
    account_payload: Mapping[str, Any],
    order_runtime_summary: Mapping[str, Any],
    price_by_symbol: Mapping[str, float],
    currency: str,
) -> dict[str, Any]:
    portfolios = order_runtime_summary.get("sleeve_portfolios", []) or []
    if portfolios:
        portfolio = _merge_order_status_portfolios(portfolios, currency)
    else:
        portfolio = _portfolio_from_account_store(account_payload, sleeve_id, currency)
    holdings = [
        _enriched_holding(item, price_by_symbol=price_by_symbol)
        for item in portfolio.get("holdings", []) or []
        if isinstance(item, Mapping)
    ]
    cash_by_currency = dict(portfolio.get("cash_by_currency") or {})
    cash = _float_or_none(portfolio.get("cash"))
    if cash is None:
        cash = float(sum(float(value or 0.0) for value in cash_by_currency.values()))
    exposure = sum(float(item.get("market_value") or 0.0) for item in holdings)
    equity = cash + exposure
    result = {
        "currency": currency,
        "cash": cash,
        "cash_by_currency": cash_by_currency or ({currency: cash} if currency else {}),
        "equity": equity,
        "gross_exposure": exposure,
        "gross_exposure_pct": exposure / equity if equity > 0 else 0.0,
        "holdings": holdings,
    }
    if currency:
        result["equity_by_currency"] = {currency: equity}
    return result


def _merge_order_status_portfolios(portfolios: list[Mapping[str, Any]], currency: str) -> dict[str, Any]:
    cash_by_currency: dict[str, float] = {}
    holdings: dict[str, dict[str, Any]] = {}
    for sleeve_payload in portfolios:
        portfolio = sleeve_payload.get("portfolio") if isinstance(sleeve_payload.get("portfolio"), Mapping) else {}
        for key, value in dict(portfolio.get("cash_by_currency") or {}).items():
            cash_by_currency[str(key).upper()] = cash_by_currency.get(str(key).upper(), 0.0) + float(value or 0.0)
        if not portfolio.get("cash_by_currency") and portfolio.get("cash") is not None:
            cash_by_currency[currency] = cash_by_currency.get(currency, 0.0) + float(portfolio.get("cash") or 0.0)
        for item in portfolio.get("holdings", []) or []:
            if not isinstance(item, Mapping):
                continue
            symbol = _symbol_key_from_holding(item)
            if not symbol:
                continue
            existing = holdings.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "quantity": 0.0,
                    "average_price": 0.0,
                    "cost_basis": 0.0,
                },
            )
            quantity = float(item.get("quantity") or 0.0)
            average_price = float(item.get("average_price") or 0.0)
            existing["quantity"] = float(existing.get("quantity") or 0.0) + quantity
            existing["cost_basis"] = float(existing.get("cost_basis") or 0.0) + quantity * average_price
            if existing["quantity"] > 0:
                existing["average_price"] = existing["cost_basis"] / existing["quantity"]
    cash = sum(cash_by_currency.values())
    return {"cash": cash, "cash_by_currency": cash_by_currency, "holdings": list(holdings.values())}


def _portfolio_from_account_store(
    account_payload: Mapping[str, Any],
    sleeve_id: str,
    currency: str,
) -> dict[str, Any]:
    sleeve = (account_payload.get("sleeves") or {}).get(sleeve_id)
    if not isinstance(sleeve, Mapping):
        return {"cash": 0.0, "cash_by_currency": {currency: 0.0}, "holdings": []}
    holdings_payload = sleeve.get("holdings") or {}
    holdings = holdings_payload.values() if isinstance(holdings_payload, Mapping) else holdings_payload
    return {
        "cash": float(sleeve.get("cash") or 0.0),
        "cash_by_currency": dict(sleeve.get("cash_by_currency") or ({currency: float(sleeve.get("cash") or 0.0)})),
        "holdings": [
            {
                "symbol": _symbol_key_from_holding(item),
                "quantity": item.get("quantity"),
                "average_price": item.get("average_price"),
            }
            for item in holdings
            if isinstance(item, Mapping) and _symbol_key_from_holding(item)
        ],
    }


def _enriched_holding(
    item: Mapping[str, Any],
    *,
    price_by_symbol: Mapping[str, float],
) -> dict[str, Any]:
    symbol = _symbol_key_from_holding(item)
    quantity = float(item.get("quantity") or 0.0)
    average_price = float(item.get("average_price") or 0.0)
    market_price = _float_or_none(item.get("market_price")) or price_by_symbol.get(symbol) or average_price
    market_value = quantity * market_price
    cost_basis = quantity * average_price
    pnl = market_value - cost_basis
    return {
        "symbol": symbol,
        "quantity": int(quantity) if quantity.is_integer() else quantity,
        "average_price": average_price,
        "market_price": market_price,
        "market_value": market_value,
        "cost_basis": cost_basis,
        "unrealized_pnl": pnl,
        "unrealized_pnl_pct": pnl / cost_basis if cost_basis > 0 else None,
    }


def _symbol_key_from_holding(item: Mapping[str, Any]) -> str:
    raw_symbol = item.get("symbol")
    if isinstance(raw_symbol, str):
        if ":" in raw_symbol:
            return raw_symbol
        market = str(item.get("market") or "").strip()
        return f"{market}:{raw_symbol}" if market else raw_symbol
    if isinstance(raw_symbol, Mapping):
        ticker = str(raw_symbol.get("ticker") or "").strip()
        market = str(raw_symbol.get("market") or item.get("market") or "").strip()
        return f"{market}:{ticker}" if market and ticker else ticker
    ticker = str(item.get("ticker") or "").strip()
    market = str(item.get("market") or "").strip()
    return f"{market}:{ticker}" if market and ticker else ticker


def _latest_cycle_snapshot_quality(
    config: Path,
    config_payload: Mapping[str, Any],
    sleeve_id: str,
) -> dict[str, Any]:
    journal_path = _journal_path(config, config_payload)
    entry = _latest_cycle_entry(journal_path, sleeve_id) if journal_path is not None else {}
    if not entry:
        return {"status": "unknown", "collected_symbol_count": None, "requested_symbol_count": None}
    counts = entry.get("counts") if isinstance(entry.get("counts"), Mapping) else {}
    updated_count = entry.get("updated_symbol_count", counts.get("updated_symbol_count"))
    failed_count = entry.get("failed_symbol_count", counts.get("failed_symbol_count"))
    requested_count = entry.get("requested_symbol_count", counts.get("requested_symbol_count"))
    if requested_count is None and updated_count is not None and failed_count is not None:
        requested_count = int(updated_count or 0) + int(failed_count or 0)
    return {
        "status": entry.get("snapshot_status"),
        "collected_symbol_count": updated_count,
        "requested_symbol_count": requested_count,
        "failed_symbol_count": failed_count,
        "source": entry.get("source"),
        "ended_at": entry.get("ended_at") or entry.get("generated_at"),
    }


def _journal_path(config: Path, config_payload: Mapping[str, Any]) -> Path | None:
    raw_path = config_payload.get("journal_path")
    if not raw_path:
        return None
    return _resolve_config_path(config, Path(str(raw_path)))


def _latest_cycle_entry(journal_path: Path, sleeve_id: str) -> dict[str, Any]:
    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if str(entry.get("sleeve_id") or "") != sleeve_id:
            continue
        if entry.get("source") not in {"runtime-run-once", "runtime-run-multi-once"}:
            continue
        if entry.get("snapshot_status") is None:
            continue
        return entry
    return {}


def _send_notification(*, title: str, message_path: Path) -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    command = [
        sys.executable,
        "-m",
        "leaps_quant_engine.cli",
        "notify-user-message",
        "--title",
        title,
        "--message-file",
        str(message_path),
        "--root",
        str(ROOT / "data" / "notification-engine"),
        "--summary-only",
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"notify-user-message failed: {completed.stderr.strip() or completed.stdout.strip()}")
    try:
        result = json.loads(completed.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"notify-user-message returned invalid JSON: {completed.stdout.strip()}") from exc
    delivery_mode = str(result.get("delivery_mode") or "").lower()
    delivery_status = str(result.get("delivery_status") or "").lower()
    if delivery_status == "failed" or (delivery_mode == "telegram" and delivery_status != "sent"):
        raise RuntimeError(f"notify-user-message delivery failed: {completed.stdout.strip()}")


def _format_report(
    payload: dict[str, Any],
    *,
    sleeve_id: str,
    symbol_names: Mapping[str, str] | None = None,
    realized_pnl: float | None = None,
    realized_pnl_by_symbol: Mapping[str, float] | None = None,
    layout: str = "mobile",
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current = payload.get("portfolio_state", {}).get("current", {}) or {}
    framework = payload.get("framework", {}) or {}
    engine_status = payload.get("engine_status", {}) or {}
    risk = framework.get("risk", {}) or {}
    execution = framework.get("execution", {}) or {}
    snapshot_quality = _snapshot_quality(payload)
    warnings = payload.get("warnings") or []
    report_source = payload.get("report_source", {}) or {}
    order_runtime_status = payload.get("order_runtime_status", {}) or {}

    current_by_symbol = {
        item.get("symbol"): item
        for item in current.get("holdings", []) or []
        if item.get("symbol")
    }
    target_by_symbol = _target_by_symbol(current_by_symbol, framework, risk)
    symbol_names = symbol_names or {}
    realized_pnl_by_symbol = realized_pnl_by_symbol or {}

    unrealized = _total_unrealized_pnl(current)
    total_pnl = unrealized + float(realized_pnl or 0.0)
    current_cost_basis = _total_cost_basis(current)
    total_pnl_pct = total_pnl / current_cost_basis if current_cost_basis > 0 else None
    equity = _float_or_none(current.get("equity"))
    cash = _float_or_none(current.get("cash"))
    exposure = _float_or_none(current.get("gross_exposure"))
    exposure_pct = current.get("gross_exposure_pct")
    currency = _portfolio_currency(current)

    lines = [
        f"[{sleeve_id}] 운용 현황",
        f"기준: {now}",
        f"소스: {_report_source_label(report_source)}",
        f"데이터: {snapshot_quality.get('status', 'unknown')} "
        f"({snapshot_quality.get('collected_symbol_count', '?')}/{snapshot_quality.get('requested_symbol_count', '?')})",
        "",
        "요약",
        f"- 총 평가액: {_money(equity, currency)} {currency}",
        f"- 현금: {_money(cash, currency)} {currency}",
        f"- 주식 평가액: {_money(exposure, currency)} {currency} ({_pct(exposure_pct)})",
        f"- 주문 후보: {execution.get('order_count', 0)}건",
        f"- 미체결 티켓: {_open_ticket_count(order_runtime_status)}건",
        f"- 활성 인사이트: {engine_status.get('framework', {}).get('active_insight_count', '-')}",
        "",
        "손익",
        f"- 미실현: {_signed_money(unrealized, currency)} {currency} ({_pct(_total_unrealized_pnl_pct(current))})",
        f"- 누적 실현 추정: {_signed_money(realized_pnl, currency)} {currency}",
        f"- 합산 추정: {_signed_money(total_pnl, currency)} {currency} ({_pct(total_pnl_pct)})",
        "- 참고: 실현손익은 체결 원장 FIFO 기반 누적 추정입니다.",
        "",
        "보유/목표",
    ]

    symbols = sorted(
        set(current_by_symbol) | set(target_by_symbol),
        key=lambda symbol: abs(float(current_by_symbol.get(symbol, {}).get("market_value") or 0.0)),
        reverse=True,
    )
    if not symbols:
        lines.append("- 보유/목표 없음")
    elif layout == "table":
        table_lines, detail_lines = _holding_table_lines(
            symbols,
            current_by_symbol=current_by_symbol,
            target_by_symbol=target_by_symbol,
            symbol_names=symbol_names,
            realized_pnl_by_symbol=realized_pnl_by_symbol,
            currency=currency,
        )
        lines.extend(_markdown_code_block(table_lines))
        if detail_lines:
            lines.extend(["", "메모"])
            lines.extend(f"- {line}" for line in detail_lines)
    else:
        lines.extend(
            _holding_mobile_lines(
                symbols,
                current_by_symbol=current_by_symbol,
                target_by_symbol=target_by_symbol,
                symbol_names=symbol_names,
                realized_pnl_by_symbol=realized_pnl_by_symbol,
                currency=currency,
            )
        )

    order_lines = _order_lines(framework, symbol_names, currency, layout=layout)
    if order_lines:
        lines.extend(["", "주문 후보"])
        if layout == "table":
            lines.extend(_markdown_code_block(order_lines))
        else:
            lines.extend(order_lines)

    open_ticket_lines = _open_ticket_lines(order_runtime_status, symbol_names, currency, layout=layout)
    if open_ticket_lines:
        lines.extend(["", "미체결 주문"])
        if layout == "table":
            lines.extend(_markdown_code_block(open_ticket_lines))
        else:
            lines.extend(open_ticket_lines)

    blend_lines = _portfolio_blend_lines(framework)
    if blend_lines:
        lines.extend(["", "Portfolio Blend"])
        lines.extend(f"- {line}" for line in blend_lines)

    attention = _attention_lines(snapshot_quality, execution, risk, warnings)
    if order_runtime_status.get("needs_attention"):
        attention.append("Order runtime needs_attention=true")
    if attention:
        lines.extend(["", "확인 필요"])
        lines.extend(f"- {line}" for line in attention)
    return "\n".join(lines).strip() + "\n"


def _holding_mobile_lines(
    symbols: list[str],
    *,
    current_by_symbol: Mapping[str, Mapping[str, Any]],
    target_by_symbol: Mapping[str, Mapping[str, Any]],
    symbol_names: Mapping[str, str],
    realized_pnl_by_symbol: Mapping[str, float],
    currency: str,
) -> list[str]:
    lines: list[str] = []
    for symbol in symbols:
        current_item = current_by_symbol.get(symbol, {})
        target_item = target_by_symbol.get(symbol, {})
        current_qty = int(current_item.get("quantity", 0) or 0)
        target_qty = int(target_item.get("target_quantity", current_qty) or 0)
        delta = target_qty - current_qty
        pnl = float(current_item.get("unrealized_pnl") or 0.0)
        realized_symbol = float(realized_pnl_by_symbol.get(symbol) or 0.0)

        lines.append(f"- {_mobile_symbol_label(symbol, symbol_names)}")
        lines.append(f"  수량 {current_qty}주 -> {target_qty}주 ({_delta_cell(delta)})")
        if current_qty:
            lines.append(
                "  "
                f"현재 {_money(current_item.get('market_price'), currency)} / "
                f"평단 {_money(current_item.get('average_price'), currency)} / "
                f"평가 {_money(current_item.get('market_value'), currency)}"
            )
            lines.append(f"  미실현 {_pnl_cell(pnl, current_item.get('unrealized_pnl_pct'), currency)}")
        elif target_qty:
            lines.append("  현재 미보유")

        if abs(realized_symbol) >= 0.5:
            realized_line = f"  누적실현 {_signed_money(realized_symbol, currency)}"
            if current_qty:
                realized_line += f" / 보유+누적 {_signed_money(pnl + realized_symbol, currency)}"
            lines.append(realized_line)

        risk_note = _risk_note(target_item)
        if risk_note:
            lines.append(f"  {risk_note}")
    return lines


def _report_source_label(report_source: Mapping[str, Any]) -> str:
    mode = str(report_source.get("mode") or "unknown")
    if mode == "recompute":
        return "recompute 새 계산"
    if mode == "latest-target":
        updated_at = report_source.get("framework_state_updated_at") or report_source.get("order_batch_generated_at")
        suffix = f" ({updated_at})" if updated_at else ""
        return f"latest live-cycle target{suffix}"
    if mode == "fast-current":
        return "fast current account/order state"
    return mode


def _open_ticket_count(order_runtime_status: Mapping[str, Any]) -> int:
    value = order_runtime_status.get("open_ticket_count")
    if value is not None:
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return len(order_runtime_status.get("open_tickets", []) or [])


def _open_ticket_lines(
    order_runtime_status: Mapping[str, Any],
    symbol_names: Mapping[str, str],
    currency: str,
    *,
    layout: str = "mobile",
) -> list[str]:
    tickets = [ticket for ticket in order_runtime_status.get("open_tickets", []) or [] if isinstance(ticket, Mapping)]
    if not tickets:
        return []
    rows: list[list[str]] = []
    lines: list[str] = []
    for ticket in tickets[:8]:
        symbol = str(ticket.get("symbol") or "")
        side = _side_label(ticket.get("side"))
        quantity = int(ticket.get("remaining_quantity") or ticket.get("quantity") or 0)
        status = str(ticket.get("status") or "-")
        limit_price = ticket.get("limit_price")
        broker_order_id = ticket.get("broker_order_id") or "-"
        if layout == "table":
            rows.append(
                [
                    _format_symbol(symbol, symbol_names),
                    side,
                    f"{quantity}주",
                    _money(limit_price, currency),
                    status,
                    str(broker_order_id),
                ]
            )
        else:
            lines.append(
                f"- {_mobile_symbol_label(symbol, symbol_names)} {side} {quantity}주 "
                f"limit {_money(limit_price, currency)} / {status} / {broker_order_id}"
            )
    if layout != "table":
        if len(tickets) > 8:
            lines.append(f"- 외 {len(tickets) - 8}건")
        return lines
    table = _format_table(
        ["종목", "방향", "잔량", "지정가", "상태", "주문번호"],
        rows,
        max_widths=[22, 4, 5, 10, 10, 18],
        right_align={2, 3},
    )
    if len(tickets) > 8:
        table.append(f"외 {len(tickets) - 8}건")
    return table


def _snapshot_quality(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    cycles = payload.get("worker", {}).get("cycles", []) or []
    if cycles:
        return cycles[-1].get("snapshot_quality", {}) or {}
    status_snapshot = payload.get("engine_status", {}).get("snapshot", {}) or {}
    return {
        "status": status_snapshot.get("status"),
        "collected_symbol_count": status_snapshot.get("updated_symbol_count"),
        "requested_symbol_count": status_snapshot.get("updated_symbol_count"),
    }


def _portfolio_blend_lines(framework: Mapping[str, Any]) -> list[str]:
    blend = _portfolio_blend_payload(framework)
    if not blend or str(blend.get("status") or "disabled") == "disabled":
        return []
    status = str(blend.get("status") or "unknown")
    progress = blend.get("progress")
    elapsed = _float_or_none(blend.get("elapsed_minutes"))
    duration = _float_or_none(blend.get("duration_minutes"))
    drift = _float_or_none(blend.get("target_drift"))
    transition_id = str(blend.get("transition_id") or "")
    bypassed = [str(item) for item in blend.get("bypassed_symbols", []) or []]

    lines = [f"status {status}"]
    if progress is not None:
        lines[0] += f" / progress {_pct(progress)}"
    if elapsed is not None and duration is not None and duration > 0:
        lines.append(f"elapsed {elapsed:.0f}/{duration:.0f} minutes")
    if drift is not None:
        lines.append(f"target drift {_pct(drift)}")
    if transition_id:
        lines.append(f"id {transition_id}")
    if bypassed:
        lines.append("bypassed " + ", ".join(bypassed))
    return lines


def _portfolio_blend_payload(framework: Mapping[str, Any]) -> Mapping[str, Any]:
    batch = framework.get("portfolio_target_batch", {}) or {}
    metadata = batch.get("metadata", {}) if isinstance(batch, Mapping) else {}
    blend = metadata.get("portfolio_blend") if isinstance(metadata, Mapping) else None
    if isinstance(blend, Mapping):
        return blend
    stage_decisions = framework.get("stage_decisions", {}) or {}
    portfolio_stage = stage_decisions.get("portfolio", {}) if isinstance(stage_decisions, Mapping) else {}
    blend = portfolio_stage.get("portfolio_blend") if isinstance(portfolio_stage, Mapping) else None
    return blend if isinstance(blend, Mapping) else {}


def _portfolio_currency(current: Mapping[str, Any]) -> str:
    for key in ("cash_by_currency", "equity_by_currency"):
        values = current.get(key)
        if isinstance(values, Mapping) and len(values) == 1:
            currency = next(iter(values.keys()), "")
            if currency:
                return str(currency).upper()
    return str(current.get("currency") or "KRW").upper()


def _target_by_symbol(
    current_by_symbol: Mapping[str, Mapping[str, Any]],
    framework: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    targets: dict[str, dict[str, Any]] = {
        symbol: {
            "target_quantity": int(item.get("quantity", 0) or 0),
            "risk_status": "hold",
            "risk_reason": "no_delta",
        }
        for symbol, item in current_by_symbol.items()
    }
    order_sizing = framework.get("order_sizing", {}) or {}
    for plan in order_sizing.get("plans", []) or []:
        symbol = plan.get("symbol")
        if symbol:
            risk_status = plan.get("risk_status")
            risk_reason = plan.get("risk_reason")
            targets[symbol] = {
                "target_quantity": plan.get("target_quantity"),
                "risk_status": risk_status if risk_status is not None else "pending_risk",
                "risk_reason": risk_reason if risk_reason is not None else plan.get("reason", ""),
            }
    for decision in risk.get("decisions", []) or []:
        symbol = decision.get("symbol")
        if not symbol:
            continue
        approved = decision.get("approved_quantity")
        targets[symbol] = {
            "target_quantity": approved if approved is not None else decision.get("original_quantity"),
            "risk_status": decision.get("status"),
            "risk_reason": decision.get("reason"),
            "risk_metadata": decision.get("metadata") or {},
        }
    return targets


def _holding_table_lines(
    symbols: list[str],
    *,
    current_by_symbol: Mapping[str, Mapping[str, Any]],
    target_by_symbol: Mapping[str, Mapping[str, Any]],
    symbol_names: Mapping[str, str],
    realized_pnl_by_symbol: Mapping[str, float],
    currency: str,
) -> tuple[list[str], list[str]]:
    rows: list[list[str]] = []
    detail_lines: list[str] = []
    for symbol in symbols:
        current_item = current_by_symbol.get(symbol, {})
        target_item = target_by_symbol.get(symbol, {})
        current_qty = int(current_item.get("quantity", 0) or 0)
        target_qty = int(target_item.get("target_quantity", current_qty) or 0)
        delta = target_qty - current_qty
        pnl = float(current_item.get("unrealized_pnl") or 0.0)
        rows.append(
            [
                _format_symbol(symbol, symbol_names),
                f"{current_qty}주",
                f"{target_qty}주",
                _delta_cell(delta),
                _money(current_item.get("market_price"), currency),
                _money(current_item.get("average_price"), currency),
                _pnl_cell(pnl, current_item.get("unrealized_pnl_pct"), currency),
            ]
        )
        realized_symbol = float(realized_pnl_by_symbol.get(symbol) or 0.0)
        if abs(realized_symbol) >= 0.5:
            detail_lines.append(f"{_format_symbol(symbol, symbol_names)} 누적실현 {_signed_money(realized_symbol, currency)}")
        risk_note = _risk_note(target_item)
        if risk_note:
            detail_lines.append(f"{_format_symbol(symbol, symbol_names)} {risk_note}")
    return _format_table(
        ["종목", "보유", "목표", "증감", "현재가", "평단", "손익"],
        rows,
        max_widths=[22, 5, 5, 8, 10, 10, 16],
        right_align={1, 2, 4, 5, 6},
    ), detail_lines


def _order_lines(
    framework: Mapping[str, Any],
    symbol_names: Mapping[str, str],
    currency: str,
    *,
    layout: str = "mobile",
) -> list[str]:
    rows: list[list[str]] = []
    mobile_lines: list[str] = []
    for order in framework.get("order_intents", []) or []:
        symbol = str(order.get("symbol") or "")
        side = _side_label(order.get("side"))
        quantity = int(order.get("quantity") or 0)
        price = order.get("reference_price")
        if layout == "table":
            rows.append([_format_symbol(symbol, symbol_names), side, f"{quantity}주", _money(price, currency)])
        else:
            mobile_lines.append(f"- {_mobile_symbol_label(symbol, symbol_names)} {side} {quantity}주 @ {_money(price, currency)}")
    if layout != "table":
        return mobile_lines
    if not rows:
        return []
    return _format_table(
        ["종목", "방향", "수량", "기준가"],
        rows,
        max_widths=[22, 4, 5, 10],
        right_align={2, 3},
    )


def _mobile_symbol_label(symbol: str, symbol_names: Mapping[str, str]) -> str:
    name = symbol_names.get(symbol) or _COMMON_SYMBOL_NAMES.get(symbol)
    if not name:
        return symbol
    ticker = symbol.split(":", 1)[1] if ":" in symbol else symbol
    return f"{name} ({ticker})"


def _markdown_code_block(lines: list[str]) -> list[str]:
    return ["```", *lines, "```"]


def _format_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    max_widths: list[int],
    right_align: set[int] | None = None,
) -> list[str]:
    right_align = right_align or set()
    clipped_rows = [
        [_truncate_display(str(cell), max_widths[index]) for index, cell in enumerate(row)]
        for row in rows
    ]
    clipped_headers = [
        _truncate_display(header, max_widths[index])
        for index, header in enumerate(headers)
    ]
    widths = [
        min(
            max_widths[index],
            max(
                [_display_width(clipped_headers[index])]
                + [_display_width(row[index]) for row in clipped_rows]
            ),
        )
        for index in range(len(headers))
    ]

    def row_line(values: list[str]) -> str:
        return "| " + " | ".join(
            _pad_display(value, widths[index], right=index in right_align)
            for index, value in enumerate(values)
        ) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    return [row_line(clipped_headers), separator, *(row_line(row) for row in clipped_rows)]


def _truncate_display(text: str, limit: int) -> str:
    if _display_width(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    result = ""
    width = 0
    for char in text:
        char_width = _display_width(char)
        if width + char_width > limit - 1:
            break
        result += char
        width += char_width
    return result + "…"


def _pad_display(text: str, width: int, *, right: bool = False) -> str:
    padding = max(width - _display_width(text), 0)
    if right:
        return " " * padding + text
    return text + " " * padding


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _delta_cell(delta: int) -> str:
    if delta > 0:
        return f"+{delta} 매수"
    if delta < 0:
        return f"{delta} 매도"
    return "유지"


def _side_label(side: Any) -> str:
    text = str(side or "").lower()
    if text == "buy":
        return "매수"
    if text == "sell":
        return "매도"
    return str(side or "-")


def _pnl_cell(pnl: float, pnl_pct: Any, currency: str) -> str:
    pct = _pct(pnl_pct)
    if pct == "-":
        return _signed_money(pnl, currency)
    return f"{_signed_money(pnl, currency)} {pct}"


def _attention_lines(
    snapshot_quality: Mapping[str, Any],
    execution: Mapping[str, Any],
    risk: Mapping[str, Any],
    warnings: list[Any],
) -> list[str]:
    lines: list[str] = []
    if snapshot_quality.get("status") not in {None, "ok", "fresh"}:
        lines.append(
            "데이터 상태 확인: "
            f"{snapshot_quality.get('status')} "
            f"({snapshot_quality.get('collected_symbol_count', '?')}/{snapshot_quality.get('requested_symbol_count', '?')})"
        )
    adjusted = [
        decision for decision in risk.get("decisions", []) or []
        if str(decision.get("status") or "").lower() in {"rejected", "blocked", "clamped"}
    ]
    if adjusted:
        lines.append(f"Risk 조정/차단 {len(adjusted)}건")
        for decision in adjusted[:5]:
            lines.append(
                f"{decision.get('symbol')}: {decision.get('status')} "
                f"({_friendly_risk_reason(decision.get('reason'), decision.get('metadata') or {})})"
            )
    order_count = int(execution.get("order_count", 0) or 0)
    if order_count:
        lines.append(f"이번 cycle 주문 후보 {order_count}건 있음")
    for warning in warnings[:3]:
        lines.append(str(warning))
    return lines


def _risk_note(target_item: Mapping[str, Any]) -> str:
    status = str(target_item.get("risk_status") or "")
    reason = str(target_item.get("risk_reason") or "")
    if status in {"", "hold", "approved", "latest_target", "latest_candidate"} and reason in {"", "no_delta"}:
        return ""
    return f"리스크 {status}:{_friendly_risk_reason(reason, target_item.get('risk_metadata') or {})}"


def _friendly_risk_reason(reason: Any, metadata: Mapping[str, Any] | None = None) -> str:
    text = str(reason or "reason_missing")
    metadata = metadata or {}
    if text == "insufficient_cash_or_position_too_small":
        available_cash = _money(metadata.get("available_cash"), metadata.get("currency"))
        return f"추가매수 불가(현금/노출한도; 가용 {available_cash})"
    if text == "exposure_limit_no_room":
        limit = metadata.get("max_total_exposure_pct")
        regime = metadata.get("market_regime") or {}
        regime_name = regime.get("name") if isinstance(regime, Mapping) else None
        suffix = f", regime {regime_name}" if regime_name else ""
        return f"총 노출한도 꽉 참(한도 {_pct(limit)}{suffix})"
    if text == "position_limit_no_room":
        return f"종목 비중한도 꽉 참(한도 {_pct(metadata.get('max_position_pct'))})"
    if text == "cash_limit_no_room":
        available_cash = _money(metadata.get("available_cash"), metadata.get("currency"))
        return f"추가매수 현금 부족(가용 {available_cash})"
    if text == "risk_clamped_to_current":
        return "리스크 한도 때문에 현재수량 유지"
    if text == "target_reduction_blocked":
        return "감축 목표가 리스크 한도에서 현재수량으로 보정됨"
    if text == "snapshot_quality_blocks_entry":
        return "데이터 신선도 부족으로 신규진입 차단"
    if text == "missing_or_invalid_price":
        return "가격 없음/비정상"
    if text == "short_target_rejected":
        return "공매도/음수 목표 차단"
    if text == "currency_policy_clamped":
        return "통화/노출 정책으로 수량 조정"
    return text


def _format_symbol(symbol: str, symbol_names: Mapping[str, str]) -> str:
    name = symbol_names.get(symbol) or _COMMON_SYMBOL_NAMES.get(symbol)
    if not name:
        return symbol
    return f"{symbol} {name}"


def _symbol_names_from_config(config: Path, sleeve_id: str) -> dict[str, str]:
    config_payload = _read_json(config)
    sleeve = _sleeve_payload(config_payload, sleeve_id)
    if not sleeve:
        return {}
    universe_path = sleeve.get("universe", {}).get("coarse_path")
    if not universe_path:
        return {}
    universe_payload = _read_json(_resolve_config_path(config, Path(str(universe_path))))
    names: dict[str, str] = {}
    for raw in universe_payload.get("symbols", []) or []:
        if isinstance(raw, str):
            market = str(universe_payload.get("market") or "").strip()
            if market:
                names.setdefault(f"{market}:{raw}", "")
            continue
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or raw.get("symbol") or "").strip()
        market = str(raw.get("market") or universe_payload.get("market") or "").strip()
        name = str(raw.get("name") or raw.get("display_name") or raw.get("company_name") or "").strip()
        if ticker and market and name:
            names[f"{market}:{ticker}"] = name
    return {key: value for key, value in names.items() if value}


def _sleeve_payload(config_payload: Mapping[str, Any], sleeve_id: str) -> Mapping[str, Any] | None:
    for sleeve in config_payload.get("sleeves", []) or []:
        if isinstance(sleeve, Mapping) and sleeve.get("sleeve_id") == sleeve_id:
            return sleeve
    return None


def _resolve_config_path(config: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    root = config.resolve().parents[2] if len(config.resolve().parents) >= 3 else ROOT
    candidate = root / path
    if candidate.exists():
        return candidate
    return config.resolve().parent / path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _realized_pnl_from_account_store(account_payload: Mapping[str, Any], *, sleeve_id: str) -> dict[str, Any]:
    fills = sorted(
        (
            fill for fill in (account_payload.get("fills") or {}).values()
            if isinstance(fill, Mapping) and fill.get("sleeve_id") == sleeve_id
        ),
        key=lambda fill: str(fill.get("filled_at") or ""),
    )
    lots: dict[str, deque[list[float]]] = defaultdict(deque)
    realized_by_symbol: dict[str, float] = defaultdict(float)
    total = 0.0
    for fill in fills:
        symbol_payload = fill.get("symbol") if isinstance(fill.get("symbol"), Mapping) else {}
        market = symbol_payload.get("market")
        ticker = symbol_payload.get("ticker")
        if not market or not ticker:
            continue
        symbol = f"{market}:{ticker}"
        quantity = float(fill.get("quantity") or 0.0)
        price = float(fill.get("fill_price") or 0.0)
        side = str(fill.get("side") or "").lower()
        if quantity <= 0 or price <= 0:
            continue
        if side == "buy":
            lots[symbol].append([quantity, price])
            continue
        if side != "sell":
            continue
        remaining = quantity
        while remaining > 0 and lots[symbol]:
            lot_quantity, lot_price = lots[symbol][0]
            matched = min(lot_quantity, remaining)
            pnl = (price - lot_price) * matched
            total += pnl
            realized_by_symbol[symbol] += pnl
            lot_quantity -= matched
            remaining -= matched
            if lot_quantity <= 0:
                lots[symbol].popleft()
            else:
                lots[symbol][0][0] = lot_quantity
    return {"total": total, "by_symbol": dict(realized_by_symbol)}


_COMMON_SYMBOL_NAMES = {
    "KRX:005930": "삼성전자",
    "KRX:000660": "SK하이닉스",
    "KRX:005380": "현대차",
    "KRX:000270": "기아",
    "KRX:035420": "NAVER",
    "KRX:035720": "카카오",
    "KRX:068270": "셀트리온",
    "KRX:207940": "삼성바이오로직스",
    "KRX:051910": "LG화학",
    "KRX:006400": "삼성SDI",
    "KRX:105560": "KB금융",
    "KRX:055550": "신한지주",
    "KRX:086790": "하나금융지주",
    "KRX:028260": "삼성물산",
    "KRX:034020": "두산에너빌리티",
    "KRX:012450": "한화에어로스페이스",
}


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if str(currency or "").upper() == "USD":
        return f"{number:,.2f}"
    return f"{number:,.0f}"


def _signed_money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if str(currency or "").upper() == "USD":
        return f"{number:+,.2f}"
    return f"{number:+,.0f}"


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        percent = float(value) * 100
        if percent != 0 and abs(percent) < 0.1:
            return f"{percent:.2f}%"
        return f"{percent:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _total_unrealized_pnl(current: Mapping[str, Any]) -> float:
    total = 0.0
    for item in current.get("holdings", []) or []:
        try:
            total += float(item.get("unrealized_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _total_cost_basis(current: Mapping[str, Any]) -> float:
    total = 0.0
    for item in current.get("holdings", []) or []:
        try:
            total += float(item.get("cost_basis") or 0.0)
        except (TypeError, ValueError):
            continue
    return total


def _total_unrealized_pnl_pct(current: Mapping[str, Any]) -> float | None:
    cost = 0.0
    pnl = 0.0
    for item in current.get("holdings", []) or []:
        try:
            cost += float(item.get("cost_basis") or 0.0)
            pnl += float(item.get("unrealized_pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    if cost <= 0:
        return None
    return pnl / cost


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs" / "runtime" / "leaps_workspace_smoke.json"
DEFAULT_OUT_DIR = ROOT / "data" / "runtime" / "portfolio-reports"
DEFAULT_ACCOUNT_STORE = ROOT / "data" / "virtual-accounts" / "kis_domestic.json"
DEFAULT_FRAMEWORK_STATE_DIR = ROOT / "data" / "runtime" / "framework-state"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a LEaps current-vs-target portfolio report.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--sleeve-id", default="LEaps")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--account-store", default=str(DEFAULT_ACCOUNT_STORE))
    parser.add_argument("--framework-state")
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--title", default="LEaps 포트폴리오 리포트")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"{args.sleeve_id}_runtime_{timestamp}.json"
    order_batch_path = out_dir / f"{args.sleeve_id}_orders_{timestamp}.json"
    message_path = out_dir / f"{args.sleeve_id}_message_{timestamp}.txt"

    payload = _run_runtime_once(
        config=Path(args.config),
        sleeve_id=args.sleeve_id,
        order_batch_path=order_batch_path,
        framework_state_path=_framework_state_path(args.framework_state, args.sleeve_id),
    )
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    symbol_names = _symbol_names_from_config(Path(args.config), args.sleeve_id)
    account_payload = _read_json(Path(args.account_store))
    realized = _realized_pnl_from_account_store(account_payload, sleeve_id=args.sleeve_id)
    message = _format_report(
        payload,
        sleeve_id=args.sleeve_id,
        symbol_names=symbol_names,
        realized_pnl=realized["total"],
        realized_pnl_by_symbol=realized["by_symbol"],
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
    return DEFAULT_FRAMEWORK_STATE_DIR / f"{safe_sleeve_id}.json"


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


def _format_report(
    payload: dict[str, Any],
    *,
    sleeve_id: str,
    symbol_names: Mapping[str, str] | None = None,
    realized_pnl: float | None = None,
    realized_pnl_by_symbol: Mapping[str, float] | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current = payload.get("portfolio_state", {}).get("current", {}) or {}
    framework = payload.get("framework", {}) or {}
    engine_status = payload.get("engine_status", {}) or {}
    risk = framework.get("risk", {}) or {}
    execution = framework.get("execution", {}) or {}
    snapshot_quality = _snapshot_quality(payload)
    warnings = payload.get("warnings") or []

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
        f"데이터: {snapshot_quality.get('status', 'unknown')} "
        f"({snapshot_quality.get('collected_symbol_count', '?')}/{snapshot_quality.get('requested_symbol_count', '?')})",
        "",
        "요약",
        f"- 총 평가액: {_money(equity, currency)} {currency}",
        f"- 현금: {_money(cash, currency)} {currency}",
        f"- 주식 평가액: {_money(exposure, currency)} {currency} ({_pct(exposure_pct)})",
        f"- 주문 후보: {execution.get('order_count', 0)}건",
        f"- 활성 인사이트: {engine_status.get('framework', {}).get('active_insight_count', '-')}",
        "",
        "손익",
        f"- 미실현: {_signed_money(unrealized, currency)} {currency} ({_pct(_total_unrealized_pnl_pct(current))})",
        f"- 실현 추정: {_signed_money(realized_pnl, currency)} {currency}",
        f"- 합산 추정: {_signed_money(total_pnl, currency)} {currency} ({_pct(total_pnl_pct)})",
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
    for symbol in symbols:
        current_item = current_by_symbol.get(symbol, {})
        target_item = target_by_symbol.get(symbol, {})
        current_qty = int(current_item.get("quantity", 0) or 0)
        target_qty = int(target_item.get("target_quantity", current_qty) or 0)
        delta = target_qty - current_qty
        action = _delta_action(delta)
        market_value = current_item.get("market_value")
        pnl = float(current_item.get("unrealized_pnl") or 0.0)
        realized_symbol = float(realized_pnl_by_symbol.get(symbol) or 0.0)
        risk_note = _risk_note(target_item)
        lines.append(
            f"- {_format_symbol(symbol, symbol_names)} | {current_qty}주 -> {target_qty}주 "
            f"({action}) | 평가 {_money(market_value, currency)}"
        )
        lines.append(
            f"  현재 {_money(current_item.get('market_price'), currency)} / "
            f"평균 {_money(current_item.get('average_price'), currency)} / "
            f"미실현 {_signed_money(pnl, currency)} ({_pct(current_item.get('unrealized_pnl_pct'))})"
            f"{_realized_suffix(realized_symbol, currency)}{risk_note}"
        )

    order_lines = _order_lines(framework, symbol_names, currency)
    if order_lines:
        lines.extend(["", "주문 후보"])
        lines.extend(order_lines)

    attention = _attention_lines(snapshot_quality, execution, risk, warnings)
    if attention:
        lines.extend(["", "확인 필요"])
        lines.extend(f"- {line}" for line in attention)
    return "\n".join(lines).strip() + "\n"


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
            targets[symbol] = {
                "target_quantity": plan.get("target_quantity"),
                "risk_status": "pending_risk",
                "risk_reason": plan.get("reason", ""),
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


def _order_lines(framework: Mapping[str, Any], symbol_names: Mapping[str, str], currency: str) -> list[str]:
    lines: list[str] = []
    for order in framework.get("order_intents", []) or []:
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "")
        quantity = int(order.get("quantity") or 0)
        price = order.get("reference_price")
        lines.append(f"- {_format_symbol(symbol, symbol_names)} {side} {quantity}주 @ {_money(price, currency)}")
    return lines


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


def _delta_action(delta: int) -> str:
    if delta > 0:
        return f"매수 +{delta}"
    if delta < 0:
        return f"매도 {delta}"
    return "유지"


def _risk_note(target_item: Mapping[str, Any]) -> str:
    status = str(target_item.get("risk_status") or "")
    reason = str(target_item.get("risk_reason") or "")
    if status in {"", "hold", "approved"} and reason in {"", "no_delta"}:
        return ""
    return f" / risk {status}:{_friendly_risk_reason(reason, target_item.get('risk_metadata') or {})}"


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


def _realized_suffix(realized_symbol: float, currency: str) -> str:
    if abs(realized_symbol) < 0.5:
        return ""
    return f" / 실현 {_signed_money(realized_symbol, currency)}"


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

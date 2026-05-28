from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SleeveHoldingSnapshot:
    symbol: str
    quantity: float
    market_value: float
    average_price: float | None = None
    market_price: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    currency: str = "KRW"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "average_price": self.average_price,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class SleeveDailySnapshot:
    date: str
    label: str
    target_label: str
    sleeve_id: str
    currency: str
    as_of: datetime
    equity: float
    cash: float
    gross_exposure: float
    gross_exposure_pct: float | None
    cumulative_cash_flow: float
    holdings: tuple[SleeveHoldingSnapshot, ...]
    source_path: Path

    @property
    def held_symbol_count(self) -> int:
        return len([holding for holding in self.holdings if abs(holding.quantity) > 1e-12])

    def to_dict(self, *, include_holdings: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "date": self.date,
            "label": self.label,
            "target_label": self.target_label,
            "sleeve_id": self.sleeve_id,
            "currency": self.currency,
            "as_of": self.as_of.isoformat(),
            "equity": self.equity,
            "cash": self.cash,
            "gross_exposure": self.gross_exposure,
            "gross_exposure_pct": self.gross_exposure_pct,
            "held_symbol_count": self.held_symbol_count,
            "held_symbols": [holding.symbol for holding in self.holdings if abs(holding.quantity) > 1e-12],
            "cumulative_cash_flow": self.cumulative_cash_flow,
            "source_path": str(self.source_path),
        }
        if include_holdings:
            payload["holdings"] = [holding.to_dict() for holding in self.holdings]
        return payload


@dataclass(frozen=True, slots=True)
class SleeveDailyPerformanceRow:
    snapshot: SleeveDailySnapshot
    previous_equity: float | None = None
    net_cash_flow: float | None = None
    daily_pnl: float | None = None
    daily_return: float | None = None

    def to_dict(self, *, include_holdings: bool = False) -> dict[str, Any]:
        payload = self.snapshot.to_dict(include_holdings=include_holdings)
        payload.update(
            {
                "previous_equity": self.previous_equity,
                "net_cash_flow": self.net_cash_flow,
                "daily_pnl": self.daily_pnl,
                "daily_return": self.daily_return,
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class SleeveDailyPerformanceReport:
    snapshot_root: Path
    rows: tuple[SleeveDailyPerformanceRow, ...]
    warnings: tuple[str, ...] = ()

    def summaries(self) -> tuple[dict[str, Any], ...]:
        grouped: dict[tuple[str, str], list[SleeveDailyPerformanceRow]] = {}
        for row in self.rows:
            key = (row.snapshot.sleeve_id, row.snapshot.currency)
            grouped.setdefault(key, []).append(row)

        summaries: list[dict[str, Any]] = []
        for (sleeve_id, currency), rows in sorted(grouped.items()):
            ordered = sorted(rows, key=lambda item: (item.snapshot.date, item.snapshot.as_of))
            returns = [row.daily_return for row in ordered if row.daily_return is not None]
            period_return = None
            if returns:
                compound = 1.0
                for value in returns:
                    compound *= 1.0 + float(value)
                period_return = compound - 1.0
            summaries.append(
                {
                    "sleeve_id": sleeve_id,
                    "currency": currency,
                    "row_count": len(ordered),
                    "start_date": ordered[0].snapshot.date,
                    "end_date": ordered[-1].snapshot.date,
                    "start_equity": ordered[0].snapshot.equity,
                    "end_equity": ordered[-1].snapshot.equity,
                    "period_pnl": _sum_present(row.daily_pnl for row in ordered),
                    "period_return": period_return,
                    "latest_cash": ordered[-1].snapshot.cash,
                    "latest_gross_exposure": ordered[-1].snapshot.gross_exposure,
                    "latest_gross_exposure_pct": ordered[-1].snapshot.gross_exposure_pct,
                    "latest_held_symbols": [
                        holding.symbol
                        for holding in ordered[-1].snapshot.holdings
                        if abs(holding.quantity) > 1e-12
                    ],
                }
            )
        return tuple(summaries)

    def to_dict(self, *, include_rows: bool = True, include_holdings: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "snapshot_root": str(self.snapshot_root),
            "row_count": len(self.rows),
            "summary_count": len(self.summaries()),
            "summaries": list(self.summaries()),
            "warnings": list(self.warnings),
        }
        if include_rows:
            payload["rows"] = [row.to_dict(include_holdings=include_holdings) for row in self.rows]
        return payload


def build_sleeve_daily_performance_report(
    snapshot_root: Path | str,
    *,
    sleeve_ids: tuple[str, ...] = (),
    currency: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> SleeveDailyPerformanceReport:
    root = Path(snapshot_root)
    requested_sleeves = {sleeve_id for sleeve_id in sleeve_ids if sleeve_id}
    requested_currency = currency.upper() if currency else None
    warnings: list[str] = []

    latest_by_key: dict[tuple[str, str, str], SleeveDailySnapshot] = {}
    for path in sorted(root.rglob("*_runtime_*.json")) if root.exists() else []:
        if path.parent.name != "portfolio-report":
            continue
        try:
            snapshots = _snapshots_from_runtime_file(path, root)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"failed_to_read_snapshot:{path}:{exc}")
            continue
        for snapshot in snapshots:
            if requested_sleeves and snapshot.sleeve_id not in requested_sleeves:
                continue
            if requested_currency and snapshot.currency != requested_currency:
                continue
            if from_date and snapshot.date < from_date:
                continue
            if to_date and snapshot.date > to_date:
                continue
            key = (snapshot.date, snapshot.sleeve_id, snapshot.currency)
            previous = latest_by_key.get(key)
            if previous is None or (snapshot.as_of, str(snapshot.source_path)) > (previous.as_of, str(previous.source_path)):
                latest_by_key[key] = snapshot

    rows: list[SleeveDailyPerformanceRow] = []
    grouped: dict[tuple[str, str], list[SleeveDailySnapshot]] = {}
    for snapshot in latest_by_key.values():
        grouped.setdefault((snapshot.sleeve_id, snapshot.currency), []).append(snapshot)

    for group_key in sorted(grouped):
        previous: SleeveDailySnapshot | None = None
        for snapshot in sorted(grouped[group_key], key=lambda item: (item.date, item.as_of)):
            if previous is None or previous.equity <= 0:
                rows.append(SleeveDailyPerformanceRow(snapshot=snapshot))
            else:
                net_cash_flow = snapshot.cumulative_cash_flow - previous.cumulative_cash_flow
                daily_pnl = snapshot.equity - previous.equity - net_cash_flow
                rows.append(
                    SleeveDailyPerformanceRow(
                        snapshot=snapshot,
                        previous_equity=previous.equity,
                        net_cash_flow=net_cash_flow,
                        daily_pnl=daily_pnl,
                        daily_return=daily_pnl / previous.equity,
                    )
                )
            previous = snapshot

    return SleeveDailyPerformanceReport(
        snapshot_root=root,
        rows=tuple(sorted(rows, key=lambda row: (row.snapshot.sleeve_id, row.snapshot.currency, row.snapshot.date))),
        warnings=tuple(warnings),
    )


def _snapshots_from_runtime_file(path: Path, root: Path) -> tuple[SleeveDailySnapshot, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    current = _current_portfolio_payload(payload)
    sleeve_id = _sleeve_id_from_payload(payload, current)
    if not sleeve_id:
        raise ValueError("missing sleeve_id")
    as_of = _parse_datetime(_as_of_from_payload(payload, current))
    date, label, target_label = _path_metadata(path, root, as_of)
    holdings = _holding_snapshots(current)
    currencies = _portfolio_currencies(current, holdings)
    cumulative_flows_by_currency = {
        currency: _cumulative_cash_flow(path, root, sleeve_id=sleeve_id, currency=currency, as_of=as_of)
        for currency in currencies
    }

    rows: list[SleeveDailySnapshot] = []
    for currency in currencies:
        currency_holdings = tuple(holding for holding in holdings if holding.currency == currency)
        equity = _mapping_value(current.get("equity_by_currency"), currency)
        if equity is None and len(currencies) == 1:
            equity = _float_or_zero(current.get("equity"))
        if equity is None:
            equity = _mapping_value(current.get("cash_by_currency"), currency) or 0.0
            equity += sum(holding.market_value for holding in currency_holdings)
        cash = _mapping_value(current.get("cash_by_currency"), currency)
        if cash is None and len(currencies) == 1:
            cash = _float_or_zero(current.get("cash"))
        gross_exposure = sum(max(holding.market_value, 0.0) for holding in currency_holdings)
        if not currency_holdings and len(currencies) == 1:
            gross_exposure = _float_or_zero(current.get("gross_exposure"))
        rows.append(
            SleeveDailySnapshot(
                date=date,
                label=label,
                target_label=target_label,
                sleeve_id=sleeve_id,
                currency=currency,
                as_of=as_of,
                equity=float(equity or 0.0),
                cash=float(cash or 0.0),
                gross_exposure=float(gross_exposure),
                gross_exposure_pct=(float(gross_exposure) / float(equity)) if equity and equity > 0 else None,
                cumulative_cash_flow=cumulative_flows_by_currency[currency],
                holdings=currency_holdings,
                source_path=path,
            )
        )
    return tuple(rows)


def _current_portfolio_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    current = ((payload.get("portfolio_state") or {}).get("current") or {}) if isinstance(payload.get("portfolio_state"), Mapping) else {}
    if current:
        return current
    status = payload.get("engine_status") if isinstance(payload.get("engine_status"), Mapping) else {}
    portfolio = status.get("portfolio") if isinstance(status, Mapping) else {}
    if isinstance(portfolio, Mapping):
        return portfolio
    return {}


def _sleeve_id_from_payload(payload: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    direct = str(current.get("sleeve_id") or payload.get("sleeve_id") or "").strip()
    if direct:
        return direct
    framework = payload.get("framework") if isinstance(payload.get("framework"), Mapping) else {}
    batch = framework.get("portfolio_target_batch") if isinstance(framework.get("portfolio_target_batch"), Mapping) else {}
    batch_sleeve = str(batch.get("sleeve_id") or "").strip()
    if batch_sleeve:
        return batch_sleeve
    report_source = payload.get("report_source") if isinstance(payload.get("report_source"), Mapping) else {}
    return str(report_source.get("sleeve_id") or "").strip()


def _as_of_from_payload(payload: Mapping[str, Any], current: Mapping[str, Any]) -> str:
    report_source = payload.get("report_source") if isinstance(payload.get("report_source"), Mapping) else {}
    return str(
        current.get("as_of")
        or payload.get("generated_at")
        or payload.get("as_of")
        or report_source.get("generated_at")
        or ""
    )


def _holding_snapshots(current: Mapping[str, Any]) -> tuple[SleeveHoldingSnapshot, ...]:
    holdings: list[SleeveHoldingSnapshot] = []
    for item in current.get("holdings", []) or []:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        holdings.append(
            SleeveHoldingSnapshot(
                symbol=symbol,
                quantity=_float_or_zero(item.get("quantity")),
                average_price=_float_or_none(item.get("average_price")),
                market_price=_float_or_none(item.get("market_price")),
                market_value=_float_or_zero(item.get("market_value")),
                unrealized_pnl=_float_or_none(item.get("unrealized_pnl")),
                unrealized_pnl_pct=_float_or_none(item.get("unrealized_pnl_pct")),
                currency=_currency_for_symbol_key(symbol),
            )
        )
    return tuple(holdings)


def _portfolio_currencies(current: Mapping[str, Any], holdings: tuple[SleeveHoldingSnapshot, ...]) -> tuple[str, ...]:
    currencies: list[str] = []
    for key in ("equity_by_currency", "cash_by_currency"):
        raw = current.get(key)
        if isinstance(raw, Mapping):
            currencies.extend(str(currency).upper() for currency in raw if str(currency).strip())
    currencies.extend(holding.currency for holding in holdings)
    if not currencies:
        currencies.append(str(current.get("currency") or "KRW").upper())
    return tuple(dict.fromkeys(currencies))


def _cumulative_cash_flow(path: Path, root: Path, *, sleeve_id: str, currency: str, as_of: datetime) -> float:
    total = 0.0
    for store_path in _store_paths_for_snapshot(path, root):
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        transfers = payload.get("cash_transfers") if isinstance(payload, Mapping) else {}
        if not isinstance(transfers, Mapping):
            continue
        for raw_transfer in transfers.values():
            if not isinstance(raw_transfer, Mapping):
                continue
            if str(raw_transfer.get("currency") or "KRW").upper() != currency:
                continue
            occurred_at = _parse_optional_datetime(str(raw_transfer.get("occurred_at") or ""))
            if occurred_at is not None and _is_after(occurred_at, as_of):
                continue
            amount = _float_or_zero(raw_transfer.get("amount"))
            if str(raw_transfer.get("to_sleeve_id") or "") == sleeve_id:
                total += amount
            if str(raw_transfer.get("from_sleeve_id") or "") == sleeve_id:
                total -= amount
    return total


def _store_paths_for_snapshot(path: Path, root: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        relative_parts = ()
    if len(relative_parts) >= 2:
        stores_dir = root / relative_parts[0] / relative_parts[1] / "stores"
        if stores_dir.exists():
            paths.extend(sorted(stores_dir.glob("*.json")))
    for parent in path.parents:
        stores_dir = parent / "stores"
        if stores_dir.exists():
            paths.extend(sorted(stores_dir.glob("*.json")))
            break
    return tuple(dict.fromkeys(paths))


def _path_metadata(path: Path, root: Path, as_of: datetime) -> tuple[str, str, str]:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = ()
    date = parts[0] if len(parts) >= 1 else as_of.date().isoformat()
    label = parts[1] if len(parts) >= 2 else ""
    target_label = parts[2] if len(parts) >= 3 else ""
    return date, label, target_label


def _mapping_value(raw: Any, currency: str) -> float | None:
    if not isinstance(raw, Mapping):
        return None
    for key, value in raw.items():
        if str(key).upper() == currency:
            return _float_or_zero(value)
    return None


def _currency_for_symbol_key(symbol_key: str) -> str:
    market = symbol_key.split(":", 1)[0].upper() if ":" in symbol_key else ""
    if market in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"}:
        return "KRW"
    if market:
        return "USD"
    return "KRW"


def _parse_datetime(value: str) -> datetime:
    parsed = _parse_optional_datetime(value)
    if parsed is None:
        raise ValueError("missing datetime")
    return parsed


def _parse_optional_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_after(left: datetime, right: datetime) -> bool:
    if left.tzinfo is None or right.tzinfo is None:
        return left.replace(tzinfo=None) > right.replace(tzinfo=None)
    return left.astimezone(timezone.utc) > right.astimezone(timezone.utc)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or_zero(value: Any) -> float:
    parsed = _float_or_none(value)
    return parsed if parsed is not None else 0.0


def _sum_present(values: Any) -> float | None:
    present = [float(value) for value in values if value is not None]
    if not present:
        return None
    return sum(present)

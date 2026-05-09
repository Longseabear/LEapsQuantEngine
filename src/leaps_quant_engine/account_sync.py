from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from leaps_quant_engine.adapters.kis import BrokerEngineClient
from leaps_quant_engine.models import OrderSide, Symbol
from leaps_quant_engine.settings import load_kis_settings
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


@dataclass(slots=True)
class KISAccountClient:
    """Read-only KIS account operations through the local broker-engine."""

    broker: BrokerEngineClient

    @classmethod
    def from_env(cls) -> "KISAccountClient":
        return cls(BrokerEngineClient.from_settings(load_kis_settings()))

    def get_balance_summary(self, *, market: str = "domestic") -> dict[str, Any]:
        _require_domestic_account_market(market)
        return self.broker.call_operation("get_account_balance_summary", {})

    def get_holdings(self, *, market: str = "domestic") -> dict[str, Any]:
        _require_domestic_account_market(market)
        return self.broker.call_operation("get_account_holdings", {})

    def get_execution_history(
        self,
        *,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
    ) -> dict[str, Any]:
        _require_domestic_account_market(market)
        return self.broker.call_operation(
            "get_account_execution_history",
            {
                "start_date": start_date,
                "end_date": end_date,
                "side": side,
                "symbol": symbol,
            },
        )


@dataclass(frozen=True, slots=True)
class KISAccountSyncReport:
    market: str
    start_date: str
    end_date: str
    balance: dict[str, Any]
    holdings: dict[str, Any]
    execution_count: int
    imported_fill_count: int
    duplicate_fill_count: int
    skipped_fill_count: int
    unassigned_fill_count: int
    unallocated_fill_count: int = 0
    cash_reconciliation: dict[str, Any] | None = None
    rejected_executions: tuple[dict[str, Any], ...] = ()
    synced_sleeves: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "balance": self.balance,
            "holdings": self.holdings,
            "execution_count": self.execution_count,
            "imported_fill_count": self.imported_fill_count,
            "duplicate_fill_count": self.duplicate_fill_count,
            "skipped_fill_count": self.skipped_fill_count,
            "unassigned_fill_count": self.unassigned_fill_count,
            "unallocated_fill_count": self.unallocated_fill_count,
            "cash_reconciliation": self.cash_reconciliation,
            "rejected_executions": list(self.rejected_executions),
            "synced_sleeves": self.synced_sleeves,
        }


@dataclass(slots=True)
class KISVirtualAccountSync:
    """Imports broker executions into the virtual sleeve account store."""

    account_client: KISAccountClient

    @classmethod
    def from_env(cls) -> "KISVirtualAccountSync":
        return cls(KISAccountClient.from_env())

    def sync(
        self,
        store: VirtualSleeveAccountStore,
        *,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
        assign_unknown_to_sleeve_id: str | None = None,
        sync_cash: bool = False,
        residual_sleeve_id: str = "default sleeve",
        report_sleeve_ids: tuple[str, ...] = (),
    ) -> KISAccountSyncReport:
        balance = self.account_client.get_balance_summary(market=market)
        holdings = self.account_client.get_holdings(market=market)
        cash_reconciliation = None
        if sync_cash:
            cash_reconciliation = store.sync_account_cash(
                balance,
                residual_sleeve_id=residual_sleeve_id,
            ).to_dict()
            touched_sleeves = set(report_sleeve_ids) | {residual_sleeve_id}
        else:
            touched_sleeves: set[str] = set(report_sleeve_ids)
        history = self.account_client.get_execution_history(
            start_date=start_date,
            end_date=end_date,
            market=market,
            side=side,
            symbol=symbol,
        )
        executions = tuple(_extract_executions(history))
        imported = 0
        duplicate = 0
        skipped = 0
        unassigned = 0
        unallocated = 0
        rejected: list[dict[str, Any]] = []

        for row in executions:
            try:
                fill = execution_to_virtual_fill(
                    row,
                    market=market,
                    assign_unknown_to_sleeve_id=assign_unknown_to_sleeve_id,
                )
            except ValueError as exc:
                skipped += 1
                rejected.append({"reason": str(exc), "execution": row})
                continue
            if store.fill_exists(fill.fill_id):
                duplicate += 1
                continue
            owned = store.ownership_for_order(fill.order_id)
            if owned is None and not fill.sleeve_id:
                try:
                    if store.record_broker_fill(fill):
                        unallocated += 1
                    else:
                        duplicate += 1
                except ValueError as exc:
                    skipped += 1
                    rejected.append({"reason": str(exc), "execution": row})
                continue
            elif fill.sleeve_id:
                touched_sleeves.add(fill.sleeve_id)
            elif owned is not None:
                touched_sleeves.add(owned.sleeve_id)
            try:
                store.apply_fill(fill)
            except ValueError as exc:
                skipped += 1
                rejected.append({"reason": str(exc), "execution": row})
                continue
            imported += 1

        synced_sleeves = {
            sleeve_id: _portfolio_report(store.current_portfolio(sleeve_id))
            for sleeve_id in sorted(touched_sleeves)
        }
        return KISAccountSyncReport(
            market=market,
            start_date=start_date,
            end_date=end_date,
            balance=balance,
            holdings=holdings,
            execution_count=len(executions),
            imported_fill_count=imported,
            duplicate_fill_count=duplicate,
            skipped_fill_count=skipped,
            unassigned_fill_count=unassigned,
            unallocated_fill_count=unallocated,
            cash_reconciliation=cash_reconciliation,
            rejected_executions=tuple(rejected),
            synced_sleeves=synced_sleeves,
        )


def execution_to_virtual_fill(
    execution: dict[str, Any],
    *,
    market: str = "domestic",
    assign_unknown_to_sleeve_id: str | None = None,
) -> VirtualFillEvent:
    order_id = _required_text(execution, "order_id")
    quantity = _required_int(execution, "execution_quantity")
    price = _float_or_none(execution.get("execution_price"))
    amount = _float_or_none(execution.get("execution_amount"))
    if price is None and amount is not None and quantity > 0:
        price = amount / quantity
    if price is None or price <= 0:
        raise ValueError("execution_price is required.")
    side = _parse_side(execution.get("side"))
    symbol = Symbol(
        ticker=_required_text(execution, "symbol"),
        market=_symbol_market(market, execution),
    )
    filled_at = _parse_execution_datetime(execution)
    fill_id = f"kis:{market}:{order_id}:{filled_at.strftime('%Y%m%dT%H%M%S')}:{quantity}:{price:g}"
    return VirtualFillEvent(
        fill_id=fill_id,
        order_id=order_id,
        broker_order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        fill_price=price,
        filled_at=filled_at,
        sleeve_id=assign_unknown_to_sleeve_id,
    )


def _extract_executions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    executions = payload.get("executions", [])
    if not isinstance(executions, list):
        raise ValueError("get_account_execution_history result must contain an executions list.")
    return [dict(row) for row in executions if isinstance(row, dict)]


def _require_domestic_account_market(market: str) -> None:
    if market != "domestic":
        raise ValueError("KIS account sync currently supports domestic account operations only.")


def _portfolio_report(portfolio) -> dict[str, Any]:
    return {
        "cash": portfolio.cash,
        "holding_count": len(portfolio.holdings),
        "holdings": [
            {
                "symbol": holding.symbol.ticker,
                "market": holding.symbol.market,
                "quantity": holding.quantity,
                "average_price": holding.average_price,
            }
            for holding in portfolio.holdings.values()
        ],
    }


def _symbol_market(market: str, execution: dict[str, Any]) -> str:
    if market == "domestic":
        return "KRX"
    exchange = str(execution.get("exchange") or execution.get("market") or "").strip().upper()
    return exchange or market.upper()


def _parse_side(value: Any) -> OrderSide:
    text = str(value or "").strip().lower()
    if text in {"buy", "b", "02", "매수"}:
        return OrderSide.BUY
    if text in {"sell", "s", "01", "매도"}:
        return OrderSide.SELL
    raise ValueError("execution side must be buy or sell.")


def _parse_execution_datetime(execution: dict[str, Any]) -> datetime:
    timestamp = str(execution.get("execution_timestamp") or "").strip()
    if timestamp:
        return _parse_kis_datetime(timestamp)
    date = str(execution.get("execution_date") or "").strip()
    time = str(execution.get("execution_time") or "").strip()
    if date:
        return _parse_kis_datetime(f"{date}T{time or '000000'}")
    raise ValueError("execution timestamp is required.")


def _parse_kis_datetime(value: str) -> datetime:
    text = value.strip()
    if len(text) == 15 and text[8] == "T":
        return datetime.strptime(text, "%Y%m%dT%H%M%S")
    if len(text) == 14 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d%H%M%S")
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)


def _required_text(payload: dict[str, Any], key: str) -> str:
    text = str(payload.get(key) or "").strip()
    if not text:
        raise ValueError(f"{key} is required.")
    return text


def _required_int(payload: dict[str, Any], key: str) -> int:
    value = _float_or_none(payload.get(key))
    if value is None or value <= 0:
        raise ValueError(f"{key} must be positive.")
    return int(value)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    return float(text)

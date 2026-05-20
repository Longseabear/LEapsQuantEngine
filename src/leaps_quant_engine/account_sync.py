from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from leaps_quant_engine.adapters.kis_direct import KISDirectClient
from leaps_quant_engine.broker_routing import currency_for_market_scope
from leaps_quant_engine.models import OrderSide, Symbol
from leaps_quant_engine.settings import load_kis_settings_for_account
from leaps_quant_engine.transactions import TransactionCostSummary
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


@dataclass(slots=True)
class KISAccountClient:
    """Read-only KIS account operations through the configured KIS adapter boundary."""

    broker: Any

    @classmethod
    def from_env(cls, account_id: str | None = None, *, metadata: Mapping[str, Any] | None = None) -> "KISAccountClient":
        return cls(KISDirectClient.from_settings(load_kis_settings_for_account(account_id, metadata=metadata)))

    def get_balance_summary(self, *, market: str = "domestic") -> dict[str, Any]:
        return self.broker.call_operation("get_account_balance_summary", {"market": market})

    def get_holdings(self, *, market: str = "domestic") -> dict[str, Any]:
        return self.broker.call_operation("get_account_holdings", {"market": market})

    def get_execution_history(
        self,
        *,
        start_date: str,
        end_date: str,
        market: str = "domestic",
        side: str = "all",
        symbol: str = "",
    ) -> dict[str, Any]:
        return self.broker.call_operation(
            "get_account_execution_history",
            {
                "start_date": start_date,
                "end_date": end_date,
                "market": market,
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
    def from_env(cls, account_id: str | None = None, *, metadata: Mapping[str, Any] | None = None) -> "KISVirtualAccountSync":
        return cls(KISAccountClient.from_env(account_id, metadata=metadata))

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
                currency=currency_for_market_scope(market),
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
    fill_id = _execution_fill_id(
        execution,
        market=market,
        order_id=order_id,
        filled_at=filled_at,
        quantity=quantity,
        price=price,
    )
    costs, fee_components = _transaction_costs_from_execution(execution, market=market)
    metadata: dict[str, Any] = {}
    if fee_components:
        metadata["fee_components"] = fee_components
        metadata["transaction_costs"] = costs.to_dict()
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
        fee=costs.total_cost,
        metadata=metadata,
    )


_FEE_KEYS = (
    "fee",
    "fees",
    "fee_amount",
    "fee_amt",
    "execution_fee",
    "transaction_fee",
    "broker_fee",
    "chag",
    "tot_fee",
)
_COMMISSION_KEYS = (
    "commission",
    "commission_amount",
    "commission_amt",
    "broker_commission",
    "brokerage_commission",
    "commission_fee",
    "comm_fee",
    "comm_amt",
    "cmsn",
)
_TAX_KEYS = (
    "tax",
    "taxes",
    "tax_amount",
    "tax_amt",
    "transaction_tax",
    "securities_transaction_tax",
    "sell_tax",
    "stt",
    "txam",
)
_REGULATORY_FEE_KEYS = (
    "regulatory_fee",
    "regulatory_fee_amount",
    "exchange_fee",
    "exchange_fee_amount",
    "sec_fee",
    "taf_fee",
    "levy",
    "levy_amount",
)
_TOTAL_COST_KEYS = (
    "total_fee",
    "total_fee_amount",
    "total_cost",
    "total_cost_amount",
    "transaction_cost",
    "transaction_cost_amount",
)


def _transaction_costs_from_execution(
    execution: dict[str, Any],
    *,
    market: str,
) -> tuple[TransactionCostSummary, dict[str, float]]:
    fee = _sum_present_fields(execution, _FEE_KEYS)
    commission = _sum_present_fields(execution, _COMMISSION_KEYS)
    tax = _sum_present_fields(execution, _TAX_KEYS)
    regulatory_fee = _sum_present_fields(execution, _REGULATORY_FEE_KEYS)
    explicit_total = _first_present_float(execution, _TOTAL_COST_KEYS)
    if explicit_total is not None:
        component_total = commission + tax + regulatory_fee
        fee = max(explicit_total - component_total, 0.0)
    components = {
        key: value
        for key, value in {
            "fee": fee,
            "commission": commission,
            "tax": tax,
            "regulatory_fee": regulatory_fee,
        }.items()
        if abs(value) > 1e-12
    }
    return (
        TransactionCostSummary(
            fee=fee,
            commission=commission,
            tax=tax,
            regulatory_fee=regulatory_fee,
            currency=currency_for_market_scope(market),
            source="kis_execution",
        ),
        components,
    )


def _sum_present_fields(payload: dict[str, Any], keys: tuple[str, ...]) -> float:
    total = 0.0
    for key in keys:
        if key not in payload:
            continue
        value = _float_or_none(payload.get(key))
        if value is not None:
            total += value
    return total


def _first_present_float(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        value = _float_or_none(payload.get(key))
        if value is not None:
            return value
    return None


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
        "cash_by_currency": dict(portfolio.cash_by_currency),
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
    normalized_market = str(execution.get("market") or "").strip().upper()
    if normalized_market:
        return normalized_market
    exchange = str(execution.get("exchange") or "").strip().upper()
    if exchange in {"NAS", "NASD", "NASDAQ", "NYS", "NYSE", "AMS", "AMEX"}:
        return "US"
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


def _execution_fill_id(
    execution: dict[str, Any],
    *,
    market: str,
    order_id: str,
    filled_at: datetime,
    quantity: int,
    price: float,
) -> str:
    explicit_execution_id = _first_text(
        execution,
        (
            "execution_id",
            "execution_no",
            "execution_number",
            "fill_id",
            "fill_no",
            "fill_number",
            "trade_id",
            "trade_no",
            "contract_id",
            "contract_no",
            "ccno",
            "cntr_no",
            "odno_exec_no",
        ),
    )
    if explicit_execution_id:
        return f"kis:{market}:fill:{order_id}:{explicit_execution_id}"

    timestamp = filled_at.strftime("%Y%m%dT%H%M%S")
    granularity = str(execution.get("source_granularity") or "").strip().lower()
    if granularity == "order_execution_summary":
        return f"kis:{market}:order-summary:{order_id}:{timestamp}"

    return f"kis:{market}:{order_id}:{timestamp}:{quantity}:{price:g}"


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


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

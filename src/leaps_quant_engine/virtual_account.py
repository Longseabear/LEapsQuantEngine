from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import time
from types import MappingProxyType
from typing import Any, Mapping

from leaps_quant_engine.broker_routing import currency_for_market_scope, currency_for_symbol
from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
from leaps_quant_engine.orders import OrderEvent, OrderTicket
from leaps_quant_engine.portfolio import Holding, Portfolio, PortfolioProvider


UNKNOWN_SLEEVE_ID = "unassigned"
DEFAULT_ACCOUNT_ID = "default"
DEFAULT_CASH_SLEEVE_ID = "default sleeve"


@dataclass(frozen=True, slots=True)
class OrderOwnership:
    order_id: str
    sleeve_id: str
    symbol: Symbol
    side: OrderSide
    quantity: int
    reference_price: float
    tag: str = ""
    broker_order_id: str = ""
    created_at: datetime | None = None

    @classmethod
    def from_intent(
        cls,
        order: OrderIntent,
        *,
        order_id: str,
        broker_order_id: str = "",
        created_at: datetime | None = None,
    ) -> "OrderOwnership":
        return cls(
            order_id=order_id,
            sleeve_id=order.sleeve_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            reference_price=order.reference_price,
            tag=order.tag,
            broker_order_id=broker_order_id,
            created_at=created_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "sleeve_id": self.sleeve_id,
            "symbol": _symbol_to_dict(self.symbol),
            "side": self.side.value,
            "quantity": self.quantity,
            "reference_price": self.reference_price,
            "tag": self.tag,
            "broker_order_id": self.broker_order_id,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OrderOwnership":
        return cls(
            order_id=str(payload["order_id"]),
            sleeve_id=str(payload.get("sleeve_id") or UNKNOWN_SLEEVE_ID),
            symbol=_symbol_from_dict(payload["symbol"]),
            side=OrderSide(str(payload.get("side") or "buy")),
            quantity=int(payload.get("quantity") or 0),
            reference_price=float(payload.get("reference_price") or 0.0),
            tag=str(payload.get("tag") or ""),
            broker_order_id=str(payload.get("broker_order_id") or ""),
            created_at=_parse_optional_datetime(payload.get("created_at")),
        )


@dataclass(frozen=True, slots=True)
class VirtualFillEvent:
    fill_id: str
    order_id: str
    symbol: Symbol
    side: OrderSide
    quantity: int
    fill_price: float
    filled_at: datetime
    sleeve_id: str | None = None
    broker_order_id: str = ""
    fee: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def notional(self) -> float:
        return self.quantity * self.fill_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": _symbol_to_dict(self.symbol),
            "side": self.side.value,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "filled_at": self.filled_at.isoformat(),
            "sleeve_id": self.sleeve_id or "",
            "broker_order_id": self.broker_order_id,
            "fee": self.fee,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VirtualFillEvent":
        return cls(
            fill_id=str(payload["fill_id"]),
            order_id=str(payload["order_id"]),
            symbol=_symbol_from_dict(payload["symbol"]),
            side=OrderSide(str(payload.get("side") or "buy")),
            quantity=int(payload.get("quantity") or 0),
            fill_price=float(payload.get("fill_price") or 0.0),
            filled_at=datetime.fromisoformat(str(payload["filled_at"])),
            sleeve_id=str(payload.get("sleeve_id") or "") or None,
            broker_order_id=str(payload.get("broker_order_id") or ""),
            fee=float(payload.get("fee") or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class PortfolioMutationRecord:
    sleeve_id: str
    symbol: Symbol
    side: OrderSide
    quantity: int
    fill_price: float
    fee: float
    realized_pnl_estimate: float
    before_quantity: int
    after_quantity: int
    before_average_price: float
    after_average_price: float
    before_cash: float
    after_cash: float
    currency: str
    fill_id: str
    order_intent_id: str = ""
    ticket_id: str = ""
    event_id: str = ""
    broker_order_id: str = ""
    applied_at: datetime | None = None

    @property
    def notional(self) -> float:
        return self.quantity * self.fill_price

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol.key,
            "side": self.side.value,
            "quantity": self.quantity,
            "fill_price": self.fill_price,
            "notional": self.notional,
            "fee": self.fee,
            "realized_pnl_estimate": self.realized_pnl_estimate,
            "before_quantity": self.before_quantity,
            "after_quantity": self.after_quantity,
            "before_average_price": self.before_average_price,
            "after_average_price": self.after_average_price,
            "before_cash": self.before_cash,
            "after_cash": self.after_cash,
            "currency": self.currency,
            "fill_id": self.fill_id,
            "order_intent_id": self.order_intent_id,
            "ticket_id": self.ticket_id,
            "event_id": self.event_id,
            "broker_order_id": self.broker_order_id,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }


@dataclass(frozen=True, slots=True)
class FillApplicationReport:
    applied: bool
    sleeve_id: str
    fill_id: str
    reason: str = ""
    portfolio: Portfolio | None = None
    mutation: PortfolioMutationRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "sleeve_id": self.sleeve_id,
            "fill_id": self.fill_id,
            "reason": self.reason,
            "mutation": self.mutation.to_dict() if self.mutation is not None else None,
        }


@dataclass(frozen=True, slots=True)
class FillAllocation:
    fill_id: str
    sleeve_id: str
    quantity: int
    allocation_id: str = ""
    allocated_at: datetime | None = None
    reason: str = ""

    def resolved_allocation_id(self) -> str:
        if self.allocation_id:
            return self.allocation_id
        return f"{self.fill_id}:{self.sleeve_id}:{self.quantity}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation_id": self.resolved_allocation_id(),
            "fill_id": self.fill_id,
            "sleeve_id": self.sleeve_id,
            "quantity": self.quantity,
            "allocated_at": self.allocated_at.isoformat() if self.allocated_at else "",
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FillAllocation":
        return cls(
            allocation_id=str(payload.get("allocation_id") or ""),
            fill_id=str(payload["fill_id"]),
            sleeve_id=str(payload.get("sleeve_id") or UNKNOWN_SLEEVE_ID),
            quantity=int(payload.get("quantity") or 0),
            allocated_at=_parse_optional_datetime(payload.get("allocated_at")),
            reason=str(payload.get("reason") or ""),
        )


@dataclass(frozen=True, slots=True)
class IgnoredBrokerFill:
    fill_id: str
    ignored_at: datetime
    reason: str = ""
    ignored_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "ignored_at": self.ignored_at.isoformat(),
            "reason": self.reason,
            "ignored_by": self.ignored_by,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "IgnoredBrokerFill":
        return cls(
            fill_id=str(payload["fill_id"]),
            ignored_at=datetime.fromisoformat(str(payload["ignored_at"])),
            reason=str(payload.get("reason") or ""),
            ignored_by=str(payload.get("ignored_by") or ""),
        )


@dataclass(frozen=True, slots=True)
class AccountCashSnapshot:
    account_id: str
    cash_balance: float
    synced_at: datetime
    currency: str = "KRW"
    deposit_total_amount: float | None = None
    previous_settlement_amount: float | None = None
    next_day_settlement_amount: float | None = None
    total_evaluation_amount: float | None = None
    net_asset_amount: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "cash_balance": self.cash_balance,
            "synced_at": self.synced_at.isoformat(),
            "currency": _currency_code(self.currency),
            "deposit_total_amount": self.deposit_total_amount,
            "previous_settlement_amount": self.previous_settlement_amount,
            "next_day_settlement_amount": self.next_day_settlement_amount,
            "total_evaluation_amount": self.total_evaluation_amount,
            "net_asset_amount": self.net_asset_amount,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AccountCashSnapshot":
        return cls(
            account_id=str(payload.get("account_id") or DEFAULT_ACCOUNT_ID),
            cash_balance=float(payload.get("cash_balance") or 0.0),
            synced_at=datetime.fromisoformat(str(payload["synced_at"])),
            currency=_currency_code(str(payload.get("currency") or "KRW")),
            deposit_total_amount=_float_or_none(payload.get("deposit_total_amount")),
            previous_settlement_amount=_float_or_none(payload.get("previous_settlement_amount")),
            next_day_settlement_amount=_float_or_none(payload.get("next_day_settlement_amount")),
            total_evaluation_amount=_float_or_none(payload.get("total_evaluation_amount")),
            net_asset_amount=_float_or_none(payload.get("net_asset_amount")),
        )

    @classmethod
    def from_balance_payload(
        cls,
        payload: dict[str, Any],
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        currency: str = "KRW",
        synced_at: datetime | None = None,
    ) -> "AccountCashSnapshot":
        return cls(
            account_id=account_id,
            cash_balance=float(payload.get("cash_balance") or 0.0),
            synced_at=synced_at or datetime.now().astimezone(),
            currency=_currency_code(str(payload.get("currency") or currency)),
            deposit_total_amount=_float_or_none(payload.get("deposit_total_amount")),
            previous_settlement_amount=_float_or_none(payload.get("previous_settlement_amount")),
            next_day_settlement_amount=_float_or_none(payload.get("next_day_settlement_amount")),
            total_evaluation_amount=_float_or_none(payload.get("total_evaluation_amount")),
            net_asset_amount=_float_or_none(payload.get("net_asset_amount")),
        )


@dataclass(frozen=True, slots=True)
class CashTransfer:
    transfer_id: str
    from_sleeve_id: str
    to_sleeve_id: str
    amount: float
    occurred_at: datetime
    account_id: str = DEFAULT_ACCOUNT_ID
    currency: str = "KRW"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "from_sleeve_id": self.from_sleeve_id,
            "to_sleeve_id": self.to_sleeve_id,
            "amount": self.amount,
            "occurred_at": self.occurred_at.isoformat(),
            "account_id": self.account_id,
            "currency": _currency_code(self.currency),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PositionState:
    sleeve_id: str
    symbol: Symbol
    quantity: int
    average_entry_price: float
    entry_time: datetime
    high_watermark_price: float
    high_watermark_at: datetime
    last_price: float | None = None
    last_updated_at: datetime | None = None
    last_stop_price: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "symbol": _symbol_to_dict(self.symbol),
            "quantity": self.quantity,
            "average_entry_price": self.average_entry_price,
            "entry_time": self.entry_time.isoformat(),
            "high_watermark_price": self.high_watermark_price,
            "high_watermark_at": self.high_watermark_at.isoformat(),
            "last_price": self.last_price,
            "last_updated_at": self.last_updated_at.isoformat() if self.last_updated_at else "",
            "last_stop_price": self.last_stop_price,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PositionState":
        return cls(
            sleeve_id=str(payload.get("sleeve_id") or UNKNOWN_SLEEVE_ID),
            symbol=_symbol_from_dict(payload["symbol"]),
            quantity=int(payload.get("quantity") or 0),
            average_entry_price=float(payload.get("average_entry_price") or 0.0),
            entry_time=datetime.fromisoformat(str(payload["entry_time"])),
            high_watermark_price=float(payload.get("high_watermark_price") or 0.0),
            high_watermark_at=datetime.fromisoformat(str(payload["high_watermark_at"])),
            last_price=_float_or_none(payload.get("last_price")),
            last_updated_at=_parse_optional_datetime(payload.get("last_updated_at")),
            last_stop_price=_float_or_none(payload.get("last_stop_price")),
        )


@dataclass(frozen=True, slots=True)
class CashReconciliationReport:
    account_id: str
    currency: str
    broker_cash_balance: float
    virtual_cash_total: float
    residual_sleeve_id: str
    residual_cash: float
    sleeve_cash: dict[str, float]

    @property
    def difference(self) -> float:
        return self.virtual_cash_total - self.broker_cash_balance

    @property
    def status(self) -> str:
        if self.residual_cash < -1e-6:
            return "overallocated"
        return "matched" if abs(self.difference) < 1e-6 else "mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "currency": _currency_code(self.currency),
            "status": self.status,
            "broker_cash_balance": self.broker_cash_balance,
            "virtual_cash_total": self.virtual_cash_total,
            "difference": self.difference,
            "residual_sleeve_id": self.residual_sleeve_id,
            "residual_cash": self.residual_cash,
            "sleeve_cash": self.sleeve_cash,
        }


@dataclass(frozen=True, slots=True)
class FillAllocationStatus:
    fill: VirtualFillEvent
    allocated_quantity: int
    allocations: tuple[FillAllocation, ...] = ()
    ignored: IgnoredBrokerFill | None = None

    @property
    def remaining_quantity(self) -> int:
        if self.ignored is not None:
            return 0
        return self.fill.quantity - self.allocated_quantity

    @property
    def status(self) -> str:
        if self.ignored is not None:
            return "ignored"
        if self.allocated_quantity <= 0:
            return "unallocated"
        if self.remaining_quantity > 0:
            return "partially_allocated"
        return "fully_allocated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill.fill_id,
            "order_id": self.fill.order_id,
            "symbol": _symbol_to_dict(self.fill.symbol),
            "side": self.fill.side.value,
            "quantity": self.fill.quantity,
            "fill_price": self.fill.fill_price,
            "filled_at": self.fill.filled_at.isoformat(),
            "allocated_quantity": self.allocated_quantity,
            "remaining_quantity": self.remaining_quantity,
            "status": self.status,
            "allocations": [allocation.to_dict() for allocation in self.allocations],
            "ignored": self.ignored.to_dict() if self.ignored is not None else None,
        }


@dataclass(frozen=True, slots=True)
class PositionReconciliationRow:
    symbol: Symbol
    broker_quantity: int
    virtual_quantity: int
    broker_average_price: float | None = None
    broker_quantity_source: str | None = None
    broker_current_quantity: int | None = None
    broker_settled_quantity: int | None = None
    broker_orderable_quantity: int | None = None

    @property
    def difference(self) -> int:
        return self.virtual_quantity - self.broker_quantity

    @property
    def status(self) -> str:
        return "matched" if self.difference == 0 else "mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.ticker,
            "market": self.symbol.market,
            "broker_quantity": self.broker_quantity,
            "virtual_quantity": self.virtual_quantity,
            "difference": self.difference,
            "broker_average_price": self.broker_average_price,
            "broker_quantity_source": self.broker_quantity_source,
            "broker_current_quantity": self.broker_current_quantity,
            "broker_settled_quantity": self.broker_settled_quantity,
            "broker_orderable_quantity": self.broker_orderable_quantity,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class VirtualAccountReconciliationReport:
    rows: tuple[PositionReconciliationRow, ...]
    allocation_statuses: tuple[FillAllocationStatus, ...] = ()

    @property
    def mismatch_count(self) -> int:
        return sum(1 for row in self.rows if row.status != "matched")

    @property
    def unallocated_fill_count(self) -> int:
        return sum(
            1
            for status in self.allocation_statuses
            if status.remaining_quantity > 0 and status.status != "ignored"
        )

    @property
    def status(self) -> str:
        if self.mismatch_count or self.unallocated_fill_count:
            return "needs_reconciliation"
        return "matched"

    def to_dict(self, *, include_fills: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "mismatch_count": self.mismatch_count,
            "unallocated_fill_count": self.unallocated_fill_count,
            "rows": [row.to_dict() for row in self.rows],
            "unallocated_fills": [
                status.to_dict()
                for status in self.allocation_statuses
                if status.remaining_quantity > 0
            ]
            if include_fills
            else [],
            "ignored_fills": [
                status.to_dict()
                for status in self.allocation_statuses
                if status.status == "ignored"
            ]
            if include_fills
            else [],
        }


@dataclass(slots=True)
class VirtualSleeveAccountStore(PortfolioProvider):
    """File-backed virtual sleeve accounts for live/paper ownership state."""

    path: Path
    default_cash_by_sleeve: dict[str, float] | None = None
    default_cash_by_currency_by_sleeve: dict[str, dict[str, float]] | None = None
    default_currency: str = "KRW"

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        state = self._load_state()
        self._ensure_sleeve(state, sleeve_id)
        self._write_state(state)
        sleeve = state["sleeves"][sleeve_id]
        return _portfolio_from_dict(sleeve)

    def position_state(self, sleeve_id: str, symbol: Symbol) -> PositionState | None:
        state = self._load_state()
        payload = (
            state.get("position_states", {})
            .get(sleeve_id, {})
            .get(symbol.key)
        )
        return PositionState.from_dict(payload) if payload is not None else None

    def position_states(self, sleeve_id: str) -> tuple[PositionState, ...]:
        state = self._load_state()
        raw_states = state.get("position_states", {}).get(sleeve_id, {})
        return tuple(
            PositionState.from_dict(payload)
            for _, payload in sorted(raw_states.items())
        )

    def update_position_mark(
        self,
        *,
        sleeve_id: str,
        symbol: Symbol,
        price: float,
        marked_at: datetime | None = None,
        stop_price: float | None = None,
    ) -> PositionState | None:
        if price <= 0:
            raise ValueError("position mark price must be positive.")
        state = self._load_state()
        payload = state.get("position_states", {}).get(sleeve_id, {}).get(symbol.key)
        if payload is None:
            return None
        position = PositionState.from_dict(payload)
        as_of = marked_at or datetime.now().astimezone()
        high_price = position.high_watermark_price
        high_at = position.high_watermark_at
        if price > high_price:
            high_price = price
            high_at = as_of
        updated = PositionState(
            sleeve_id=position.sleeve_id,
            symbol=position.symbol,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            entry_time=position.entry_time,
            high_watermark_price=high_price,
            high_watermark_at=high_at,
            last_price=price,
            last_updated_at=as_of,
            last_stop_price=stop_price if stop_price is not None else position.last_stop_price,
        )
        state.setdefault("position_states", {}).setdefault(sleeve_id, {})[symbol.key] = updated.to_dict()
        self._write_state(state)
        return updated

    def initialize_sleeve(
        self,
        sleeve_id: str,
        *,
        cash: float = 0.0,
        currency: str | None = None,
        overwrite: bool = False,
    ) -> Portfolio:
        state = self._load_state()
        if overwrite or sleeve_id not in state["sleeves"]:
            code = _currency_code(currency or self.default_currency)
            state["sleeves"][sleeve_id] = {
                "cash": float(cash),
                "cash_by_currency": {code: float(cash)} if cash else {},
                "holdings": {},
            }
            self._write_state(state)
        return self.current_portfolio(sleeve_id)

    def transfer_cash(
        self,
        *,
        from_sleeve_id: str,
        to_sleeve_id: str,
        amount: float,
        reason: str = "",
        account_id: str = DEFAULT_ACCOUNT_ID,
        currency: str | None = None,
        transfer_id: str = "",
        occurred_at: datetime | None = None,
    ) -> CashTransfer:
        if amount <= 0:
            raise ValueError("cash transfer amount must be positive.")
        code = _currency_code(currency or self.default_currency)
        state = self._load_state()
        self._ensure_sleeve(state, from_sleeve_id)
        self._ensure_sleeve(state, to_sleeve_id)
        source = state["sleeves"][from_sleeve_id]
        if _sleeve_cash_for_currency(source, code) < amount:
            raise ValueError("cash transfer exceeds source sleeve cash.")
        _adjust_sleeve_cash(state["sleeves"][from_sleeve_id], code, -amount)
        _adjust_sleeve_cash(state["sleeves"][to_sleeve_id], code, amount)
        event = CashTransfer(
            transfer_id=transfer_id or f"cash:{account_id}:{code}:{from_sleeve_id}:{to_sleeve_id}:{len(state['cash_transfers']) + 1}",
            from_sleeve_id=from_sleeve_id,
            to_sleeve_id=to_sleeve_id,
            amount=amount,
            occurred_at=occurred_at or datetime.now().astimezone(),
            account_id=account_id,
            currency=code,
            reason=reason,
        )
        state["cash_transfers"][event.transfer_id] = event.to_dict()
        self._write_state(state)
        return event

    def sync_account_cash(
        self,
        balance_payload: dict[str, Any],
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        currency: str | None = None,
        residual_sleeve_id: str = DEFAULT_CASH_SLEEVE_ID,
        synced_at: datetime | None = None,
    ) -> CashReconciliationReport:
        code = _currency_code(currency or self.default_currency)
        snapshot = AccountCashSnapshot.from_balance_payload(
            balance_payload,
            account_id=account_id,
            currency=code,
            synced_at=synced_at,
        )
        code = _currency_code(snapshot.currency)
        state = self._load_state()
        self._ensure_sleeve(state, residual_sleeve_id)
        snapshot_key = _account_cash_snapshot_key(account_id, code)
        state["account_cash_snapshots"][snapshot_key] = snapshot.to_dict()
        if code == "KRW":
            state["account_cash_snapshots"][account_id] = snapshot.to_dict()
        non_residual_cash = sum(
            _sleeve_cash_for_currency(raw, code)
            for sleeve_id, raw in state["sleeves"].items()
            if sleeve_id != residual_sleeve_id
        )
        residual_cash = snapshot.cash_balance - non_residual_cash
        _set_sleeve_cash_for_currency(
            state["sleeves"][residual_sleeve_id],
            code,
            max(residual_cash, 0.0),
        )
        self._write_state(state)
        return self.cash_reconciliation_report(
            account_id=account_id,
            currency=code,
            residual_sleeve_id=residual_sleeve_id,
        )

    def cash_reconciliation_report(
        self,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        currency: str | None = None,
        residual_sleeve_id: str = DEFAULT_CASH_SLEEVE_ID,
    ) -> CashReconciliationReport:
        code = _currency_code(currency or self.default_currency)
        state = self._load_state()
        snapshot_payload = state["account_cash_snapshots"].get(_account_cash_snapshot_key(account_id, code))
        if snapshot_payload is None and code == "KRW":
            snapshot_payload = state["account_cash_snapshots"].get(account_id)
        broker_cash_balance = float(snapshot_payload.get("cash_balance") or 0.0) if snapshot_payload else 0.0
        sleeve_cash = {
            sleeve_id: _sleeve_cash_for_currency(raw, code)
            for sleeve_id, raw in state["sleeves"].items()
        }
        return CashReconciliationReport(
            account_id=account_id,
            currency=code,
            broker_cash_balance=broker_cash_balance,
            virtual_cash_total=sum(sleeve_cash.values()),
            residual_sleeve_id=residual_sleeve_id,
            residual_cash=sleeve_cash.get(residual_sleeve_id, 0.0),
            sleeve_cash=sleeve_cash,
        )

    def register_order_intent(
        self,
        order: OrderIntent,
        *,
        order_id: str,
        broker_order_id: str = "",
        created_at: datetime | None = None,
    ) -> OrderOwnership:
        if not order_id.strip():
            raise ValueError("order_id is required.")
        state = self._load_state()
        self._ensure_sleeve(state, order.sleeve_id)
        record = OrderOwnership.from_intent(
            order,
            order_id=order_id,
            broker_order_id=broker_order_id,
            created_at=created_at,
        )
        state["order_ownership"][record.order_id] = record.to_dict()
        for alias in _broker_order_aliases(broker_order_id):
            state["broker_order_index"][alias] = record.order_id
        self._write_state(state)
        return record

    def register_order_ticket(
        self,
        ticket: OrderTicket,
        *,
        broker_order_id: str = "",
    ) -> OrderOwnership:
        state = self._load_state()
        self._ensure_sleeve(state, ticket.sleeve_id)
        record = OrderOwnership(
            order_id=ticket.order_intent_id,
            sleeve_id=ticket.sleeve_id,
            symbol=ticket.symbol,
            side=ticket.side,
            quantity=ticket.quantity,
            reference_price=ticket.reference_price,
            tag=ticket.tag,
            broker_order_id=broker_order_id or ticket.broker_order_id or "",
            created_at=ticket.created_at,
        )
        state["order_ownership"][record.order_id] = record.to_dict()
        for alias in _broker_order_aliases(record.broker_order_id):
            state["broker_order_index"][alias] = record.order_id
        self._write_state(state)
        return record

    def bind_broker_order_id(self, order_id: str, broker_order_id: str) -> OrderOwnership:
        if not order_id.strip():
            raise ValueError("order_id is required.")
        if not broker_order_id.strip():
            raise ValueError("broker_order_id is required.")
        state = self._load_state()
        payload = state["order_ownership"].get(order_id)
        if payload is None:
            raise ValueError(f"Unknown order ownership '{order_id}'.")
        record = OrderOwnership.from_dict(payload)
        resolved_broker_order_id = (
            record.broker_order_id
            if record.broker_order_id and broker_order_id in _broker_order_aliases(record.broker_order_id)
            else broker_order_id
        )
        updated = OrderOwnership(
            order_id=record.order_id,
            sleeve_id=record.sleeve_id,
            symbol=record.symbol,
            side=record.side,
            quantity=record.quantity,
            reference_price=record.reference_price,
            tag=record.tag,
            broker_order_id=resolved_broker_order_id,
            created_at=record.created_at,
        )
        state["order_ownership"][updated.order_id] = updated.to_dict()
        for alias in _broker_order_aliases(resolved_broker_order_id):
            state["broker_order_index"][alias] = updated.order_id
        self._write_state(state)
        return updated

    def apply_order_event(self, event: OrderEvent) -> Portfolio:
        report = self.apply_order_event_with_report(event)
        if report.portfolio is not None:
            return report.portfolio
        return self.current_portfolio(event.sleeve_id)

    def apply_order_event_with_report(self, event: OrderEvent) -> FillApplicationReport:
        if event.broker_order_id:
            try:
                self.bind_broker_order_id(event.order_intent_id, event.broker_order_id)
            except ValueError:
                pass
        if not event.is_fill or event.quantity <= 0 or event.fill_price is None:
            return FillApplicationReport(
                applied=False,
                sleeve_id=event.sleeve_id,
                fill_id="",
                reason="order_event_is_not_a_fill",
                portfolio=self.current_portfolio(event.sleeve_id),
            )
        fee = _float_or_none(event.metadata.get("fee")) or 0.0
        return self.apply_fill_with_report(
            VirtualFillEvent(
                fill_id=f"order-event:{event.event_id}",
                order_id=event.order_intent_id,
                broker_order_id=event.broker_order_id or "",
                symbol=event.symbol,
                side=event.side,
                quantity=event.quantity,
                fill_price=event.fill_price,
                filled_at=event.occurred_at,
                sleeve_id=event.sleeve_id,
                fee=fee,
            ),
            order_intent_id=event.order_intent_id,
            ticket_id=event.ticket_id,
            event_id=event.event_id,
        )

    def apply_fill(self, fill: VirtualFillEvent) -> Portfolio:
        report = self.apply_fill_with_report(fill)
        if report.portfolio is None:
            return self.current_portfolio(report.sleeve_id)
        return report.portfolio

    def apply_fill_with_report(
        self,
        fill: VirtualFillEvent,
        *,
        order_intent_id: str = "",
        ticket_id: str = "",
        event_id: str = "",
    ) -> FillApplicationReport:
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive.")
        state = self._load_state()
        if fill.fill_id in state["fills"]:
            sleeve_id = state["fills"][fill.fill_id].get("sleeve_id") or UNKNOWN_SLEEVE_ID
            return FillApplicationReport(
                applied=False,
                sleeve_id=sleeve_id,
                fill_id=fill.fill_id,
                reason="duplicate_fill_id",
                portfolio=_portfolio_from_dict(state["sleeves"][sleeve_id]),
            )

        sleeve_id, mutation = self._apply_fill_to_state_with_report(
            state,
            fill,
            order_intent_id=order_intent_id,
            ticket_id=ticket_id,
            event_id=event_id,
        )
        self._write_state(state)
        return FillApplicationReport(
            applied=True,
            sleeve_id=sleeve_id,
            fill_id=fill.fill_id,
            reason="applied",
            portfolio=_portfolio_from_dict(state["sleeves"][sleeve_id]),
            mutation=mutation,
        )

    def apply_fill_allocations(
        self,
        fill: VirtualFillEvent,
        allocations: tuple[FillAllocation, ...],
    ) -> dict[str, Portfolio]:
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive.")
        if not allocations:
            raise ValueError("at least one fill allocation is required.")
        state = self._load_state()
        if fill.fill_id in state["fills"]:
            raise ValueError("fill was already applied directly and cannot be allocated.")
        if fill.fill_id in state["ignored_broker_fills"]:
            raise ValueError("ignored broker fill cannot be allocated.")
        existing_allocated_quantity = sum(
            int(raw.get("quantity") or 0)
            for raw in state["fill_allocations"].values()
            if raw.get("fill_id") == fill.fill_id
        )
        new_allocated_quantity = sum(
            allocation.quantity
            for allocation in allocations
            if allocation.resolved_allocation_id() not in state["fill_allocations"]
        )
        if existing_allocated_quantity + new_allocated_quantity > fill.quantity:
            raise ValueError("fill allocations exceed the fill quantity.")
        applied: dict[str, Portfolio] = {}
        for allocation in allocations:
            if allocation.fill_id != fill.fill_id:
                raise ValueError("allocation fill_id must match fill.fill_id.")
            if allocation.quantity <= 0:
                raise ValueError("allocation quantity must be positive.")
            allocation_id = allocation.resolved_allocation_id()
            if allocation_id in state["fill_allocations"]:
                portfolio = self.current_portfolio(allocation.sleeve_id)
                applied[allocation.sleeve_id] = portfolio
                continue
            allocation_fill = VirtualFillEvent(
                fill_id=allocation_id,
                order_id=fill.order_id,
                symbol=fill.symbol,
                side=fill.side,
                quantity=allocation.quantity,
                fill_price=fill.fill_price,
                filled_at=fill.filled_at,
                sleeve_id=allocation.sleeve_id,
                broker_order_id=fill.broker_order_id,
                fee=fill.fee * (allocation.quantity / fill.quantity),
                metadata=_scale_fill_metadata(fill.metadata, allocation.quantity / fill.quantity),
            )
            self._apply_fill_to_state(state, allocation_fill, record_unknown_ownership=False)
            state["fill_allocations"][allocation_id] = allocation.to_dict()
            applied[allocation.sleeve_id] = _portfolio_from_dict(state["sleeves"][allocation.sleeve_id])
        state["broker_fills"][fill.fill_id] = fill.to_dict()
        self._write_state(state)
        return applied

    def record_broker_fill(self, fill: VirtualFillEvent) -> bool:
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive.")
        state = self._load_state()
        if fill.fill_id in state["fills"]:
            raise ValueError("fill was already applied to a sleeve portfolio.")
        if fill.fill_id in state["broker_fills"]:
            return False
        state["broker_fills"][fill.fill_id] = fill.to_dict()
        self._write_state(state)
        return True

    def ignore_broker_fill(
        self,
        fill_id: str,
        *,
        reason: str = "",
        ignored_by: str = "",
        ignored_at: datetime | None = None,
    ) -> IgnoredBrokerFill:
        state = self._load_state()
        if fill_id not in state["broker_fills"]:
            raise ValueError(f"broker fill not found: {fill_id}")
        if fill_id in state["fills"]:
            raise ValueError("fill was already applied to a sleeve portfolio.")
        allocated_quantity = sum(
            int(raw.get("quantity") or 0)
            for raw in state["fill_allocations"].values()
            if raw.get("fill_id") == fill_id
        )
        if allocated_quantity:
            raise ValueError("partially or fully allocated broker fill cannot be ignored.")
        record = IgnoredBrokerFill(
            fill_id=fill_id,
            ignored_at=ignored_at or datetime.now().astimezone(),
            reason=reason,
            ignored_by=ignored_by,
        )
        state["ignored_broker_fills"][fill_id] = record.to_dict()
        self._write_state(state)
        return record

    def broker_fill(self, fill_id: str) -> VirtualFillEvent | None:
        state = self._load_state()
        payload = state["broker_fills"].get(fill_id)
        return VirtualFillEvent.from_dict(payload) if payload is not None else None

    def fill_allocation_statuses(
        self,
        *,
        symbol: Symbol | None = None,
        side: OrderSide | None = None,
    ) -> tuple[FillAllocationStatus, ...]:
        state = self._load_state()
        allocations_by_fill: dict[str, list[FillAllocation]] = {}
        for raw in state["fill_allocations"].values():
            allocation = FillAllocation.from_dict(raw)
            allocations_by_fill.setdefault(allocation.fill_id, []).append(allocation)
        ignored_by_fill = {
            fill_id: IgnoredBrokerFill.from_dict(raw)
            for fill_id, raw in state["ignored_broker_fills"].items()
        }
        statuses: list[FillAllocationStatus] = []
        for raw in state["broker_fills"].values():
            fill = VirtualFillEvent.from_dict(raw)
            if symbol is not None and fill.symbol != symbol:
                continue
            if side is not None and fill.side is not side:
                continue
            allocations = tuple(sorted(
                allocations_by_fill.get(fill.fill_id, []),
                key=lambda allocation: allocation.resolved_allocation_id(),
            ))
            allocated_quantity = sum(allocation.quantity for allocation in allocations)
            statuses.append(
                FillAllocationStatus(
                    fill=fill,
                    allocated_quantity=allocated_quantity,
                    allocations=allocations,
                    ignored=ignored_by_fill.get(fill.fill_id),
                )
            )
        return tuple(sorted(statuses, key=lambda status: (status.fill.filled_at, status.fill.fill_id)))

    def reconciliation_report(
        self,
        broker_holdings: dict[str, Any] | list[dict[str, Any]],
        *,
        include_fills: bool = True,
    ) -> VirtualAccountReconciliationReport:
        state = self._load_state()
        broker_positions = _broker_positions_from_holdings(broker_holdings)
        virtual_positions = _virtual_positions_from_state(state)
        rows = []
        for symbol_key in sorted(set(broker_positions) | set(virtual_positions)):
            symbol = _symbol_from_dict(broker_positions.get(symbol_key, {}).get("symbol") or _symbol_dict_from_key(symbol_key))
            rows.append(
                PositionReconciliationRow(
                    symbol=symbol,
                    broker_quantity=int(broker_positions.get(symbol_key, {}).get("quantity") or 0),
                    virtual_quantity=int(virtual_positions.get(symbol_key, 0)),
                    broker_average_price=broker_positions.get(symbol_key, {}).get("average_price"),
                    broker_quantity_source=broker_positions.get(symbol_key, {}).get("quantity_source"),
                    broker_current_quantity=broker_positions.get(symbol_key, {}).get("current_quantity"),
                    broker_settled_quantity=broker_positions.get(symbol_key, {}).get("settled_quantity"),
                    broker_orderable_quantity=broker_positions.get(symbol_key, {}).get("orderable_quantity"),
                )
            )
        statuses = self.fill_allocation_statuses() if include_fills else ()
        return VirtualAccountReconciliationReport(rows=tuple(rows), allocation_statuses=statuses)

    def ownership_for_order(self, order_id: str) -> OrderOwnership | None:
        state = self._load_state()
        resolved_order_id = state["broker_order_index"].get(order_id, order_id)
        payload = state["order_ownership"].get(resolved_order_id)
        return OrderOwnership.from_dict(payload) if payload is not None else None

    def fill_exists(self, fill_id: str) -> bool:
        state = self._load_state()
        return fill_id in state["fills"]

    def _resolve_ownership(self, state: dict[str, Any], fill: VirtualFillEvent) -> OrderOwnership | None:
        order_id = fill.order_id
        if order_id not in state["order_ownership"] and fill.broker_order_id:
            order_id = state["broker_order_index"].get(fill.broker_order_id, order_id)
        payload = state["order_ownership"].get(order_id)
        return OrderOwnership.from_dict(payload) if payload is not None else None

    def _ensure_sleeve(self, state: dict[str, Any], sleeve_id: str) -> None:
        if sleeve_id in state["sleeves"]:
            _normalize_sleeve_cash(state["sleeves"][sleeve_id], self.default_currency)
            self._ensure_configured_cash_buckets(state, sleeve_id)
            return
        configured_cash = self._configured_cash_by_currency(sleeve_id)
        default_cash = (self.default_cash_by_sleeve or {}).get(sleeve_id, 0.0)
        code = _currency_code(self.default_currency)
        state["sleeves"][sleeve_id] = {
            "cash": sum(configured_cash.values()) if configured_cash else float(default_cash),
            "cash_by_currency": configured_cash or ({code: float(default_cash)} if default_cash else {}),
            "holdings": {},
        }

    def _ensure_configured_cash_buckets(self, state: dict[str, Any], sleeve_id: str) -> None:
        configured_cash = self._configured_cash_by_currency(sleeve_id)
        if not configured_cash:
            return
        sleeve = state["sleeves"][sleeve_id]
        if sleeve.get("holdings") or state.get("fills") or state.get("cash_transfers"):
            return
        cash_by_currency = _cash_by_currency_from_payload(sleeve, self.default_currency)
        changed = False
        for currency, amount in configured_cash.items():
            if currency not in cash_by_currency and amount > 0:
                cash_by_currency[currency] = amount
                changed = True
        if changed:
            sleeve["cash_by_currency"] = cash_by_currency
            sleeve["cash"] = sum(cash_by_currency.values())

    def _configured_cash_by_currency(self, sleeve_id: str) -> dict[str, float]:
        configured = (self.default_cash_by_currency_by_sleeve or {}).get(sleeve_id, {})
        return {
            _currency_code(currency): float(amount)
            for currency, amount in configured.items()
            if abs(float(amount)) > 1e-12
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": 1,
                "sleeves": {},
                "order_ownership": {},
                "broker_order_index": {},
                "fills": {},
                "broker_fills": {},
                "fill_allocations": {},
                "ignored_broker_fills": {},
                "portfolio_mutations": {},
                "account_cash_snapshots": {},
                "cash_transfers": {},
                "position_states": {},
            }
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Virtual sleeve account state must be an object: {self.path}")
        payload.setdefault("version", 1)
        payload.setdefault("sleeves", {})
        payload.setdefault("order_ownership", {})
        payload.setdefault("broker_order_index", {})
        payload.setdefault("fills", {})
        payload.setdefault("broker_fills", {})
        payload.setdefault("fill_allocations", {})
        payload.setdefault("ignored_broker_fills", {})
        payload.setdefault("portfolio_mutations", {})
        payload.setdefault("account_cash_snapshots", {})
        payload.setdefault("cash_transfers", {})
        payload.setdefault("position_states", {})
        for raw in dict(payload.get("sleeves") or {}).values():
            if isinstance(raw, dict):
                _normalize_sleeve_cash(raw, self.default_currency)
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp_path, self.path)
                return
            except PermissionError:
                if attempt >= 5:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def _apply_fill_to_state(
        self,
        state: dict[str, Any],
        fill: VirtualFillEvent,
        *,
        record_unknown_ownership: bool = True,
    ) -> str:
        sleeve_id, _mutation = self._apply_fill_to_state_with_report(
            state,
            fill,
            record_unknown_ownership=record_unknown_ownership,
        )
        return sleeve_id

    def _apply_fill_to_state_with_report(
        self,
        state: dict[str, Any],
        fill: VirtualFillEvent,
        *,
        record_unknown_ownership: bool = True,
        order_intent_id: str = "",
        ticket_id: str = "",
        event_id: str = "",
    ) -> tuple[str, PortfolioMutationRecord]:
        ownership = self._resolve_ownership(state, fill)
        sleeve_id = fill.sleeve_id or ownership.sleeve_id if ownership else fill.sleeve_id or UNKNOWN_SLEEVE_ID
        self._ensure_sleeve(state, sleeve_id)
        portfolio = _portfolio_from_dict(state["sleeves"][sleeve_id])
        previous_holding = portfolio.holdings.get(fill.symbol.key)
        previous_quantity = previous_holding.quantity if previous_holding is not None else 0
        previous_average_price = previous_holding.average_price if previous_holding is not None else 0.0
        currency = currency_for_symbol(fill.symbol)
        previous_cash = portfolio.cash_for_currency(currency)
        _apply_fill_to_portfolio(portfolio, fill)
        updated_holding = portfolio.holdings.get(fill.symbol.key)
        updated_quantity = updated_holding.quantity if updated_holding is not None else 0
        updated_average_price = updated_holding.average_price if updated_holding is not None else 0.0
        updated_cash = portfolio.cash_for_currency(currency)
        state["sleeves"][sleeve_id] = _portfolio_to_dict(portfolio)
        _apply_fill_to_position_state(
            state,
            sleeve_id=sleeve_id,
            fill=fill,
            previous_quantity=previous_quantity,
            portfolio=portfolio,
        )
        fill_payload = fill.to_dict()
        fill_payload["sleeve_id"] = sleeve_id
        state["fills"][fill.fill_id] = fill_payload
        if ownership is None and record_unknown_ownership:
            state["order_ownership"][fill.order_id] = OrderOwnership(
                order_id=fill.order_id,
                sleeve_id=sleeve_id,
                symbol=fill.symbol,
                side=fill.side,
                quantity=fill.quantity,
                reference_price=fill.fill_price,
                broker_order_id=fill.broker_order_id,
                created_at=fill.filled_at,
            ).to_dict()
        realized_pnl = 0.0
        if fill.side is OrderSide.SELL:
            realized_quantity = min(fill.quantity, previous_quantity)
            realized_pnl = (fill.fill_price - previous_average_price) * realized_quantity - fill.fee
        mutation = PortfolioMutationRecord(
            sleeve_id=sleeve_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=fill.quantity,
            fill_price=fill.fill_price,
            fee=fill.fee,
            realized_pnl_estimate=realized_pnl,
            before_quantity=previous_quantity,
            after_quantity=updated_quantity,
            before_average_price=previous_average_price,
            after_average_price=updated_average_price,
            before_cash=previous_cash,
            after_cash=updated_cash,
            currency=currency,
            fill_id=fill.fill_id,
            order_intent_id=order_intent_id or fill.order_id,
            ticket_id=ticket_id,
            event_id=event_id,
            broker_order_id=fill.broker_order_id,
            applied_at=fill.filled_at,
        )
        state.setdefault("portfolio_mutations", {})[fill.fill_id] = mutation.to_dict()
        return sleeve_id, mutation


def _apply_fill_to_portfolio(portfolio: Portfolio, fill: VirtualFillEvent) -> None:
    currency = currency_for_symbol(fill.symbol)
    holding = portfolio.holdings.setdefault(fill.symbol.key, Holding(fill.symbol))
    if fill.side is OrderSide.BUY:
        previous_cost = holding.quantity * holding.average_price
        new_quantity = holding.quantity + fill.quantity
        holding.average_price = (previous_cost + fill.notional) / new_quantity
        holding.quantity = new_quantity
        _adjust_portfolio_cash(portfolio, currency, -(fill.notional + fill.fee))
        return

    new_quantity = holding.quantity - fill.quantity
    if new_quantity < 0:
        raise ValueError("sell fill exceeds sleeve holding quantity.")
    _adjust_portfolio_cash(portfolio, currency, max(fill.notional - fill.fee, 0.0))
    if new_quantity <= 0:
        portfolio.holdings.pop(fill.symbol.key, None)
        return
    holding.quantity = new_quantity


def _scale_fill_metadata(metadata: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    payload = dict(metadata)
    payload["allocation_ratio"] = ratio
    fee_components = payload.get("fee_components")
    if isinstance(fee_components, dict):
        payload["fee_components"] = _scale_numeric_mapping(fee_components, ratio)
    transaction_costs = payload.get("transaction_costs")
    if isinstance(transaction_costs, dict):
        scaled = dict(transaction_costs)
        for key in ("fee", "commission", "tax", "regulatory_fee", "slippage_cost", "total_cost"):
            if key in scaled:
                scaled[key] = _safe_scaled_float(scaled[key], ratio)
        payload["transaction_costs"] = scaled
    return payload


def _scale_numeric_mapping(payload: Mapping[str, Any], ratio: float) -> dict[str, Any]:
    return {key: _safe_scaled_float(value, ratio) for key, value in payload.items()}


def _safe_scaled_float(value: Any, ratio: float) -> float:
    return float(str(value).replace(",", "").strip() or 0.0) * ratio


def _apply_fill_to_position_state(
    state: dict[str, Any],
    *,
    sleeve_id: str,
    fill: VirtualFillEvent,
    previous_quantity: int,
    portfolio: Portfolio,
) -> None:
    position_states = state.setdefault("position_states", {}).setdefault(sleeve_id, {})
    symbol_key = fill.symbol.key
    holding = portfolio.holdings.get(symbol_key)
    if holding is None or holding.quantity <= 0:
        position_states.pop(symbol_key, None)
        return

    existing_payload = position_states.get(symbol_key)
    existing = PositionState.from_dict(existing_payload) if existing_payload is not None else None
    if existing is None or previous_quantity <= 0:
        updated = PositionState(
            sleeve_id=sleeve_id,
            symbol=fill.symbol,
            quantity=holding.quantity,
            average_entry_price=holding.average_price,
            entry_time=fill.filled_at,
            high_watermark_price=fill.fill_price,
            high_watermark_at=fill.filled_at,
            last_price=fill.fill_price,
            last_updated_at=fill.filled_at,
        )
        position_states[symbol_key] = updated.to_dict()
        return

    high_watermark_price = existing.high_watermark_price
    high_watermark_at = existing.high_watermark_at
    if fill.fill_price > high_watermark_price:
        high_watermark_price = fill.fill_price
        high_watermark_at = fill.filled_at
    updated = PositionState(
        sleeve_id=sleeve_id,
        symbol=fill.symbol,
        quantity=holding.quantity,
        average_entry_price=holding.average_price,
        entry_time=existing.entry_time,
        high_watermark_price=high_watermark_price,
        high_watermark_at=high_watermark_at,
        last_price=fill.fill_price,
        last_updated_at=fill.filled_at,
        last_stop_price=existing.last_stop_price,
    )
    position_states[symbol_key] = updated.to_dict()


def _portfolio_from_dict(payload: dict[str, Any]) -> Portfolio:
    cash_by_currency = _cash_by_currency_from_payload(payload, "KRW")
    holdings = {}
    for symbol_key, raw in dict(payload.get("holdings") or {}).items():
        symbol = _symbol_from_dict(raw.get("symbol") or _symbol_dict_from_key(symbol_key))
        holdings[symbol.key] = Holding(
            symbol=symbol,
            quantity=int(raw.get("quantity") or 0),
            average_price=float(raw.get("average_price") or 0.0),
        )
    return Portfolio(cash=sum(cash_by_currency.values()), holdings=holdings, cash_by_currency=cash_by_currency)


def _portfolio_to_dict(portfolio: Portfolio) -> dict[str, Any]:
    cash_by_currency = {
        _currency_code(currency): float(amount)
        for currency, amount in dict(portfolio.cash_by_currency or {"KRW": portfolio.cash}).items()
        if abs(float(amount)) > 1e-12
    }
    return {
        "cash": sum(cash_by_currency.values()),
        "cash_by_currency": cash_by_currency,
        "holdings": {
            key: {
                "symbol": _symbol_to_dict(holding.symbol),
                "quantity": holding.quantity,
                "average_price": holding.average_price,
            }
            for key, holding in portfolio.holdings.items()
            if holding.quantity != 0
        },
    }


def _virtual_positions_from_state(state: dict[str, Any]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for sleeve in dict(state.get("sleeves") or {}).values():
        for symbol_key, raw in dict(sleeve.get("holdings") or {}).items():
            positions[symbol_key] = positions.get(symbol_key, 0) + int(raw.get("quantity") or 0)
    return positions


def _broker_positions_from_holdings(payload: dict[str, Any] | list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = payload.get("holdings", []) if isinstance(payload, dict) else payload
    positions: dict[str, dict[str, Any]] = {}
    if not isinstance(rows, list):
        return positions
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or row.get("ticker") or "").strip()
        if not ticker:
            continue
        symbol = Symbol(ticker=ticker, market=str(row.get("market") or "KRX"))
        quantity, quantity_source = _broker_position_quantity(row)
        positions[symbol.key] = {
            "symbol": _symbol_to_dict(symbol),
            "quantity": quantity,
            "average_price": _float_or_none(row.get("average_purchase_price") or row.get("average_price")),
            "quantity_source": quantity_source,
            "current_quantity": _int_or_none(row.get("current_quantity")),
            "settled_quantity": _int_or_none(row.get("settled_quantity")),
            "orderable_quantity": _int_or_none(row.get("orderable_quantity")),
        }
    return positions


def _broker_position_quantity(row: dict[str, Any]) -> tuple[int, str]:
    for key in ("current_quantity", "holding_quantity", "quantity"):
        quantity = _int_or_none(row.get(key))
        if quantity is not None:
            return quantity, str(row.get("quantity_source") or key)
    return 0, "missing"


def _currency_code(currency: str) -> str:
    code = str(currency or "").strip().upper()
    return code or "KRW"


def _account_cash_snapshot_key(account_id: str, currency: str) -> str:
    return f"{account_id}:{_currency_code(currency)}"


def _cash_by_currency_from_payload(payload: dict[str, Any], default_currency: str) -> dict[str, float]:
    raw = payload.get("cash_by_currency")
    if isinstance(raw, dict):
        return {
            _currency_code(str(currency)): float(amount or 0.0)
            for currency, amount in raw.items()
            if abs(float(amount or 0.0)) > 1e-12
        }
    cash = float(payload.get("cash") or 0.0)
    return {_currency_code(default_currency): cash} if abs(cash) > 1e-12 else {}


def _normalize_sleeve_cash(payload: dict[str, Any], default_currency: str) -> None:
    cash_by_currency = _cash_by_currency_from_payload(payload, default_currency)
    payload["cash_by_currency"] = cash_by_currency
    payload["cash"] = sum(cash_by_currency.values())


def _sleeve_cash_for_currency(payload: dict[str, Any], currency: str) -> float:
    cash_by_currency = _cash_by_currency_from_payload(payload, currency)
    return float(cash_by_currency.get(_currency_code(currency), 0.0))


def _set_sleeve_cash_for_currency(payload: dict[str, Any], currency: str, amount: float) -> None:
    cash_by_currency = _cash_by_currency_from_payload(payload, currency)
    code = _currency_code(currency)
    value = float(amount)
    if abs(value) > 1e-12:
        cash_by_currency[code] = value
    else:
        cash_by_currency.pop(code, None)
    payload["cash_by_currency"] = cash_by_currency
    payload["cash"] = sum(cash_by_currency.values())


def _adjust_sleeve_cash(payload: dict[str, Any], currency: str, delta: float) -> None:
    _set_sleeve_cash_for_currency(
        payload,
        currency,
        _sleeve_cash_for_currency(payload, currency) + float(delta),
    )


def _adjust_portfolio_cash(portfolio: Portfolio, currency: str, delta: float) -> None:
    portfolio.adjust_cash_for_currency(currency, delta)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    return float(text)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    return int(float(text))


def _broker_order_aliases(broker_order_id: str) -> tuple[str, ...]:
    text = str(broker_order_id or "").strip()
    if not text:
        return ()
    aliases = [text]
    for separator in (":", "|"):
        if separator in text:
            parts = [part.strip() for part in text.split(separator) if part.strip()]
            aliases.extend(parts)
            if len(parts) >= 2:
                aliases.append(parts[-1])
    return tuple(dict.fromkeys(aliases))


def _symbol_to_dict(symbol: Symbol) -> dict[str, str]:
    return {
        "ticker": symbol.ticker,
        "market": symbol.market,
    }


def _symbol_from_dict(payload: dict[str, Any]) -> Symbol:
    return Symbol(str(payload["ticker"]), str(payload.get("market") or "KR"))


def _symbol_dict_from_key(symbol_key: str) -> dict[str, str]:
    if ":" not in symbol_key:
        return {"market": "KR", "ticker": symbol_key}
    market, ticker = symbol_key.split(":", 1)
    return {"market": market, "ticker": ticker}


def _parse_optional_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text)

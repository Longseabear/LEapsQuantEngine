from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
from typing import Any

from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol
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
        )


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
class AccountCashSnapshot:
    account_id: str
    cash_balance: float
    synced_at: datetime
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
        synced_at: datetime | None = None,
    ) -> "AccountCashSnapshot":
        return cls(
            account_id=account_id,
            cash_balance=float(payload.get("cash_balance") or 0.0),
            synced_at=synced_at or datetime.now().astimezone(),
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
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "from_sleeve_id": self.from_sleeve_id,
            "to_sleeve_id": self.to_sleeve_id,
            "amount": self.amount,
            "occurred_at": self.occurred_at.isoformat(),
            "account_id": self.account_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CashReconciliationReport:
    account_id: str
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
        return "matched" if abs(self.difference) < 1e-6 else "mismatch"

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
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

    @property
    def remaining_quantity(self) -> int:
        return self.fill.quantity - self.allocated_quantity

    @property
    def status(self) -> str:
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
        }


@dataclass(frozen=True, slots=True)
class PositionReconciliationRow:
    symbol: Symbol
    broker_quantity: int
    virtual_quantity: int
    broker_average_price: float | None = None

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
        return sum(1 for status in self.allocation_statuses if status.remaining_quantity > 0)

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
        }


@dataclass(slots=True)
class VirtualSleeveAccountStore(PortfolioProvider):
    """File-backed virtual sleeve accounts for live/paper ownership state."""

    path: Path
    default_cash_by_sleeve: dict[str, float] | None = None

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        state = self._load_state()
        self._ensure_sleeve(state, sleeve_id)
        self._write_state(state)
        sleeve = state["sleeves"][sleeve_id]
        return _portfolio_from_dict(sleeve)

    def initialize_sleeve(self, sleeve_id: str, *, cash: float = 0.0, overwrite: bool = False) -> Portfolio:
        state = self._load_state()
        if overwrite or sleeve_id not in state["sleeves"]:
            state["sleeves"][sleeve_id] = {
                "cash": float(cash),
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
        transfer_id: str = "",
        occurred_at: datetime | None = None,
    ) -> CashTransfer:
        if amount <= 0:
            raise ValueError("cash transfer amount must be positive.")
        state = self._load_state()
        self._ensure_sleeve(state, from_sleeve_id)
        self._ensure_sleeve(state, to_sleeve_id)
        source = state["sleeves"][from_sleeve_id]
        if float(source.get("cash") or 0.0) < amount:
            raise ValueError("cash transfer exceeds source sleeve cash.")
        state["sleeves"][from_sleeve_id]["cash"] = float(source.get("cash") or 0.0) - amount
        state["sleeves"][to_sleeve_id]["cash"] = float(state["sleeves"][to_sleeve_id].get("cash") or 0.0) + amount
        event = CashTransfer(
            transfer_id=transfer_id or f"cash:{account_id}:{from_sleeve_id}:{to_sleeve_id}:{len(state['cash_transfers']) + 1}",
            from_sleeve_id=from_sleeve_id,
            to_sleeve_id=to_sleeve_id,
            amount=amount,
            occurred_at=occurred_at or datetime.now().astimezone(),
            account_id=account_id,
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
        residual_sleeve_id: str = DEFAULT_CASH_SLEEVE_ID,
        synced_at: datetime | None = None,
    ) -> CashReconciliationReport:
        snapshot = AccountCashSnapshot.from_balance_payload(
            balance_payload,
            account_id=account_id,
            synced_at=synced_at,
        )
        state = self._load_state()
        self._ensure_sleeve(state, residual_sleeve_id)
        state["account_cash_snapshots"][account_id] = snapshot.to_dict()
        non_residual_cash = sum(
            float(raw.get("cash") or 0.0)
            for sleeve_id, raw in state["sleeves"].items()
            if sleeve_id != residual_sleeve_id
        )
        state["sleeves"][residual_sleeve_id]["cash"] = snapshot.cash_balance - non_residual_cash
        self._write_state(state)
        return self.cash_reconciliation_report(
            account_id=account_id,
            residual_sleeve_id=residual_sleeve_id,
        )

    def cash_reconciliation_report(
        self,
        *,
        account_id: str = DEFAULT_ACCOUNT_ID,
        residual_sleeve_id: str = DEFAULT_CASH_SLEEVE_ID,
    ) -> CashReconciliationReport:
        state = self._load_state()
        snapshot_payload = state["account_cash_snapshots"].get(account_id)
        broker_cash_balance = float(snapshot_payload.get("cash_balance") or 0.0) if snapshot_payload else 0.0
        sleeve_cash = {
            sleeve_id: float(raw.get("cash") or 0.0)
            for sleeve_id, raw in state["sleeves"].items()
        }
        return CashReconciliationReport(
            account_id=account_id,
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
        if broker_order_id:
            state["broker_order_index"][broker_order_id] = record.order_id
        self._write_state(state)
        return record

    def apply_fill(self, fill: VirtualFillEvent) -> Portfolio:
        if fill.quantity <= 0:
            raise ValueError("fill quantity must be positive.")
        state = self._load_state()
        if fill.fill_id in state["fills"]:
            sleeve_id = state["fills"][fill.fill_id].get("sleeve_id") or UNKNOWN_SLEEVE_ID
            return _portfolio_from_dict(state["sleeves"][sleeve_id])

        sleeve_id = self._apply_fill_to_state(state, fill)
        self._write_state(state)
        return _portfolio_from_dict(state["sleeves"][sleeve_id])

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
                )
            )
        statuses = self.fill_allocation_statuses() if include_fills else ()
        return VirtualAccountReconciliationReport(rows=tuple(rows), allocation_statuses=statuses)

    def ownership_for_order(self, order_id: str) -> OrderOwnership | None:
        state = self._load_state()
        payload = state["order_ownership"].get(order_id)
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
            return
        default_cash = (self.default_cash_by_sleeve or {}).get(sleeve_id, 0.0)
        state["sleeves"][sleeve_id] = {
            "cash": float(default_cash),
            "holdings": {},
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
                "account_cash_snapshots": {},
                "cash_transfers": {},
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
        payload.setdefault("account_cash_snapshots", {})
        payload.setdefault("cash_transfers", {})
        return payload

    def _write_state(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, self.path)

    def _apply_fill_to_state(
        self,
        state: dict[str, Any],
        fill: VirtualFillEvent,
        *,
        record_unknown_ownership: bool = True,
    ) -> str:
        ownership = self._resolve_ownership(state, fill)
        sleeve_id = fill.sleeve_id or ownership.sleeve_id if ownership else fill.sleeve_id or UNKNOWN_SLEEVE_ID
        self._ensure_sleeve(state, sleeve_id)
        portfolio = _portfolio_from_dict(state["sleeves"][sleeve_id])
        _apply_fill_to_portfolio(portfolio, fill)
        state["sleeves"][sleeve_id] = _portfolio_to_dict(portfolio)
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
        return sleeve_id


def _apply_fill_to_portfolio(portfolio: Portfolio, fill: VirtualFillEvent) -> None:
    holding = portfolio.holdings.setdefault(fill.symbol.key, Holding(fill.symbol))
    if fill.side is OrderSide.BUY:
        previous_cost = holding.quantity * holding.average_price
        new_quantity = holding.quantity + fill.quantity
        holding.average_price = (previous_cost + fill.notional) / new_quantity
        holding.quantity = new_quantity
        portfolio.cash -= fill.notional + fill.fee
        return

    new_quantity = holding.quantity - fill.quantity
    if new_quantity < 0:
        raise ValueError("sell fill exceeds sleeve holding quantity.")
    portfolio.cash += max(fill.notional - fill.fee, 0.0)
    if new_quantity <= 0:
        portfolio.holdings.pop(fill.symbol.key, None)
        return
    holding.quantity = new_quantity


def _portfolio_from_dict(payload: dict[str, Any]) -> Portfolio:
    holdings = {}
    for symbol_key, raw in dict(payload.get("holdings") or {}).items():
        symbol = _symbol_from_dict(raw.get("symbol") or _symbol_dict_from_key(symbol_key))
        holdings[symbol.key] = Holding(
            symbol=symbol,
            quantity=int(raw.get("quantity") or 0),
            average_price=float(raw.get("average_price") or 0.0),
        )
    return Portfolio(cash=float(payload.get("cash") or 0.0), holdings=holdings)


def _portfolio_to_dict(portfolio: Portfolio) -> dict[str, Any]:
    return {
        "cash": portfolio.cash,
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
        positions[symbol.key] = {
            "symbol": _symbol_to_dict(symbol),
            "quantity": int(float(str(row.get("holding_quantity") or row.get("quantity") or 0).replace(",", ""))),
            "average_price": _float_or_none(row.get("average_purchase_price") or row.get("average_price")),
        }
    return positions


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    return float(text)


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

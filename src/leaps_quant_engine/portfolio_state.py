from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio, currency_for_symbol


@dataclass(frozen=True, slots=True)
class PortfolioHoldingSnapshot:
    symbol: Symbol
    quantity: int
    average_price: float
    market_price: float | None
    market_value: float
    cost_basis: float
    unrealized_pnl: float
    unrealized_pnl_pct: float | None

    @classmethod
    def from_holding(cls, holding: Holding, data: DataSlice) -> "PortfolioHoldingSnapshot":
        market_price = _mark_price(holding, data)
        market_value = holding.quantity * market_price if market_price is not None else 0.0
        cost_basis = holding.quantity * holding.average_price
        unrealized_pnl = market_value - cost_basis
        unrealized_pnl_pct = unrealized_pnl / cost_basis if cost_basis > 0 else None
        return cls(
            symbol=holding.symbol,
            quantity=holding.quantity,
            average_price=holding.average_price,
            market_price=market_price,
            market_value=market_value,
            cost_basis=cost_basis,
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=unrealized_pnl_pct,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.key,
            "quantity": self.quantity,
            "average_price": self.average_price,
            "market_price": self.market_price,
            "market_value": self.market_value,
            "cost_basis": self.cost_basis,
            "unrealized_pnl": self.unrealized_pnl,
            "unrealized_pnl_pct": self.unrealized_pnl_pct,
        }


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    sleeve_id: str
    as_of: datetime
    cash: float
    equity: float
    gross_exposure: float
    net_exposure: float
    cash_by_currency: dict[str, float] = field(default_factory=dict)
    equity_by_currency: dict[str, float] = field(default_factory=dict)
    holdings: tuple[PortfolioHoldingSnapshot, ...] = ()

    @classmethod
    def from_portfolio(
        cls,
        *,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
    ) -> "PortfolioSnapshot":
        holdings = tuple(
            PortfolioHoldingSnapshot.from_holding(holding, data)
            for holding in portfolio.holdings.values()
            if holding.quantity != 0
        )
        gross_exposure = sum(abs(holding.market_value) for holding in holdings)
        net_exposure = sum(holding.market_value for holding in holdings)
        cash_by_currency = dict(portfolio.cash_by_currency)
        if not cash_by_currency and portfolio.cash:
            currencies = {currency_for_symbol(holding.symbol) for holding in holdings}
            if len(currencies) == 1:
                cash_by_currency[next(iter(currencies))] = portfolio.cash
        equity_by_currency = portfolio.equity_by_currency(data, portfolio.currencies(data))
        scalar_equity = next(iter(equity_by_currency.values())) if len(equity_by_currency) == 1 else 0.0
        return cls(
            sleeve_id=sleeve_id,
            as_of=data.time,
            cash=portfolio.cash,
            equity=scalar_equity,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            cash_by_currency=cash_by_currency,
            equity_by_currency=equity_by_currency,
            holdings=holdings,
        )

    @property
    def held_symbol_count(self) -> int:
        return len(self.holdings)

    @property
    def gross_exposure_pct(self) -> float | None:
        if self.equity <= 0:
            return None
        return self.gross_exposure / self.equity

    @property
    def net_exposure_pct(self) -> float | None:
        if self.equity <= 0:
            return None
        return self.net_exposure / self.equity

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "as_of": self.as_of.isoformat(),
            "cash": self.cash,
            "cash_by_currency": self.cash_by_currency,
            "equity": self.equity,
            "equity_by_currency": self.equity_by_currency,
            "gross_exposure": self.gross_exposure,
            "net_exposure": self.net_exposure,
            "gross_exposure_pct": self.gross_exposure_pct,
            "net_exposure_pct": self.net_exposure_pct,
            "held_symbol_count": self.held_symbol_count,
            "holdings": [holding.to_dict() for holding in self.holdings],
        }


@dataclass(frozen=True, slots=True)
class PendingOrderSnapshot:
    order_intent_count: int = 0
    reserved_cash: float = 0.0
    reserved_sell_quantities: dict[str, int] = field(default_factory=dict)
    order_intents: tuple[OrderIntent, ...] = ()

    @classmethod
    def from_order_intents(cls, order_intents: tuple[OrderIntent, ...]) -> "PendingOrderSnapshot":
        reserved_cash = 0.0
        reserved_sell_quantities: dict[str, int] = {}
        for intent in order_intents:
            if intent.side is OrderSide.BUY:
                reserved_cash += intent.notional
                continue
            reserved_sell_quantities[intent.symbol.key] = (
                reserved_sell_quantities.get(intent.symbol.key, 0) + intent.quantity
            )
        return cls(
            order_intent_count=len(order_intents),
            reserved_cash=reserved_cash,
            reserved_sell_quantities=reserved_sell_quantities,
            order_intents=order_intents,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_intent_count": self.order_intent_count,
            "reserved_cash": self.reserved_cash,
            "reserved_sell_quantities": dict(self.reserved_sell_quantities),
            "order_intents": [
                {
                    "sleeve_id": intent.sleeve_id,
                    "symbol": intent.symbol.key,
                    "side": intent.side.value,
                    "quantity": intent.quantity,
                    "reference_price": intent.reference_price,
                    "notional": intent.notional,
                    "tag": intent.tag,
                }
                for intent in self.order_intents
            ],
        }


@dataclass(frozen=True, slots=True)
class PortfolioEngineState:
    sleeve_id: str
    as_of: datetime
    current: PortfolioSnapshot
    allocation_batch: Any | None = None
    target_batch: Any | None = None
    risk_decisions: Any | None = None
    pending: PendingOrderSnapshot = field(default_factory=PendingOrderSnapshot)

    @classmethod
    def from_cycle(
        cls,
        *,
        cycle: Any,
        portfolio: Portfolio,
        data: DataSlice,
    ) -> "PortfolioEngineState":
        return cls(
            sleeve_id=cycle.sleeve_id,
            as_of=data.time,
            current=PortfolioSnapshot.from_portfolio(
                sleeve_id=cycle.sleeve_id,
                portfolio=portfolio,
                data=data,
            ),
            allocation_batch=cycle.portfolio_target_batch,
            target_batch=cycle.order_sizing_batch,
            risk_decisions=cycle.risk_decisions,
            pending=PendingOrderSnapshot.from_order_intents(tuple(cycle.order_intents)),
        )

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "as_of": self.as_of.isoformat(),
            "current": self.current.to_dict(),
            "allocation": self._batch_to_dict(self.allocation_batch, include_details=include_details),
            "target": self._batch_to_dict(self.target_batch, include_details=include_details),
            "risk": self._batch_to_dict(self.risk_decisions, include_details=True),
            "pending": self.pending.to_dict(),
        }

    def _batch_to_dict(self, batch: Any | None, *, include_details: bool) -> dict[str, Any] | None:
        if batch is None:
            return None
        if not hasattr(batch, "to_dict"):
            return None
        if batch.__class__.__name__ in {"PortfolioTargetBatch", "OrderSizingBatch"} and not include_details:
            return {
                "batch_id": batch.batch_id,
                "sleeve_id": batch.sleeve_id,
                "generated_at": batch.generated_at.isoformat(),
                "model_name": batch.model_name,
                "target_count": batch.target_count,
                "plan_count": batch.plan_count,
                "metadata": dict(batch.metadata),
            }
        return batch.to_dict()


def _mark_price(holding: Holding, data: DataSlice) -> float | None:
    bar = data.get(holding.symbol)
    if bar is not None:
        return bar.close
    if holding.average_price > 0:
        return holding.average_price
    return None

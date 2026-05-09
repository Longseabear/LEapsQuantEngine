from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, Symbol


@dataclass(slots=True)
class Holding:
    symbol: Symbol
    quantity: int = 0
    average_price: float = 0.0


@dataclass(slots=True)
class Portfolio:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)

    @property
    def held_symbols(self) -> tuple[Symbol, ...]:
        return tuple(holding.symbol for holding in self.holdings.values() if holding.quantity != 0)

    def quantity(self, symbol: Symbol) -> int:
        holding = self.holdings.get(symbol.key)
        return holding.quantity if holding else 0

    def mark_price(self, symbol: Symbol, data: DataSlice) -> float | None:
        bar = data.get(symbol)
        if bar is not None:
            return bar.close
        holding = self.holdings.get(symbol.key)
        if holding is not None and holding.average_price > 0:
            return holding.average_price
        return None

    def position_value(self, symbol: Symbol, data: DataSlice) -> float:
        price = self.mark_price(symbol, data)
        if price is None:
            return 0.0
        return self.quantity(symbol) * price

    def equity(self, data: DataSlice) -> float:
        return self.cash + sum(
            self.position_value(holding.symbol, data)
            for holding in self.holdings.values()
        )

    def apply_fill(self, intent: OrderIntent) -> None:
        holding = self.holdings.setdefault(intent.symbol.key, Holding(intent.symbol))
        signed_quantity = intent.quantity if intent.side is OrderSide.BUY else -intent.quantity
        new_quantity = holding.quantity + signed_quantity
        cash_delta = intent.notional if intent.side is OrderSide.SELL else -intent.notional
        self.cash += cash_delta

        if new_quantity <= 0:
            self.holdings.pop(intent.symbol.key, None)
            return

        if intent.side is OrderSide.BUY:
            previous_cost = holding.quantity * holding.average_price
            holding.average_price = (previous_cost + intent.notional) / new_quantity
        holding.quantity = new_quantity


class PortfolioProvider(Protocol):
    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        """Return the current virtual portfolio for a sleeve."""


@dataclass(slots=True)
class StaticPortfolioProvider:
    portfolios: dict[str, Portfolio] = field(default_factory=dict)
    default_cash_by_sleeve: dict[str, float] = field(default_factory=dict)

    def current_portfolio(self, sleeve_id: str) -> Portfolio:
        portfolio = self.portfolios.get(sleeve_id)
        if portfolio is not None:
            return portfolio
        return Portfolio(cash=self.default_cash_by_sleeve.get(sleeve_id, 0.0))


@dataclass(frozen=True, slots=True)
class PortfolioView:
    cash: float
    quantities: dict[str, int]

    @classmethod
    def from_portfolio(cls, portfolio: Portfolio) -> "PortfolioView":
        return cls(
            cash=portfolio.cash,
            quantities={key: holding.quantity for key, holding in portfolio.holdings.items()},
        )

    def quantity(self, symbol: Symbol) -> int:
        return self.quantities.get(symbol.key, 0)

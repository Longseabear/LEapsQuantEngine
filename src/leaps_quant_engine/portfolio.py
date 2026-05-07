from __future__ import annotations

from dataclasses import dataclass, field

from leaps_quant_engine.models import OrderIntent, OrderSide, Symbol


@dataclass(slots=True)
class Holding:
    symbol: Symbol
    quantity: int = 0
    average_price: float = 0.0


@dataclass(slots=True)
class Portfolio:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)

    def quantity(self, symbol: Symbol) -> int:
        holding = self.holdings.get(symbol.key)
        return holding.quantity if holding else 0

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

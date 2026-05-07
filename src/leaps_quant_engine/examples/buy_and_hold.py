from __future__ import annotations

from leaps_quant_engine.algorithm import Algorithm
from leaps_quant_engine.models import DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import PortfolioView


class BuyAndHoldAlgorithm(Algorithm):
    def __init__(
        self,
        symbol: Symbol | None = None,
        quantity: int = 1,
        symbols: list[Symbol] | None = None,
    ) -> None:
        self.symbol = symbol or _first_symbol(symbols)
        self.quantity = quantity

    def on_data(self, data: DataSlice, portfolio: PortfolioView) -> list[PortfolioTarget]:
        if data.get(self.symbol) is None:
            return []
        return [PortfolioTarget(self.symbol, self.quantity, tag="buy-and-hold")]


def _first_symbol(symbols: list[Symbol] | None) -> Symbol:
    if not symbols:
        raise ValueError("BuyAndHoldAlgorithm requires symbol or symbols")
    return symbols[0]

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


@dataclass(frozen=True, slots=True)
class Symbol:
    ticker: str
    market: str = "KR"

    @property
    def key(self) -> str:
        return f"{self.market}:{self.ticker}"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: Symbol
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


@dataclass(frozen=True, slots=True)
class DataSlice:
    time: datetime
    bars: dict[str, Bar]

    def get(self, symbol: Symbol | str) -> Bar | None:
        key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.bars.get(key)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class PortfolioTarget:
    symbol: Symbol
    quantity: int
    tag: str = ""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    sleeve_id: str
    symbol: Symbol
    side: OrderSide
    quantity: int
    reference_price: float
    tag: str = ""

    @property
    def notional(self) -> float:
        return self.quantity * self.reference_price

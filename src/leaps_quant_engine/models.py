from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Symbol:
    ticker: str
    market: str = "KR"

    @property
    def key(self) -> str:
        return f"{self.market}:{self.ticker}"


class DataResolution(str, Enum):
    ANY = "any"
    DAILY = "daily"
    DAILY_CONFIRMED = "daily_confirmed"
    LIVE = "live"
    QUOTE = "quote"
    MINUTE = "minute"


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: Symbol
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    resolution: str = DataResolution.ANY.value


@dataclass(frozen=True, slots=True)
class DataSlice:
    time: datetime
    bars: dict[str, Bar]
    resolution: str = DataResolution.ANY.value

    def get(self, symbol: Symbol | str) -> Bar | None:
        key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.bars.get(key)


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


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
    order_type: OrderType | str = OrderType.LIMIT
    limit_price: float | None = None
    time_in_force: TimeInForce | str = TimeInForce.DAY
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_type", _coerce_order_type(self.order_type))
        object.__setattr__(self, "time_in_force", _coerce_time_in_force(self.time_in_force))
        order_type = _coerce_order_type(self.order_type)
        limit_price = None if self.limit_price is None else float(self.limit_price)
        if order_type is OrderType.MARKET:
            limit_price = None
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def notional(self) -> float:
        return self.quantity * self.reference_price


def _coerce_order_type(value: OrderType | str) -> OrderType:
    if isinstance(value, OrderType):
        return value
    return OrderType(str(value or OrderType.LIMIT.value).strip().lower())


def _coerce_time_in_force(value: TimeInForce | str) -> TimeInForce:
    if isinstance(value, TimeInForce):
        return value
    return TimeInForce(str(value or TimeInForce.DAY.value).strip().lower())

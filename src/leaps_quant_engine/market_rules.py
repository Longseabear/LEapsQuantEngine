from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from math import ceil, floor
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from leaps_quant_engine.models import OrderSide, OrderType, Symbol, TimeInForce


REGULAR_ORDERABLE_PHASES = frozenset({"regular_open_auction", "regular_continuous", "regular_close_auction"})
AFTER_HOURS_ORDERABLE_PHASES = frozenset({"pre_open_after_hours", "after_hours_close", "after_hours_single_price"})
ORDERABLE_PHASES = REGULAR_ORDERABLE_PHASES | AFTER_HOURS_ORDERABLE_PHASES


@dataclass(frozen=True, slots=True)
class MarketSession:
    market_scope: str
    session_phase: str
    is_orderable: bool
    is_regular_market_open: bool = False
    source: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "MarketSession":
        phase = str(payload.get("session_phase") or payload.get("phase") or "").strip()
        orderable = payload.get("is_orderable_session", payload.get("is_orderable"))
        if orderable is None:
            orderable = phase in ORDERABLE_PHASES
        return cls(
            market_scope=str(payload.get("market_scope") or payload.get("market") or "").strip().lower(),
            session_phase=phase,
            is_orderable=bool(orderable),
            is_regular_market_open=bool(payload.get("is_regular_market_open", payload.get("is_market_open", False))),
            source=str(payload.get("source") or ""),
        )


@dataclass(frozen=True, slots=True)
class BrokerRouteCapability:
    market_scope: str
    fractional_quantity: bool = False
    min_quantity: int = 1
    supports_market_order: bool = True
    supports_limit_order: bool = True
    supported_time_in_force: tuple[TimeInForce, ...] = (TimeInForce.DAY, TimeInForce.IOC, TimeInForce.FOK)
    enforce_tick_size: bool = True

    def supports(self, *, order_type: OrderType, time_in_force: TimeInForce) -> bool:
        if order_type is OrderType.MARKET and not self.supports_market_order:
            return False
        if order_type is OrderType.LIMIT and not self.supports_limit_order:
            return False
        return time_in_force in self.supported_time_in_force


def default_capability_for_market_scope(market_scope: str | None) -> BrokerRouteCapability:
    scope = str(market_scope or "domestic").strip().lower()
    if scope == "overseas":
        return BrokerRouteCapability(market_scope="overseas", fractional_quantity=False)
    return BrokerRouteCapability(market_scope="domestic", fractional_quantity=False)


def krx_tick_size(price: float) -> int:
    value = float(price)
    if value < 2_000:
        return 1
    if value < 5_000:
        return 5
    if value < 20_000:
        return 10
    if value < 50_000:
        return 50
    if value < 200_000:
        return 100
    if value < 500_000:
        return 500
    return 1_000


def is_valid_krx_tick(price: float) -> bool:
    tick = krx_tick_size(price)
    return abs(float(price) / tick - round(float(price) / tick)) <= 1e-9


def round_krx_price_to_tick(price: float, *, side: OrderSide) -> int:
    tick = krx_tick_size(price)
    value = float(price) / tick
    units = ceil(value) if side is OrderSide.BUY else floor(value)
    return int(max(units, 0) * tick)


def overseas_order_tick_size(price: float, *, exchange: str | None = None) -> Decimal:
    normalized_exchange = str(exchange or "").strip().upper()
    value = _decimal_price(price)
    if normalized_exchange in {"NASD", "NASDAQ", "NAS", "NYSE", "NYS", "AMEX", "AMS", "US"}:
        return Decimal("0.0001") if value < Decimal("1") else Decimal("0.01")
    return Decimal("0.0001")


def round_overseas_price_to_tick(price: float, *, side: OrderSide, exchange: str | None = None) -> float:
    value = _decimal_price(price)
    if value <= 0:
        return float(value)
    tick = overseas_order_tick_size(float(value), exchange=exchange)
    rounding = ROUND_CEILING if side is OrderSide.BUY else ROUND_FLOOR
    units = (value / tick).to_integral_value(rounding=rounding)
    normalized = max(units * tick, tick)
    return float(normalized)


def is_whole_share_quantity(quantity: Any) -> bool:
    try:
        numeric = float(quantity)
    except (TypeError, ValueError):
        return False
    return numeric >= 1 and numeric.is_integer()


def synthetic_domestic_market_session(now: datetime) -> MarketSession:
    current = now.time()
    phase = _domestic_session_phase(current)
    return MarketSession(
        market_scope="domestic",
        session_phase=phase,
        is_orderable=phase in ORDERABLE_PHASES,
        is_regular_market_open=phase in REGULAR_ORDERABLE_PHASES,
        source="synthetic_kst_clock",
    )


def synthetic_us_market_session(now: datetime) -> MarketSession:
    eastern_now = now.astimezone(ZoneInfo("America/New_York"))
    current = eastern_now.time()
    if eastern_now.weekday() >= 5:
        phase = "closed"
    elif time(4, 0) <= current < time(9, 30):
        phase = "pre_market"
    elif time(9, 30) <= current < time(16, 0):
        phase = "regular_continuous"
    elif time(16, 0) <= current < time(20, 0):
        phase = "after_market"
    else:
        phase = "closed"
    return MarketSession(
        market_scope="overseas",
        session_phase=phase,
        is_orderable=phase == "regular_continuous",
        is_regular_market_open=phase == "regular_continuous",
        source="synthetic_us_eastern_clock",
    )


def _domestic_session_phase(current: time) -> str:
    if time(8, 30) <= current < time(8, 40):
        return "pre_open_after_hours"
    if time(8, 40) <= current < time(9, 0):
        return "regular_open_auction"
    if time(9, 0) <= current < time(15, 20):
        return "regular_continuous"
    if time(15, 20) <= current < time(15, 30):
        return "regular_close_auction"
    if time(15, 40) <= current < time(16, 0):
        return "after_hours_close"
    if time(16, 0) <= current < time(18, 0):
        return "after_hours_single_price"
    return "closed"


def is_domestic_symbol(symbol: Symbol) -> bool:
    return symbol.market.upper() in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"}


def _decimal_price(price: float) -> Decimal:
    try:
        return Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("price must be numeric.") from exc

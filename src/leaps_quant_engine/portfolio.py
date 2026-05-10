from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, Symbol
from leaps_quant_engine.orders import OrderEvent


@dataclass(slots=True)
class Holding:
    symbol: Symbol
    quantity: int = 0
    average_price: float = 0.0


@dataclass(slots=True)
class Cash:
    currency: str
    amount: float = 0.0
    reserved: float = 0.0
    unsettled: float = 0.0

    def __post_init__(self) -> None:
        self.currency = _currency_code(self.currency)

    @property
    def available(self) -> float:
        return self.amount - self.reserved

    def to_dict(self) -> dict[str, float | str]:
        return {
            "currency": self.currency,
            "amount": self.amount,
            "reserved": self.reserved,
            "unsettled": self.unsettled,
            "available": self.available,
        }


@dataclass(slots=True)
class CashBook:
    balances: dict[str, Cash] = field(default_factory=dict)

    @classmethod
    def from_amounts(cls, amounts: dict[str, float]) -> "CashBook":
        book = cls()
        for currency, amount in amounts.items():
            if abs(float(amount)) > 1e-12:
                book.set_amount(currency, float(amount))
        return book

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.balances))

    @property
    def is_empty(self) -> bool:
        return not self.balances

    @property
    def is_multi_currency(self) -> bool:
        return len(self.balances) > 1

    def amount(self, currency: str) -> float:
        cash = self.balances.get(_currency_code(currency))
        return cash.amount if cash is not None else 0.0

    def available_amount(self, currency: str) -> float:
        cash = self.balances.get(_currency_code(currency))
        return cash.available if cash is not None else 0.0

    def set_amount(self, currency: str, amount: float) -> None:
        code = _currency_code(currency)
        value = float(amount)
        if abs(value) <= 1e-12:
            self.balances.pop(code, None)
            return
        cash = self.balances.get(code)
        if cash is None:
            self.balances[code] = Cash(currency=code, amount=value)
            return
        cash.amount = value

    def add_amount(self, currency: str, amount: float) -> None:
        code = _currency_code(currency)
        self.set_amount(code, self.amount(code) + float(amount))

    def to_amounts(self) -> dict[str, float]:
        return {
            currency: cash.amount
            for currency, cash in sorted(self.balances.items())
            if abs(cash.amount) > 1e-12
        }

    def to_dict(self) -> dict[str, dict[str, float | str]]:
        return {currency: cash.to_dict() for currency, cash in sorted(self.balances.items())}


@dataclass(slots=True)
class Portfolio:
    cash: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    cash_by_currency: dict[str, float] = field(default_factory=dict)
    cash_book: CashBook = field(default_factory=CashBook)

    def __post_init__(self) -> None:
        if self.cash_book.is_empty and self.cash_by_currency:
            self.cash_book = CashBook.from_amounts(self.cash_by_currency)
        self._sync_cash_views()

    @property
    def held_symbols(self) -> tuple[Symbol, ...]:
        return tuple(holding.symbol for holding in self.holdings.values() if holding.quantity != 0)

    def quantity(self, symbol: Symbol) -> int:
        holding = self.holdings.get(symbol.key)
        return holding.quantity if holding else 0

    def cash_for_currency(self, currency: str) -> float:
        code = _currency_code(currency)
        if not self.cash_book.is_empty:
            return self.cash_book.amount(code)
        return self.cash

    def cash_by_currency_for(self, currencies: Iterable[str]) -> dict[str, float]:
        codes = tuple(sorted({_currency_code(currency) for currency in currencies}))
        if not codes:
            return dict(self.cash_by_currency)
        if not self.cash_book.is_empty:
            return {code: self.cash_book.amount(code) for code in codes}
        if len(codes) == 1:
            return {codes[0]: self.cash}
        return {code: 0.0 for code in codes}

    def cash_for_symbol(self, symbol: Symbol) -> float:
        return self.cash_for_currency(currency_for_symbol(symbol))

    def set_cash_for_currency(self, currency: str, amount: float) -> None:
        self.cash_book.set_amount(currency, amount)
        self._sync_cash_views(preserve_scalar_if_empty=False)

    def adjust_cash_for_currency(self, currency: str, delta: float) -> None:
        if self.cash_book.is_empty:
            self.cash_book.set_amount(currency, self.cash)
        self.cash_book.add_amount(currency, delta)
        self._sync_cash_views(preserve_scalar_if_empty=False)

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

    def position_value_for_currency(self, currency: str, data: DataSlice) -> float:
        code = _currency_code(currency)
        return sum(
            self.position_value(holding.symbol, data)
            for holding in self.holdings.values()
            if currency_for_symbol(holding.symbol) == code
        )

    def equity_for_currency(self, currency: str, data: DataSlice) -> float:
        code = _currency_code(currency)
        return self.equity_by_currency(data, (code,)).get(code, 0.0)

    def equity_by_currency(
        self,
        data: DataSlice,
        currencies: Iterable[str] | None = None,
    ) -> dict[str, float]:
        codes = set(_currency_code(currency) for currency in currencies or ())
        codes.update(
            currency_for_symbol(holding.symbol)
            for holding in self.holdings.values()
            if holding.quantity != 0
        )
        if currencies is None:
            codes.update(self.cash_book.currencies)
            codes.update(currency_for_symbol(bar.symbol) for bar in data.bars.values())
        cash_by_currency = self.cash_by_currency_for(codes)
        codes.update(cash_by_currency)
        return {
            code: cash_by_currency.get(code, 0.0) + self.position_value_for_currency(code, data)
            for code in sorted(codes)
        }

    def equity(self, data: DataSlice) -> float:
        currencies = self.currencies(data)
        if len(currencies) > 1:
            raise ValueError("Portfolio.equity requires a currency when multiple currencies are present.")
        if len(currencies) == 1:
            return self.equity_by_currency(data, currencies)[currencies[0]]
        return self.cash + sum(
            self.position_value(holding.symbol, data)
            for holding in self.holdings.values()
        )

    def currencies(self, data: DataSlice | None = None) -> tuple[str, ...]:
        currencies = set(self.cash_book.currencies)
        currencies.update(
            currency_for_symbol(holding.symbol)
            for holding in self.holdings.values()
            if holding.quantity != 0
        )
        if data is not None:
            currencies.update(currency_for_symbol(bar.symbol) for bar in data.bars.values())
        return tuple(sorted(currencies))

    def apply_fill(self, intent: OrderIntent) -> None:
        holding = self.holdings.setdefault(intent.symbol.key, Holding(intent.symbol))
        signed_quantity = intent.quantity if intent.side is OrderSide.BUY else -intent.quantity
        new_quantity = holding.quantity + signed_quantity
        cash_delta = intent.notional if intent.side is OrderSide.SELL else -intent.notional
        if not self.cash_book.is_empty:
            self.adjust_cash_for_currency(currency_for_symbol(intent.symbol), cash_delta)
        else:
            self.cash += cash_delta

        if new_quantity <= 0:
            self.holdings.pop(intent.symbol.key, None)
            return

        if intent.side is OrderSide.BUY:
            previous_cost = holding.quantity * holding.average_price
            holding.average_price = (previous_cost + intent.notional) / new_quantity
        holding.quantity = new_quantity

    def apply_order_event(self, event: OrderEvent) -> None:
        if not event.is_fill or event.quantity <= 0 or event.fill_price is None:
            return
        holding = self.holdings.setdefault(event.symbol.key, Holding(event.symbol))
        signed_quantity = event.quantity if event.side is OrderSide.BUY else -event.quantity
        new_quantity = holding.quantity + signed_quantity
        fee = _float_or_zero(event.metadata.get("fee") if event.metadata else 0.0)
        cash_delta = max(event.notional - fee, 0.0) if event.side is OrderSide.SELL else -(event.notional + fee)
        if not self.cash_book.is_empty:
            self.adjust_cash_for_currency(currency_for_symbol(event.symbol), cash_delta)
        else:
            self.cash += cash_delta

        if new_quantity <= 0:
            self.holdings.pop(event.symbol.key, None)
            return

        if event.side is OrderSide.BUY:
            previous_cost = holding.quantity * holding.average_price
            holding.average_price = (previous_cost + event.notional) / new_quantity
        holding.quantity = new_quantity

    def _sync_cash_views(self, *, preserve_scalar_if_empty: bool = True) -> None:
        if self.cash_book.is_empty:
            self.cash_by_currency = {}
            if not preserve_scalar_if_empty:
                self.cash = 0.0
            return
        self.cash_by_currency = self.cash_book.to_amounts()
        if len(self.cash_by_currency) == 1:
            self.cash = next(iter(self.cash_by_currency.values()))
        else:
            self.cash = sum(self.cash_by_currency.values())


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


def _currency_code(currency: str) -> str:
    code = str(currency or "").strip().upper()
    return code or "KRW"


def _float_or_zero(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def currency_for_symbol(symbol: Symbol) -> str:
    return "KRW" if symbol.market.upper() in {"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"} else "USD"

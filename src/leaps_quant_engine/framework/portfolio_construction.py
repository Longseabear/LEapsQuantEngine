from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.models import DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class PortfolioConstructionContext:
    sleeve_id: str
    data: DataSlice
    portfolio: Portfolio
    active_insights: tuple[Insight, ...]
    managed_symbols: tuple[Symbol, ...]


class PortfolioConstructionModel(Protocol):
    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioTarget, ...]:
        """Convert active insights into desired holdings."""


@dataclass(frozen=True, slots=True)
class EqualWeightPortfolioConstructionModel:
    max_portfolio_pct: float = 1.0
    long_only: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_portfolio_pct <= 1.0:
            raise ValueError("max_portfolio_pct must be between 0 and 1.")

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioTarget, ...]:
        latest_by_symbol = _latest_insight_by_symbol(context.active_insights)
        actionable = tuple(
            insight
            for insight in latest_by_symbol.values()
            if insight.direction is InsightDirection.UP
            or (not self.long_only and insight.direction is InsightDirection.DOWN)
        )
        target_qty_by_symbol: dict[str, PortfolioTarget] = {}
        if actionable:
            equal_weight = self.max_portfolio_pct / len(actionable)
            for insight in actionable:
                signed_weight = _target_weight(insight, equal_weight, long_only=self.long_only)
                target_qty_by_symbol[insight.symbol_key] = PortfolioTarget(
                    symbol=insight.symbol,
                    quantity=_quantity_for_weight(context, insight.symbol, signed_weight),
                    tag=f"framework:{insight.alpha_id}:{insight.direction.value}",
                )

        for symbol in context.managed_symbols:
            if symbol.key in target_qty_by_symbol:
                continue
            if context.portfolio.quantity(symbol) == 0:
                continue
            target_qty_by_symbol[symbol.key] = PortfolioTarget(
                symbol=symbol,
                quantity=0,
                tag="framework:insight_inactive",
            )
        return tuple(target_qty_by_symbol.values())


def _latest_insight_by_symbol(insights: tuple[Insight, ...]) -> dict[str, Insight]:
    latest: dict[str, Insight] = {}
    for insight in insights:
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[insight.symbol_key] = insight
    return latest


def _target_weight(insight: Insight, equal_weight: float, *, long_only: bool) -> float:
    weight = abs(insight.weight) if insight.weight is not None else equal_weight
    weight = min(weight, equal_weight)
    if insight.direction is InsightDirection.DOWN and not long_only:
        return -weight
    return weight


def _quantity_for_weight(context: PortfolioConstructionContext, symbol: Symbol, weight: float) -> int:
    bar = context.data.get(symbol)
    if bar is None or bar.close <= 0:
        return context.portfolio.quantity(symbol)
    equity = _portfolio_equity(context.portfolio, context.data)
    return int((equity * weight) // bar.close)


def _portfolio_equity(portfolio: Portfolio, data: DataSlice) -> float:
    equity = portfolio.cash
    for holding in portfolio.holdings.values():
        bar = data.get(holding.symbol)
        price = bar.close if bar is not None else holding.average_price
        equity += holding.quantity * price
    return equity


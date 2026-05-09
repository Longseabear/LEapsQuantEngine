from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4

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
    portfolio_equity: float = 0.0
    target_portfolio_value: float = 0.0

    @property
    def equity(self) -> float:
        return self.portfolio_equity if self.portfolio_equity > 0 else _portfolio_equity(self.portfolio, self.data)

    @property
    def target_value(self) -> float:
        return self.target_portfolio_value if self.target_portfolio_value > 0 else self.equity


class PortfolioConstructionModel(Protocol):
    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioTarget, ...]:
        """Convert active insights into desired holdings."""


@dataclass(frozen=True, slots=True)
class PortfolioTargetBatch:
    sleeve_id: str
    generated_at: datetime
    targets: tuple[PortfolioTarget, ...]
    source_insight_ids: tuple[str, ...] = ()
    model_name: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    batch_id: str = field(default_factory=lambda: f"portfolio-targets-{uuid4()}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def target_count(self) -> int:
        return len(self.targets)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "generated_at": self.generated_at.isoformat(),
            "model_name": self.model_name,
            "reason": self.reason,
            "source_insight_ids": list(self.source_insight_ids),
            "target_count": self.target_count,
            "targets": [
                {
                    "symbol": target.symbol.key,
                    "quantity": target.quantity,
                    "tag": target.tag,
                }
                for target in self.targets
            ],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RebalancePolicy:
    cash_reserve_pct: float = 0.0
    min_order_notional: float = 0.0
    min_quantity_delta: int = 1
    allow_exit_below_min_notional: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.cash_reserve_pct < 1.0:
            raise ValueError("cash_reserve_pct must be between 0 inclusive and 1 exclusive.")
        if self.min_order_notional < 0:
            raise ValueError("min_order_notional cannot be negative.")
        if self.min_quantity_delta < 0:
            raise ValueError("min_quantity_delta cannot be negative.")


@dataclass(frozen=True, slots=True)
class PortfolioConstructionEngine:
    model: PortfolioConstructionModel
    rebalance_policy: RebalancePolicy = field(default_factory=RebalancePolicy)
    reason: str = "portfolio_construction"

    def create_targets(self, context: PortfolioConstructionContext) -> PortfolioTargetBatch:
        equity = _portfolio_equity(context.portfolio, context.data)
        target_value = equity * (1.0 - self.rebalance_policy.cash_reserve_pct)
        prepared_context = replace(
            context,
            portfolio_equity=equity,
            target_portfolio_value=target_value,
        )
        raw_targets = self.model.create_targets(prepared_context)
        targets = self._filter_rebalance_noise(prepared_context, raw_targets)
        return PortfolioTargetBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.data.time,
            targets=targets,
            source_insight_ids=tuple(insight.insight_id for insight in context.active_insights),
            model_name=type(self.model).__name__,
            reason=self.reason,
            metadata={
                "raw_target_count": len(raw_targets),
                "filtered_target_count": len(targets),
                "portfolio_equity": equity,
                "target_portfolio_value": target_value,
                "cash_reserve_pct": self.rebalance_policy.cash_reserve_pct,
                "min_order_notional": self.rebalance_policy.min_order_notional,
                "min_quantity_delta": self.rebalance_policy.min_quantity_delta,
            },
        )

    def _filter_rebalance_noise(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioTarget, ...],
    ) -> tuple[PortfolioTarget, ...]:
        filtered: list[PortfolioTarget] = []
        for target in targets:
            current_quantity = context.portfolio.quantity(target.symbol)
            delta = target.quantity - current_quantity
            if delta == 0:
                continue
            if abs(delta) < self.rebalance_policy.min_quantity_delta:
                continue
            if self._below_min_notional(context, target, delta, current_quantity=current_quantity):
                continue
            filtered.append(target)
        return tuple(filtered)

    def _below_min_notional(
        self,
        context: PortfolioConstructionContext,
        target: PortfolioTarget,
        delta: int,
        *,
        current_quantity: int,
    ) -> bool:
        min_notional = self.rebalance_policy.min_order_notional
        if min_notional <= 0:
            return False
        if (
            self.rebalance_policy.allow_exit_below_min_notional
            and target.quantity == 0
            and current_quantity != 0
        ):
            return False
        bar = context.data.get(target.symbol)
        if bar is None:
            return True
        return abs(delta) * bar.close < min_notional


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
    return int((context.target_value * weight) // bar.close)


def _portfolio_equity(portfolio: Portfolio, data: DataSlice) -> float:
    equity = portfolio.cash
    for holding in portfolio.holdings.values():
        bar = data.get(holding.symbol)
        price = bar.close if bar is not None else holding.average_price
        equity += holding.quantity * price
    return equity

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

    @property
    def held_symbols(self) -> tuple[Symbol, ...]:
        return self.portfolio.held_symbols


class PortfolioConstructionModel(Protocol):
    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioTarget, ...]:
        """Convert active insights into desired holdings."""


@dataclass(frozen=True, slots=True)
class PortfolioTargetPlan:
    target: PortfolioTarget
    current_quantity: int
    target_quantity: int
    delta_quantity: int
    current_price: float | None
    current_value: float
    target_value: float
    delta_value: float
    source_insight_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_entry(self) -> bool:
        return self.current_quantity == 0 and self.target_quantity > 0

    @property
    def is_exit(self) -> bool:
        return self.current_quantity != 0 and self.target_quantity == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.target.symbol.key,
            "current_quantity": self.current_quantity,
            "target_quantity": self.target_quantity,
            "delta_quantity": self.delta_quantity,
            "current_price": self.current_price,
            "current_value": self.current_value,
            "target_value": self.target_value,
            "delta_value": self.delta_value,
            "source_insight_ids": list(self.source_insight_ids),
            "reason": self.reason,
            "tag": self.target.tag,
            "is_entry": self.is_entry,
            "is_exit": self.is_exit,
        }


@dataclass(frozen=True, slots=True)
class PortfolioTargetBatch:
    sleeve_id: str
    generated_at: datetime
    targets: tuple[PortfolioTarget, ...]
    plans: tuple[PortfolioTargetPlan, ...] = ()
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

    @property
    def plan_count(self) -> int:
        return len(self.plans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "generated_at": self.generated_at.isoformat(),
            "model_name": self.model_name,
            "reason": self.reason,
            "source_insight_ids": list(self.source_insight_ids),
            "target_count": self.target_count,
            "plan_count": self.plan_count,
            "targets": [
                {
                    "symbol": target.symbol.key,
                    "quantity": target.quantity,
                    "tag": target.tag,
                }
                for target in self.targets
            ],
            "plans": [plan.to_dict() for plan in self.plans],
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
        raw_plans = self._build_plans(prepared_context, raw_targets)
        plans = self._filter_rebalance_noise(raw_plans)
        targets = tuple(plan.target for plan in plans)
        return PortfolioTargetBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.data.time,
            targets=targets,
            plans=plans,
            source_insight_ids=tuple(insight.insight_id for insight in context.active_insights),
            model_name=type(self.model).__name__,
            reason=self.reason,
            metadata={
                "raw_target_count": len(raw_targets),
                "filtered_target_count": len(targets),
                "raw_plan_count": len(raw_plans),
                "filtered_plan_count": len(plans),
                "portfolio_equity": equity,
                "target_portfolio_value": target_value,
                "cash_reserve_pct": self.rebalance_policy.cash_reserve_pct,
                "min_order_notional": self.rebalance_policy.min_order_notional,
                "min_quantity_delta": self.rebalance_policy.min_quantity_delta,
            },
        )

    def _build_plans(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioTarget, ...],
    ) -> tuple[PortfolioTargetPlan, ...]:
        insight_ids_by_symbol = _insight_ids_by_symbol(context.active_insights)
        return tuple(
            _target_plan(
                context,
                target,
                source_insight_ids=insight_ids_by_symbol.get(target.symbol.key, ()),
                reason=_target_reason(target),
            )
            for target in targets
        )

    def _filter_rebalance_noise(self, plans: tuple[PortfolioTargetPlan, ...]) -> tuple[PortfolioTargetPlan, ...]:
        filtered: list[PortfolioTargetPlan] = []
        for plan in plans:
            if plan.delta_quantity == 0:
                continue
            if abs(plan.delta_quantity) < self.rebalance_policy.min_quantity_delta:
                continue
            if self._below_min_notional(plan):
                continue
            filtered.append(plan)
        return tuple(filtered)

    def _below_min_notional(self, plan: PortfolioTargetPlan) -> bool:
        min_notional = self.rebalance_policy.min_order_notional
        if min_notional <= 0:
            return False
        if (
            self.rebalance_policy.allow_exit_below_min_notional
            and plan.target_quantity == 0
            and plan.current_quantity != 0
        ):
            return False
        if plan.current_price is None:
            return True
        return abs(plan.delta_quantity) * plan.current_price < min_notional


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

        for symbol in _symbols_by_key((*context.managed_symbols, *context.held_symbols)).values():
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


def _insight_ids_by_symbol(insights: tuple[Insight, ...]) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {}
    for insight in insights:
        result.setdefault(insight.symbol_key, []).append(insight.insight_id)
    return {symbol_key: tuple(insight_ids) for symbol_key, insight_ids in result.items()}


def _symbols_by_key(symbols: tuple[Symbol, ...]) -> dict[str, Symbol]:
    return {symbol.key: symbol for symbol in symbols}


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


def _target_plan(
    context: PortfolioConstructionContext,
    target: PortfolioTarget,
    *,
    source_insight_ids: tuple[str, ...],
    reason: str,
) -> PortfolioTargetPlan:
    current_quantity = context.portfolio.quantity(target.symbol)
    current_price = context.portfolio.mark_price(target.symbol, context.data)
    current_value = context.portfolio.position_value(target.symbol, context.data)
    target_value = (target.quantity * current_price) if current_price is not None else 0.0
    return PortfolioTargetPlan(
        target=target,
        current_quantity=current_quantity,
        target_quantity=target.quantity,
        delta_quantity=target.quantity - current_quantity,
        current_price=current_price,
        current_value=current_value,
        target_value=target_value,
        delta_value=target_value - current_value,
        source_insight_ids=source_insight_ids,
        reason=reason,
    )


def _target_reason(target: PortfolioTarget) -> str:
    if target.quantity == 0:
        return "exit"
    return "target"


def _portfolio_equity(portfolio: Portfolio, data: DataSlice) -> float:
    return portfolio.equity(data)

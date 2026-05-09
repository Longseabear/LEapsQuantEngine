from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioTargetBatch,
    PortfolioTargetPlan,
    RebalancePolicy,
)
from leaps_quant_engine.models import DataSlice, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class OrderSizingContext:
    sleeve_id: str
    data: DataSlice
    portfolio: Portfolio
    portfolio_targets: PortfolioTargetBatch


@dataclass(frozen=True, slots=True)
class OrderSizingPlan:
    allocation: PortfolioAllocationTarget
    current_quantity: int
    target_quantity: int
    delta_quantity: int
    current_price: float | None
    current_value: float
    desired_value: float
    rounded_value: float
    rounding_loss: float
    target_percent: float
    source_insight_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_entry(self) -> bool:
        return self.current_quantity == 0 and self.target_quantity != 0

    @property
    def is_exit(self) -> bool:
        return self.current_quantity != 0 and self.target_quantity == 0

    @property
    def target(self) -> PortfolioTarget:
        return PortfolioTarget(
            symbol=self.allocation.symbol,
            quantity=self.target_quantity,
            tag=self.allocation.tag,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.allocation.symbol.key,
            "current_quantity": self.current_quantity,
            "target_quantity": self.target_quantity,
            "delta_quantity": self.delta_quantity,
            "current_price": self.current_price,
            "current_value": self.current_value,
            "target_percent": self.target_percent,
            "desired_value": self.desired_value,
            "rounded_value": self.rounded_value,
            "rounding_loss": self.rounding_loss,
            "source_insight_ids": list(self.source_insight_ids),
            "reason": self.reason,
            "tag": self.allocation.tag,
            "is_entry": self.is_entry,
            "is_exit": self.is_exit,
        }


@dataclass(frozen=True, slots=True)
class OrderSizingBatch:
    sleeve_id: str
    generated_at: datetime
    targets: tuple[PortfolioTarget, ...]
    plans: tuple[OrderSizingPlan, ...]
    source_batch_id: str
    model_name: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    batch_id: str = field(default_factory=lambda: f"order-sizing-{uuid4()}")

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
            "source_batch_id": self.source_batch_id,
            "sleeve_id": self.sleeve_id,
            "generated_at": self.generated_at.isoformat(),
            "model_name": self.model_name,
            "reason": self.reason,
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
class OrderSizingEngine:
    rebalance_policy: RebalancePolicy = field(default_factory=RebalancePolicy)
    reason: str = "order_sizing"

    def size(self, context: OrderSizingContext) -> OrderSizingBatch:
        raw_plans = tuple(
            _sizing_plan(context, plan)
            for plan in context.portfolio_targets.plans
        )
        plans = self._filter_rebalance_noise(raw_plans)
        targets = tuple(plan.target for plan in plans)
        return OrderSizingBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.data.time,
            targets=targets,
            plans=plans,
            source_batch_id=context.portfolio_targets.batch_id,
            model_name=type(self).__name__,
            reason=self.reason,
            metadata={
                "raw_target_count": len(context.portfolio_targets.targets),
                "filtered_target_count": len(targets),
                "raw_plan_count": len(raw_plans),
                "filtered_plan_count": len(plans),
                "cash_reserve_pct": self.rebalance_policy.cash_reserve_pct,
                "min_order_notional": self.rebalance_policy.min_order_notional,
                "min_quantity_delta": self.rebalance_policy.min_quantity_delta,
                "rounding_loss": sum(abs(plan.rounding_loss) for plan in raw_plans),
            },
        )

    def _filter_rebalance_noise(self, plans: tuple[OrderSizingPlan, ...]) -> tuple[OrderSizingPlan, ...]:
        filtered: list[OrderSizingPlan] = []
        for plan in plans:
            if plan.delta_quantity == 0:
                continue
            if abs(plan.delta_quantity) < self.rebalance_policy.min_quantity_delta:
                continue
            if self._below_min_notional(plan):
                continue
            filtered.append(plan)
        return tuple(filtered)

    def _below_min_notional(self, plan: OrderSizingPlan) -> bool:
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


def _sizing_plan(context: OrderSizingContext, plan: PortfolioTargetPlan) -> OrderSizingPlan:
    price = plan.current_price
    current_quantity = context.portfolio.quantity(plan.target.symbol)
    if price is None or price <= 0:
        target_quantity = current_quantity
        rounded_value = current_quantity * (price or 0.0)
    else:
        target_quantity = _quantity_for_desired_value(plan.desired_value, price)
        rounded_value = target_quantity * price
    return OrderSizingPlan(
        allocation=plan.target,
        current_quantity=current_quantity,
        target_quantity=target_quantity,
        delta_quantity=target_quantity - current_quantity,
        current_price=price,
        current_value=plan.current_value,
        desired_value=plan.desired_value,
        rounded_value=rounded_value,
        rounding_loss=plan.desired_value - rounded_value,
        target_percent=plan.target_percent,
        source_insight_ids=plan.source_insight_ids,
        reason=plan.reason,
    )


def _quantity_for_desired_value(desired_value: float, price: float) -> int:
    if desired_value >= 0:
        return int(desired_value // price)
    return -int(abs(desired_value) // price)

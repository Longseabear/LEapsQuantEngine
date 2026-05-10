from __future__ import annotations

from dataclasses import dataclass, field, replace
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
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol


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
    lot_optimizer_enabled: bool = True
    lot_optimizer_min_lot_fraction: float = 0.25

    def size(self, context: OrderSizingContext) -> OrderSizingBatch:
        source_plans = _plans_by_symbol(context.portfolio_targets)
        target_value_by_currency = _target_value_by_currency(context, self.rebalance_policy)
        raw_plans = tuple(
            _sizing_plan(
                context,
                target,
                source_plan=source_plans.get(target.symbol.key),
                target_value_by_currency=target_value_by_currency,
            )
            for target in context.portfolio_targets.targets
        )
        sized_plans, lot_optimizer_metadata = self._optimize_lots(raw_plans, target_value_by_currency)
        plans = self._filter_rebalance_noise(sized_plans)
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
                "rounding_loss": sum(abs(plan.rounding_loss) for plan in sized_plans),
                "raw_rounding_loss": sum(abs(plan.rounding_loss) for plan in raw_plans),
                "portfolio_equity_by_currency": _portfolio_equity_by_currency(context),
                "target_portfolio_value_by_currency": target_value_by_currency,
                "recomputed_from_current_state": True,
                **lot_optimizer_metadata,
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

    def _optimize_lots(
        self,
        plans: tuple[OrderSizingPlan, ...],
        target_value_by_currency: Mapping[str, float],
    ) -> tuple[tuple[OrderSizingPlan, ...], dict[str, Any]]:
        if not self.lot_optimizer_enabled:
            return plans, {"lot_optimizer_enabled": False}

        next_quantities = [plan.target_quantity for plan in plans]
        adjustment_count = 0
        deployed_notional = 0.0
        cash_left_by_currency: dict[str, float] = {}

        currencies = sorted({currency_for_symbol(plan.allocation.symbol) for plan in plans})
        for currency in currencies:
            indexes = [
                index
                for index, plan in enumerate(plans)
                if currency_for_symbol(plan.allocation.symbol) == currency
                and plan.target_percent > 0
                and plan.desired_value > 0
                and plan.current_price is not None
                and plan.current_price > 0
            ]
            if not indexes:
                continue

            intended_budget = min(
                target_value_by_currency.get(currency, 0.0),
                sum(max(plans[index].desired_value, 0.0) for index in indexes),
            )
            if intended_budget <= 0:
                continue

            spent = sum(max(next_quantities[index], 0) * (plans[index].current_price or 0.0) for index in indexes)
            available = max(0.0, intended_budget - spent)

            while True:
                best_index: int | None = None
                best_score = 0.0
                for index in indexes:
                    plan = plans[index]
                    price = plan.current_price or 0.0
                    if price <= 0 or price > available:
                        continue

                    quantity = max(next_quantities[index], 0)
                    desired_lots = plan.desired_value / price
                    lot_gap = desired_lots - quantity
                    if lot_gap > 0:
                        score = min(lot_gap, 1.0)
                    elif quantity == 0 and desired_lots >= self.lot_optimizer_min_lot_fraction:
                        score = desired_lots
                    else:
                        continue

                    if score < self.lot_optimizer_min_lot_fraction:
                        continue
                    score *= max(plan.target_percent, 1e-9)
                    if score > best_score:
                        best_score = score
                        best_index = index

                if best_index is None:
                    break

                price = plans[best_index].current_price or 0.0
                next_quantities[best_index] += 1
                available -= price
                adjustment_count += 1
                deployed_notional += price

            cash_left_by_currency[currency] = available

        if adjustment_count == 0:
            return plans, {
                "lot_optimizer_enabled": True,
                "lot_optimizer_adjustment_count": 0,
                "lot_optimizer_deployed_notional": 0.0,
                "lot_optimizer_cash_left_by_currency": cash_left_by_currency,
            }

        optimized: list[OrderSizingPlan] = []
        for index, plan in enumerate(plans):
            target_quantity = next_quantities[index]
            if target_quantity == plan.target_quantity:
                optimized.append(plan)
                continue
            price = plan.current_price or 0.0
            rounded_value = target_quantity * price
            optimized.append(
                replace(
                    plan,
                    target_quantity=target_quantity,
                    delta_quantity=target_quantity - plan.current_quantity,
                    rounded_value=rounded_value,
                    rounding_loss=plan.desired_value - rounded_value,
                )
            )

        return tuple(optimized), {
            "lot_optimizer_enabled": True,
            "lot_optimizer_adjustment_count": adjustment_count,
            "lot_optimizer_deployed_notional": deployed_notional,
            "lot_optimizer_cash_left_by_currency": cash_left_by_currency,
        }


def _sizing_plan(
    context: OrderSizingContext,
    allocation: PortfolioAllocationTarget,
    *,
    source_plan: PortfolioTargetPlan | None,
    target_value_by_currency: Mapping[str, float],
) -> OrderSizingPlan:
    price = context.portfolio.mark_price(allocation.symbol, context.data)
    current_quantity = context.portfolio.quantity(allocation.symbol)
    current_value = context.portfolio.position_value(allocation.symbol, context.data)
    desired_value = target_value_by_currency.get(currency_for_symbol(allocation.symbol), 0.0) * allocation.target_percent
    if price is None or price <= 0:
        target_quantity = current_quantity
        rounded_value = current_quantity * (price or 0.0)
    else:
        target_quantity = _quantity_for_desired_value(desired_value, price)
        rounded_value = target_quantity * price
    return OrderSizingPlan(
        allocation=allocation,
        current_quantity=current_quantity,
        target_quantity=target_quantity,
        delta_quantity=target_quantity - current_quantity,
        current_price=price,
        current_value=current_value,
        desired_value=desired_value,
        rounded_value=rounded_value,
        rounding_loss=desired_value - rounded_value,
        target_percent=allocation.target_percent,
        source_insight_ids=source_plan.source_insight_ids if source_plan is not None else (),
        reason=source_plan.reason if source_plan is not None else _target_reason(allocation),
    )


def _quantity_for_desired_value(desired_value: float, price: float) -> int:
    if desired_value >= 0:
        return int(desired_value // price)
    return -int(abs(desired_value) // price)


def _plans_by_symbol(batch: PortfolioTargetBatch) -> dict[str, PortfolioTargetPlan]:
    return {plan.target.symbol.key: plan for plan in batch.plans}


def _target_value_by_currency(
    context: OrderSizingContext,
    rebalance_policy: RebalancePolicy,
) -> dict[str, float]:
    return {
        currency: equity * (1.0 - rebalance_policy.cash_reserve_pct)
        for currency, equity in _portfolio_equity_by_currency(context).items()
    }


def _portfolio_equity_by_currency(context: OrderSizingContext) -> dict[str, float]:
    currencies = set(context.portfolio.currencies(context.data))
    currencies.update(currency_for_symbol(target.symbol) for target in context.portfolio_targets.targets)
    if not currencies:
        currencies = {"KRW"}
    return context.portfolio.equity_by_currency(context.data, currencies)


def _target_reason(target: PortfolioAllocationTarget) -> str:
    if target.target_percent == 0:
        return "exit"
    return "target"

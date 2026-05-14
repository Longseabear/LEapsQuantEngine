from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import inspect
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.models import DataSlice, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol
from leaps_quant_engine.runtime_state import RuntimeModelStateView, StatePatch


@dataclass(frozen=True, slots=True)
class PortfolioConstructionContext:
    sleeve_id: str
    data: DataSlice
    portfolio: Portfolio
    active_insights: tuple[Insight, ...]
    managed_symbols: tuple[Symbol, ...]
    portfolio_equity: float = 0.0
    target_portfolio_value: float = 0.0
    portfolio_equity_by_currency: Mapping[str, float] = field(default_factory=dict)
    target_portfolio_value_by_currency: Mapping[str, float] = field(default_factory=dict)
    model_state: RuntimeModelStateView = field(default_factory=RuntimeModelStateView)

    @property
    def equity(self) -> float:
        if self.portfolio_equity > 0:
            return self.portfolio_equity
        return _single_currency_value(
            _portfolio_equity_by_currency(self.portfolio, self.data, _relevant_symbols(self))
        )

    @property
    def target_value(self) -> float:
        return self.target_portfolio_value if self.target_portfolio_value > 0 else self.equity

    def equity_for_symbol(self, symbol: Symbol) -> float:
        currency = currency_for_symbol(symbol)
        if currency in self.portfolio_equity_by_currency:
            return float(self.portfolio_equity_by_currency[currency])
        return self.portfolio.equity_for_currency(currency, self.data)

    def target_value_for_symbol(self, symbol: Symbol) -> float:
        currency = currency_for_symbol(symbol)
        if currency in self.target_portfolio_value_by_currency:
            return float(self.target_portfolio_value_by_currency[currency])
        return self.equity_for_symbol(symbol)

    @property
    def held_symbols(self) -> tuple[Symbol, ...]:
        return self.portfolio.held_symbols


@dataclass(frozen=True, slots=True)
class PortfolioAllocationTarget:
    symbol: Symbol
    target_percent: float
    tag: str = ""

    def __post_init__(self) -> None:
        if not -1.0 <= self.target_percent <= 1.0:
            raise ValueError("target_percent must be between -1 and 1.")


class PortfolioConstructionModel(Protocol):
    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        """Convert active insights into desired portfolio weights."""


@dataclass(frozen=True, slots=True)
class PortfolioTargetPlan:
    target: PortfolioAllocationTarget
    current_quantity: int
    current_price: float | None
    current_value: float
    target_percent: float
    desired_value: float
    source_insight_ids: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_entry(self) -> bool:
        return self.current_quantity == 0 and self.target_percent != 0

    @property
    def is_exit(self) -> bool:
        return self.current_quantity != 0 and self.target_percent == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.target.symbol.key,
            "current_quantity": self.current_quantity,
            "current_price": self.current_price,
            "current_value": self.current_value,
            "target_percent": self.target_percent,
            "desired_value": self.desired_value,
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
    targets: tuple[PortfolioAllocationTarget, ...]
    plans: tuple[PortfolioTargetPlan, ...] = ()
    source_insight_ids: tuple[str, ...] = ()
    model_name: str = ""
    reason: str = ""
    state_patches: tuple[StatePatch, ...] = ()
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
            "state_patch_count": len(self.state_patches),
            "targets": [
                {
                    "symbol": target.symbol.key,
                    "target_percent": target.target_percent,
                    "tag": target.tag,
                }
                for target in self.targets
            ],
            "plans": [plan.to_dict() for plan in self.plans],
            "state_patches": [patch.to_dict() for patch in self.state_patches],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RebalancePolicy:
    cash_reserve_pct: float = 0.0
    min_order_notional: float = 0.0
    min_quantity_delta: int = 1
    allow_exit_below_min_notional: bool = True
    cadence: str = "every_cycle"
    reused_target_churn_guard: bool = False
    reused_target_churn_max_quantity_delta: int = 1
    reused_target_churn_lot_fraction: float = 0.5
    reused_target_churn_equity_bps: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.cash_reserve_pct < 1.0:
            raise ValueError("cash_reserve_pct must be between 0 inclusive and 1 exclusive.")
        if self.min_order_notional < 0:
            raise ValueError("min_order_notional cannot be negative.")
        if self.min_quantity_delta < 0:
            raise ValueError("min_quantity_delta cannot be negative.")
        if not str(self.cadence).strip():
            raise ValueError("cadence cannot be empty.")
        if self.reused_target_churn_max_quantity_delta < 0:
            raise ValueError("reused_target_churn_max_quantity_delta cannot be negative.")
        if self.reused_target_churn_lot_fraction < 0:
            raise ValueError("reused_target_churn_lot_fraction cannot be negative.")
        if self.reused_target_churn_equity_bps < 0:
            raise ValueError("reused_target_churn_equity_bps cannot be negative.")


@dataclass(frozen=True, slots=True)
class PortfolioConstructionEngine:
    model: PortfolioConstructionModel
    rebalance_policy: RebalancePolicy = field(default_factory=RebalancePolicy)
    reason: str = "portfolio_construction"

    def create_targets(self, context: PortfolioConstructionContext) -> PortfolioTargetBatch:
        relevant_symbols = _relevant_symbols(context)
        equity_by_currency = _portfolio_equity_by_currency(context.portfolio, context.data, relevant_symbols)
        target_value_by_currency = {
            currency: equity * (1.0 - self.rebalance_policy.cash_reserve_pct)
            for currency, equity in equity_by_currency.items()
        }
        equity = _single_currency_value(equity_by_currency)
        target_value = _single_currency_value(target_value_by_currency)
        prepared_context = replace(
            context,
            portfolio_equity=equity,
            target_portfolio_value=target_value,
            portfolio_equity_by_currency=MappingProxyType(dict(equity_by_currency)),
            target_portfolio_value_by_currency=MappingProxyType(dict(target_value_by_currency)),
        )
        raw_targets = tuple(
            _coerce_allocation_target(prepared_context, target)
            for target in self.model.create_targets(prepared_context)
        )
        state_patches = _state_patches_for_model(self.model, prepared_context, raw_targets)
        plans = self._build_plans(prepared_context, raw_targets)
        return PortfolioTargetBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.data.time,
            targets=raw_targets,
            plans=plans,
            source_insight_ids=tuple(insight.insight_id for insight in context.active_insights),
            model_name=type(self.model).__name__,
            reason=self.reason,
            state_patches=state_patches,
            metadata={
                "raw_target_count": len(raw_targets),
                "filtered_target_count": len(raw_targets),
                "raw_plan_count": len(plans),
                "filtered_plan_count": len(plans),
                "portfolio_equity": equity,
                "target_portfolio_value": target_value,
                "portfolio_equity_by_currency": dict(equity_by_currency),
                "target_portfolio_value_by_currency": dict(target_value_by_currency),
                "cash_reserve_pct": self.rebalance_policy.cash_reserve_pct,
                "min_order_notional": self.rebalance_policy.min_order_notional,
                "min_quantity_delta": self.rebalance_policy.min_quantity_delta,
                "cadence": self.rebalance_policy.cadence,
                "reused_target_churn_guard": self.rebalance_policy.reused_target_churn_guard,
                "reused_target_churn_max_quantity_delta": self.rebalance_policy.reused_target_churn_max_quantity_delta,
                "reused_target_churn_lot_fraction": self.rebalance_policy.reused_target_churn_lot_fraction,
                "reused_target_churn_equity_bps": self.rebalance_policy.reused_target_churn_equity_bps,
            },
        )

    def _build_plans(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
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


@dataclass(frozen=True, slots=True)
class EqualWeightPortfolioConstructionModel:
    max_portfolio_pct: float = 1.0
    long_only: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_portfolio_pct <= 1.0:
            raise ValueError("max_portfolio_pct must be between 0 and 1.")

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        latest_by_symbol = _latest_insight_by_symbol(context.active_insights)
        actionable_by_currency: dict[str, list[Insight]] = {}
        for insight in latest_by_symbol.values():
            if insight.direction is not InsightDirection.UP and (self.long_only or insight.direction is not InsightDirection.DOWN):
                continue
            actionable_by_currency.setdefault(currency_for_symbol(insight.symbol), []).append(insight)
        target_qty_by_symbol: dict[str, PortfolioTarget] = {}
        for actionable in actionable_by_currency.values():
            equal_weight = self.max_portfolio_pct / len(actionable)
            for insight in actionable:
                signed_weight = _target_weight(insight, equal_weight, long_only=self.long_only)
                target_qty_by_symbol[insight.symbol_key] = PortfolioAllocationTarget(
                    symbol=insight.symbol,
                    target_percent=signed_weight,
                    tag=f"framework:{insight.alpha_id}:{insight.direction.value}",
                )

        for insight in _latest_exit_insights(context.active_insights).values():
            if insight.symbol_key in target_qty_by_symbol:
                continue
            if context.portfolio.quantity(insight.symbol) == 0:
                continue
            target_qty_by_symbol[insight.symbol_key] = PortfolioAllocationTarget(
                symbol=insight.symbol,
                target_percent=0.0,
                tag=f"framework:{insight.alpha_id}:{insight.direction.value}",
            )
        return tuple(target_qty_by_symbol.values())


def _latest_insight_by_symbol(insights: tuple[Insight, ...]) -> dict[str, Insight]:
    latest: dict[str, Insight] = {}
    for insight in insights:
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[insight.symbol_key] = insight
    return latest


def _latest_exit_insights(insights: tuple[Insight, ...]) -> dict[str, Insight]:
    latest: dict[str, Insight] = {}
    for insight in insights:
        if insight.direction not in {InsightDirection.FLAT, InsightDirection.DOWN}:
            continue
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
    if insight.direction is InsightDirection.DOWN and not long_only:
        return -equal_weight
    return equal_weight


def _target_plan(
    context: PortfolioConstructionContext,
    target: PortfolioAllocationTarget,
    *,
    source_insight_ids: tuple[str, ...],
    reason: str,
) -> PortfolioTargetPlan:
    current_quantity = context.portfolio.quantity(target.symbol)
    current_price = context.portfolio.mark_price(target.symbol, context.data)
    current_value = context.portfolio.position_value(target.symbol, context.data)
    desired_value = context.target_value_for_symbol(target.symbol) * target.target_percent
    return PortfolioTargetPlan(
        target=target,
        current_quantity=current_quantity,
        current_price=current_price,
        current_value=current_value,
        target_percent=target.target_percent,
        desired_value=desired_value,
        source_insight_ids=source_insight_ids,
        reason=reason,
    )


def _target_reason(target: PortfolioAllocationTarget) -> str:
    if target.target_percent == 0:
        return "exit"
    return "target"


def _portfolio_equity_by_currency(
    portfolio: Portfolio,
    data: DataSlice,
    symbols: tuple[Symbol, ...],
) -> dict[str, float]:
    currencies = set(portfolio.currencies())
    currencies.update(currency_for_symbol(symbol) for symbol in symbols)
    if not currencies:
        currencies = {"KRW"}
    return portfolio.equity_by_currency(data, currencies)


def _single_currency_value(values: Mapping[str, float]) -> float:
    non_zero = {
        currency: value
        for currency, value in values.items()
        if abs(value) > 1e-12
    }
    if len(non_zero) == 1:
        return next(iter(non_zero.values()))
    return 0.0


def _relevant_symbols(context: PortfolioConstructionContext) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    symbols.extend(insight.symbol for insight in context.active_insights)
    symbols.extend(context.managed_symbols)
    symbols.extend(context.held_symbols)
    return tuple(_symbols_by_key(tuple(symbols)).values())


def _coerce_allocation_target(
    context: PortfolioConstructionContext,
    target: PortfolioAllocationTarget | PortfolioTarget,
) -> PortfolioAllocationTarget:
    if isinstance(target, PortfolioAllocationTarget):
        return target
    if isinstance(target, PortfolioTarget):
        if target.quantity == 0:
            target_percent = 0.0
        else:
            price = context.portfolio.mark_price(target.symbol, context.data)
            target_value = context.target_value_for_symbol(target.symbol)
            target_percent = 0.0 if price is None or target_value <= 0 else (target.quantity * price) / target_value
        return PortfolioAllocationTarget(
            symbol=target.symbol,
            target_percent=target_percent,
            tag=target.tag,
        )
    raise TypeError(f"Unsupported portfolio target type: {type(target).__name__}")


def _state_patches_for_model(
    model: PortfolioConstructionModel,
    context: PortfolioConstructionContext,
    targets: tuple[PortfolioAllocationTarget, ...],
) -> tuple[StatePatch, ...]:
    producer = getattr(model, "state_patches", None)
    if not callable(producer):
        return ()
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(producer).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_kwargs or "context" in parameters:
        kwargs["context"] = context
    if supports_kwargs or "targets" in parameters:
        kwargs["targets"] = targets
    if kwargs:
        result = producer(**kwargs)
    elif not parameters:
        result = producer()
    else:
        result = producer(context, targets)
    patches = tuple(result or ())
    for patch in patches:
        if not isinstance(patch, StatePatch):
            raise TypeError("state_patches(...) must return StatePatch objects.")
    return patches

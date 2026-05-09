from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from leaps_quant_engine.models import DataSlice, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.snapshots import SnapshotQualityReport, SnapshotQualityStatus


class RiskDecisionStatus(str, Enum):
    APPROVED = "approved"
    CLAMPED = "clamped"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    original_target: PortfolioTarget
    approved_target: PortfolioTarget | None
    status: RiskDecisionStatus
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.original_target.symbol.key,
            "original_quantity": self.original_target.quantity,
            "approved_quantity": self.approved_target.quantity if self.approved_target else None,
            "status": self.status.value,
            "reason": self.reason,
            "tag": self.original_target.tag,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RiskDecisionBatch:
    sleeve_id: str
    decisions: tuple[RiskDecision, ...]

    @property
    def approved_targets(self) -> tuple[PortfolioTarget, ...]:
        return tuple(
            decision.approved_target
            for decision in self.decisions
            if decision.approved_target is not None
            and decision.status in {RiskDecisionStatus.APPROVED, RiskDecisionStatus.CLAMPED}
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "sleeve_id": self.sleeve_id,
            "decision_count": len(self.decisions),
            "approved_count": len(self.approved_targets),
            "decisions": [decision.to_dict() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class RiskManagementContext:
    sleeve_id: str
    data: DataSlice
    portfolio: Portfolio
    targets: tuple[PortfolioTarget, ...]
    snapshot_quality: SnapshotQualityReport | None = None


class RiskManagementModel(Protocol):
    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        """Approve, clamp, reject, or add risk-driven targets."""


@dataclass(frozen=True, slots=True)
class PassThroughRiskManagementModel:
    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        return RiskDecisionBatch(
            sleeve_id=context.sleeve_id,
            decisions=tuple(
                RiskDecision(
                    original_target=target,
                    approved_target=target,
                    status=RiskDecisionStatus.APPROVED,
                    reason="pass_through",
                )
                for target in context.targets
            ),
        )


@dataclass(frozen=True, slots=True)
class RiskLimits:
    long_only: bool = True
    max_position_pct: float = 1.0
    max_total_exposure_pct: float = 1.0
    cash_buffer_pct: float = 0.0
    require_fresh_for_entries: bool = True
    reject_invalid_snapshot: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_position_pct <= 1.0:
            raise ValueError("max_position_pct must be between 0 and 1.")
        if not 0.0 <= self.max_total_exposure_pct <= 1.0:
            raise ValueError("max_total_exposure_pct must be between 0 and 1.")
        if not 0.0 <= self.cash_buffer_pct < 1.0:
            raise ValueError("cash_buffer_pct must be between 0 inclusive and 1 exclusive.")


@dataclass(frozen=True, slots=True)
class BasicRiskManagementModel:
    """Small deterministic risk model for v0 framework cycles."""

    limits: RiskLimits = field(default_factory=RiskLimits)

    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        available_cash = max(0.0, context.portfolio.cash * (1.0 - self.limits.cash_buffer_pct))
        decisions: list[RiskDecision] = []
        approved_quantities = {
            holding.symbol.key: holding.quantity
            for holding in context.portfolio.holdings.values()
            if holding.quantity != 0
        }
        for target in context.targets:
            decision, available_cash = self._evaluate_target(
                context,
                target,
                available_cash,
                approved_quantities,
            )
            if decision.approved_target is not None:
                approved_quantities[decision.approved_target.symbol.key] = decision.approved_target.quantity
            decisions.append(decision)
        return RiskDecisionBatch(sleeve_id=context.sleeve_id, decisions=tuple(decisions))

    def _evaluate_target(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        available_cash: float,
        approved_quantities: dict[str, int],
    ) -> tuple[RiskDecision, float]:
        if self.limits.long_only and target.quantity < 0:
            return (
                RiskDecision(
                    original_target=target,
                    approved_target=None,
                    status=RiskDecisionStatus.REJECTED,
                    reason="short_target_rejected",
                    metadata={"long_only": True},
                ),
                available_cash,
            )

        current_quantity = context.portfolio.quantity(target.symbol)
        quality_rejection = self._snapshot_quality_rejection(context, target, current_quantity)
        if quality_rejection is not None:
            return quality_rejection, available_cash

        price = context.portfolio.mark_price(target.symbol, context.data)
        if price is None or price <= 0:
            return (
                RiskDecision(
                    original_target=target,
                    approved_target=None,
                    status=RiskDecisionStatus.REJECTED,
                    reason="missing_or_invalid_price",
                ),
                available_cash,
            )

        bounded_target = self._clamp_position_size(context, target, price)
        bounded_target = self._clamp_total_exposure(context, bounded_target, price, approved_quantities)
        quantity_after_position_limit = bounded_target.quantity
        quantity_after_cash_limit, available_cash = self._clamp_cash(
            current_quantity=current_quantity,
            target_quantity=quantity_after_position_limit,
            price=price,
            available_cash=available_cash,
        )
        if quantity_after_cash_limit == current_quantity and target.quantity != current_quantity:
            return (
                RiskDecision(
                    original_target=target,
                    approved_target=None,
                    status=RiskDecisionStatus.REJECTED,
                    reason="insufficient_cash",
                    metadata={
                        "current_quantity": current_quantity,
                        "requested_quantity": target.quantity,
                        "position_limited_quantity": quantity_after_position_limit,
                        "available_cash": available_cash,
                        "price": price,
                    },
                ),
                available_cash,
            )

        approved_target = PortfolioTarget(
            symbol=target.symbol,
            quantity=quantity_after_cash_limit,
            tag=target.tag,
        )
        status = RiskDecisionStatus.APPROVED if approved_target.quantity == target.quantity else RiskDecisionStatus.CLAMPED
        reason = "approved" if status is RiskDecisionStatus.APPROVED else "risk_limits_clamped"
        return (
            RiskDecision(
                original_target=target,
                approved_target=approved_target,
                status=status,
                reason=reason,
                metadata={
                    "current_quantity": current_quantity,
                    "price": price,
                    "max_position_pct": self.limits.max_position_pct,
                    "max_total_exposure_pct": self.limits.max_total_exposure_pct,
                    "cash_buffer_pct": self.limits.cash_buffer_pct,
                    "available_cash_after": available_cash,
                    "snapshot_quality_status": context.snapshot_quality.status.value
                    if context.snapshot_quality is not None
                    else None,
                },
            ),
            available_cash,
        )

    def _snapshot_quality_rejection(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        current_quantity: int,
    ) -> RiskDecision | None:
        quality = context.snapshot_quality
        if quality is None:
            return None
        if self.limits.reject_invalid_snapshot and quality.status is SnapshotQualityStatus.INVALID:
            return RiskDecision(
                original_target=target,
                approved_target=None,
                status=RiskDecisionStatus.REJECTED,
                reason="snapshot_quality_invalid",
                metadata={"snapshot_quality": quality.to_dict()},
            )
        if self.limits.require_fresh_for_entries and target.quantity > current_quantity and not quality.allows_new_entries:
            return RiskDecision(
                original_target=target,
                approved_target=None,
                status=RiskDecisionStatus.REJECTED,
                reason="snapshot_quality_blocks_entry",
                metadata={"snapshot_quality": quality.to_dict()},
            )
        return None

    def _clamp_position_size(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        price: float,
    ) -> PortfolioTarget:
        if self.limits.max_position_pct >= 1.0:
            return target
        equity = context.portfolio.equity(context.data)
        if equity <= 0:
            return PortfolioTarget(symbol=target.symbol, quantity=0, tag=target.tag)
        max_abs_quantity = int((equity * self.limits.max_position_pct) // price)
        if abs(target.quantity) <= max_abs_quantity:
            return target
        signed_quantity = max_abs_quantity if target.quantity > 0 else -max_abs_quantity
        return PortfolioTarget(symbol=target.symbol, quantity=signed_quantity, tag=target.tag)

    def _clamp_total_exposure(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        price: float,
        approved_quantities: dict[str, int],
    ) -> PortfolioTarget:
        if self.limits.max_total_exposure_pct >= 1.0:
            return target
        equity = context.portfolio.equity(context.data)
        if equity <= 0:
            return PortfolioTarget(symbol=target.symbol, quantity=0, tag=target.tag)
        max_total_exposure = equity * self.limits.max_total_exposure_pct
        exposure_without_target = 0.0
        for holding in context.portfolio.holdings.values():
            if holding.symbol.key == target.symbol.key:
                continue
            quantity = approved_quantities.get(holding.symbol.key, holding.quantity)
            mark = context.portfolio.mark_price(holding.symbol, context.data)
            if mark is None:
                continue
            exposure_without_target += abs(quantity * mark)
        allowed_symbol_exposure = max(0.0, max_total_exposure - exposure_without_target)
        max_abs_quantity = int(allowed_symbol_exposure // price)
        if abs(target.quantity) <= max_abs_quantity:
            return target
        signed_quantity = max_abs_quantity if target.quantity > 0 else -max_abs_quantity
        return PortfolioTarget(symbol=target.symbol, quantity=signed_quantity, tag=target.tag)

    def _clamp_cash(
        self,
        *,
        current_quantity: int,
        target_quantity: int,
        price: float,
        available_cash: float,
    ) -> tuple[int, float]:
        delta = target_quantity - current_quantity
        if delta <= 0:
            return target_quantity, available_cash
        affordable_delta = int(available_cash // price)
        if affordable_delta >= delta:
            return target_quantity, available_cash - (delta * price)
        return current_quantity + affordable_delta, available_cash - (affordable_delta * price)

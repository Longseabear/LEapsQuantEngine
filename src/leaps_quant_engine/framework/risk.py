from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from leaps_quant_engine.models import DataSlice, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio


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

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.original_target.symbol.key,
            "original_quantity": self.original_target.quantity,
            "approved_quantity": self.approved_target.quantity if self.approved_target else None,
            "status": self.status.value,
            "reason": self.reason,
            "tag": self.original_target.tag,
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


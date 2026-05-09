from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightBatch, InsightManager, InsightManagerUpdate, SnapshotContext
from leaps_quant_engine.execution import ImmediateExecutionModel
from leaps_quant_engine.framework.portfolio_construction import (
    EqualWeightPortfolioConstructionModel,
    PortfolioConstructionEngine,
    PortfolioConstructionContext,
    PortfolioConstructionModel,
    PortfolioTargetBatch,
)
from leaps_quant_engine.framework.risk import (
    BasicRiskManagementModel,
    RiskDecisionBatch,
    RiskManagementContext,
    RiskManagementModel,
)
from leaps_quant_engine.models import DataSlice, OrderIntent, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.snapshots import IndicatorSnapshot


@dataclass(frozen=True, slots=True)
class StageTiming:
    alpha_ms: float
    insight_manager_ms: float
    portfolio_ms: float
    risk_ms: float
    execution_ms: float

    @property
    def total_ms(self) -> float:
        return self.alpha_ms + self.insight_manager_ms + self.portfolio_ms + self.risk_ms + self.execution_ms

    def to_dict(self) -> dict[str, float]:
        return {
            "alpha_ms": self.alpha_ms,
            "insight_manager_ms": self.insight_manager_ms,
            "portfolio_ms": self.portfolio_ms,
            "risk_ms": self.risk_ms,
            "execution_ms": self.execution_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True, slots=True)
class FrameworkCycleResult:
    sleeve_id: str
    source_snapshot_id: str | None
    indicator_snapshot_id: str
    new_insight_batch: InsightBatch
    insight_manager_update: InsightManagerUpdate
    active_insights: tuple[Insight, ...]
    portfolio_target_batch: PortfolioTargetBatch
    risk_decisions: RiskDecisionBatch
    order_intents: tuple[OrderIntent, ...]
    timings: StageTiming

    @property
    def active_insight_count(self) -> int:
        return len(self.active_insights)

    @property
    def portfolio_targets(self) -> tuple[PortfolioTarget, ...]:
        return self.portfolio_target_batch.targets

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        new_insights = self.new_insight_batch.to_dict() if include_details else {
            "batch_id": self.new_insight_batch.batch_id,
            "sleeve_id": self.new_insight_batch.sleeve_id,
            "universe_id": self.new_insight_batch.universe_id,
            "source_snapshot_id": self.new_insight_batch.source_snapshot_id,
            "generated_at": self.new_insight_batch.generated_at.isoformat(),
            "alpha_ids": list(self.new_insight_batch.alpha_ids),
            "insight_count": self.new_insight_batch.insight_count,
        }
        manager_update = self.insight_manager_update.to_dict() if include_details else {
            "added_count": self.insight_manager_update.added_count,
            "expired_count": self.insight_manager_update.expired_count,
            "cancelled_count": self.insight_manager_update.cancelled_count,
            "superseded_count": self.insight_manager_update.superseded_count,
        }
        return {
            "sleeve_id": self.sleeve_id,
            "source_snapshot_id": self.source_snapshot_id,
            "indicator_snapshot_id": self.indicator_snapshot_id,
            "new_insights": new_insights,
            "insight_manager_update": manager_update,
            "active_insight_count": self.active_insight_count,
            "active_insights": [insight.to_dict() for insight in self.active_insights] if include_details else [],
            "portfolio_target_batch": self.portfolio_target_batch.to_dict() if include_details else {
                "batch_id": self.portfolio_target_batch.batch_id,
                "sleeve_id": self.portfolio_target_batch.sleeve_id,
                "generated_at": self.portfolio_target_batch.generated_at.isoformat(),
                "model_name": self.portfolio_target_batch.model_name,
                "reason": self.portfolio_target_batch.reason,
                "source_insight_ids": list(self.portfolio_target_batch.source_insight_ids),
                "target_count": self.portfolio_target_batch.target_count,
                "metadata": dict(self.portfolio_target_batch.metadata),
            },
            "portfolio_targets": [
                {
                    "symbol": target.symbol.key,
                    "quantity": target.quantity,
                    "tag": target.tag,
                }
                for target in self.portfolio_targets
            ],
            "risk": self.risk_decisions.to_dict(),
            "order_intents": [
                {
                    "sleeve_id": order.sleeve_id,
                    "symbol": order.symbol.key,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "reference_price": order.reference_price,
                    "tag": order.tag,
                }
                for order in self.order_intents
            ],
            "timings": self.timings.to_dict(),
        }


@dataclass(slots=True)
class FrameworkRunner:
    sleeve_id: str
    alpha_runtime: AlphaRuntime
    insight_manager: InsightManager = None  # type: ignore[assignment]
    portfolio_model: PortfolioConstructionModel = None  # type: ignore[assignment]
    portfolio_engine: PortfolioConstructionEngine = None  # type: ignore[assignment]
    risk_model: RiskManagementModel = None  # type: ignore[assignment]
    execution_model: ImmediateExecutionModel = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.insight_manager is None:
            self.insight_manager = InsightManager()
        if self.portfolio_model is None:
            self.portfolio_model = EqualWeightPortfolioConstructionModel()
        if self.portfolio_engine is None:
            self.portfolio_engine = PortfolioConstructionEngine(model=self.portfolio_model)
        if self.risk_model is None:
            self.risk_model = BasicRiskManagementModel()
        if self.execution_model is None:
            self.execution_model = ImmediateExecutionModel()

    def run_once(
        self,
        *,
        indicator_snapshot: IndicatorSnapshot,
        data: DataSlice,
        portfolio: Portfolio,
    ) -> FrameworkCycleResult:
        context = SnapshotContext.from_indicator_snapshot(indicator_snapshot)

        started = time.perf_counter()
        insight_batch = self.alpha_runtime.run(context)
        alpha_ms = _elapsed_ms(started)

        started = time.perf_counter()
        manager_update = self.insight_manager.ingest(insight_batch, as_of=context.as_of)
        active_insights = self.insight_manager.active(context.as_of, sleeve_id=self.sleeve_id)
        insight_manager_ms = _elapsed_ms(started)

        started = time.perf_counter()
        portfolio_target_batch = self.portfolio_engine.create_targets(
            PortfolioConstructionContext(
                sleeve_id=self.sleeve_id,
                data=data,
                portfolio=portfolio,
                active_insights=active_insights,
                managed_symbols=self.insight_manager.tracked_symbols(self.sleeve_id),
            )
        )
        portfolio_ms = _elapsed_ms(started)

        started = time.perf_counter()
        risk_decisions = self.risk_model.manage_risk(
            RiskManagementContext(
                sleeve_id=self.sleeve_id,
                data=data,
                portfolio=portfolio,
                targets=portfolio_target_batch.targets,
            )
        )
        risk_ms = _elapsed_ms(started)

        started = time.perf_counter()
        orders = tuple(
            self.execution_model.create_orders(
                self.sleeve_id,
                portfolio,
                data,
                list(risk_decisions.approved_targets),
            )
        )
        execution_ms = _elapsed_ms(started)

        return FrameworkCycleResult(
            sleeve_id=self.sleeve_id,
            source_snapshot_id=indicator_snapshot.source_snapshot_id,
            indicator_snapshot_id=indicator_snapshot.snapshot_id,
            new_insight_batch=insight_batch,
            insight_manager_update=manager_update,
            active_insights=active_insights,
            portfolio_target_batch=portfolio_target_batch,
            risk_decisions=risk_decisions,
            order_intents=orders,
            timings=StageTiming(
                alpha_ms=alpha_ms,
                insight_manager_ms=insight_manager_ms,
                portfolio_ms=portfolio_ms,
                risk_ms=risk_ms,
                execution_ms=execution_ms,
            ),
        )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000

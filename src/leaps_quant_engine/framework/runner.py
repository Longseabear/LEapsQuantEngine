from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import time
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightBatch, InsightManager, InsightManagerUpdate, SnapshotContext
from leaps_quant_engine.cadence import cadence_due, normalize_cadence
from leaps_quant_engine.execution import ExecutionContext, ExecutionEngine, ImmediateExecutionModel, OrderIntentBatch
from leaps_quant_engine.framework.portfolio_construction import (
    EqualWeightPortfolioConstructionModel,
    PortfolioConstructionEngine,
    PortfolioConstructionContext,
    PortfolioConstructionModel,
    PortfolioTargetBatch,
)
from leaps_quant_engine.framework.portfolio_blend import PortfolioBlendEngine
from leaps_quant_engine.framework.order_sizing import OrderSizingBatch, OrderSizingContext, OrderSizingEngine
from leaps_quant_engine.framework.portfolio_target_resolver import PortfolioTargetResolver
from leaps_quant_engine.framework.state import FrameworkRunnerState
from leaps_quant_engine.framework.risk import (
    BasicRiskManagementModel,
    RiskDecisionBatch,
    RiskManagementContext,
    RiskManagementModel,
)
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import DataSlice, OrderIntent, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.fundamentals import FundamentalSnapshot
from leaps_quant_engine.runtime_state import ModelStateEvent, RuntimeModelStateView, RuntimeStateStore, StatePatch
from leaps_quant_engine.snapshots import IndicatorSnapshot, SnapshotQualityStatus


@dataclass(frozen=True, slots=True)
class StageTiming:
    alpha_ms: float
    insight_manager_ms: float
    portfolio_ms: float
    order_sizing_ms: float
    risk_ms: float
    execution_ms: float

    @property
    def total_ms(self) -> float:
        return (
            self.alpha_ms
            + self.insight_manager_ms
            + self.portfolio_ms
            + self.order_sizing_ms
            + self.risk_ms
            + self.execution_ms
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "alpha_ms": self.alpha_ms,
            "insight_manager_ms": self.insight_manager_ms,
            "portfolio_ms": self.portfolio_ms,
            "order_sizing_ms": self.order_sizing_ms,
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
    order_sizing_batch: OrderSizingBatch
    risk_decisions: RiskDecisionBatch
    execution_batch: OrderIntentBatch
    order_intents: tuple[OrderIntent, ...]
    timings: StageTiming
    state_patches: tuple[StatePatch, ...] = ()
    state_events: tuple[ModelStateEvent, ...] = ()
    state_commit_enabled: bool = False
    stage_decisions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_decisions", MappingProxyType(dict(self.stage_decisions)))

    @property
    def active_insight_count(self) -> int:
        return len(self.active_insights)

    @property
    def portfolio_targets(self) -> tuple[PortfolioTarget, ...]:
        return self.order_sizing_batch.targets

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        new_insights = self.new_insight_batch.to_dict() if include_details else {
            "batch_id": self.new_insight_batch.batch_id,
            "sleeve_id": self.new_insight_batch.sleeve_id,
            "universe_id": self.new_insight_batch.universe_id,
            "source_snapshot_id": self.new_insight_batch.source_snapshot_id,
            "generated_at": self.new_insight_batch.generated_at.isoformat(),
            "alpha_ids": list(self.new_insight_batch.alpha_ids),
            "insight_count": self.new_insight_batch.insight_count,
            "state_patch_count": len(self.new_insight_batch.state_patches),
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
                "state_patch_count": len(self.portfolio_target_batch.state_patches),
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
            "order_sizing": self.order_sizing_batch.to_dict() if include_details else {
                "batch_id": self.order_sizing_batch.batch_id,
                "source_batch_id": self.order_sizing_batch.source_batch_id,
                "sleeve_id": self.order_sizing_batch.sleeve_id,
                "generated_at": self.order_sizing_batch.generated_at.isoformat(),
                "model_name": self.order_sizing_batch.model_name,
                "reason": self.order_sizing_batch.reason,
                "target_count": self.order_sizing_batch.target_count,
                "metadata": dict(self.order_sizing_batch.metadata),
            },
            "risk": self.risk_decisions.to_dict(),
            "execution": self.execution_batch.to_dict() if include_details else {
                "batch_id": self.execution_batch.batch_id,
                "sleeve_id": self.execution_batch.sleeve_id,
                "generated_at": self.execution_batch.generated_at.isoformat(),
                "model_name": self.execution_batch.model_name,
                "reason": self.execution_batch.reason,
                "order_count": self.execution_batch.order_count,
                "state_patch_count": len(self.execution_batch.state_patches),
                "metadata": dict(self.execution_batch.metadata),
            },
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
            "model_state": {
                "patch_count": len(self.state_patches),
                "event_count": len(self.state_events),
                "commit_enabled": self.state_commit_enabled,
                "patches": [patch.to_dict() for patch in self.state_patches] if include_details else [],
                "events": [event.to_dict() for event in self.state_events] if include_details else [],
            },
            "timings": self.timings.to_dict(),
            "stage_decisions": dict(self.stage_decisions),
        }


@dataclass(slots=True)
class FrameworkRunner:
    sleeve_id: str
    alpha_runtime: AlphaRuntime
    insight_manager: InsightManager = None  # type: ignore[assignment]
    portfolio_model: PortfolioConstructionModel = None  # type: ignore[assignment]
    portfolio_engine: PortfolioConstructionEngine = None  # type: ignore[assignment]
    portfolio_target_resolver: PortfolioTargetResolver = None  # type: ignore[assignment]
    portfolio_blend_engine: PortfolioBlendEngine | None = None
    risk_model: RiskManagementModel = None  # type: ignore[assignment]
    order_sizing_engine: OrderSizingEngine = None  # type: ignore[assignment]
    execution_model: ImmediateExecutionModel = None  # type: ignore[assignment]
    execution_engine: ExecutionEngine = None  # type: ignore[assignment]
    runtime_state_store: RuntimeStateStore | None = None
    runtime_state_commit_enabled: bool = True
    _last_portfolio_run_at: datetime | None = field(default=None, init=False)
    _last_portfolio_target_batch: PortfolioTargetBatch | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.insight_manager is None:
            self.insight_manager = InsightManager()
        if self.portfolio_model is None:
            self.portfolio_model = EqualWeightPortfolioConstructionModel()
        if self.portfolio_engine is None:
            self.portfolio_engine = PortfolioConstructionEngine(model=self.portfolio_model)
        if self.portfolio_target_resolver is None:
            self.portfolio_target_resolver = PortfolioTargetResolver()
        if self.risk_model is None:
            self.risk_model = BasicRiskManagementModel()
        if self.order_sizing_engine is None:
            self.order_sizing_engine = OrderSizingEngine(rebalance_policy=self.portfolio_engine.rebalance_policy)
        if self.execution_model is None:
            self.execution_model = ImmediateExecutionModel()
        if self.execution_engine is None:
            self.execution_engine = ExecutionEngine(model=self.execution_model)

    def restore_state(self, state: FrameworkRunnerState | None) -> None:
        if state is None:
            return
        if state.sleeve_id and state.sleeve_id != self.sleeve_id:
            return
        self._last_portfolio_run_at = state.last_portfolio_run_at
        self._last_portfolio_target_batch = state.last_portfolio_target_batch
        self.alpha_runtime.restore_last_run_state(state.alpha_last_run_by_alpha_id)
        self.insight_manager = InsightManager()
        if state.active_insights:
            self.insight_manager.ingest(
                InsightBatch(
                    sleeve_id=self.sleeve_id,
                    universe_id=None,
                    source_snapshot_id=None,
                    generated_at=state.updated_at,
                    alpha_ids=tuple(sorted({insight.alpha_id for insight in state.active_insights})),
                    insights=state.active_insights,
                    metadata={"restored_from_framework_state": True},
                ),
                as_of=state.updated_at,
            )

    def export_state(self, *, as_of: datetime | None = None) -> FrameworkRunnerState:
        state_time = as_of or datetime.now()
        return FrameworkRunnerState(
            sleeve_id=self.sleeve_id,
            updated_at=state_time,
            active_insights=self.insight_manager.active(state_time, sleeve_id=self.sleeve_id),
            alpha_last_run_by_alpha_id=self.alpha_runtime.last_run_state(),
            last_portfolio_run_at=self._last_portfolio_run_at,
            last_portfolio_target_batch=self._last_portfolio_target_batch,
        )

    def run_once(
        self,
        *,
        indicator_snapshot: IndicatorSnapshot,
        fundamental_snapshot: FundamentalSnapshot | None = None,
        data: DataSlice,
        portfolio: Portfolio,
        alpha_symbols_by_model: Mapping[str, Iterable[Symbol | str]] | None = None,
        market_session: MarketSession | None = None,
        market_sessions: Mapping[str, MarketSession] | None = None,
    ) -> FrameworkCycleResult:
        context = SnapshotContext.from_indicator_snapshot(
            indicator_snapshot,
            fundamental_snapshot=fundamental_snapshot,
            model_state=self._model_state_view(),
        )

        started = time.perf_counter()
        invalid_snapshot = _snapshot_quality_invalid(context)
        if invalid_snapshot:
            insight_batch = InsightBatch(
                sleeve_id=context.sleeve_id,
                universe_id=context.universe_id,
                source_snapshot_id=context.source_snapshot_id,
                generated_at=context.as_of,
                alpha_ids=self.alpha_runtime.active_alpha_ids(),
                insights=(),
                metadata={
                    "ran_alpha_ids": [],
                    "skipped_alpha_ids": list(self.alpha_runtime.active_alpha_ids()),
                    "cadence_by_alpha": {},
                    "state_patch_count": 0,
                    "skipped_reason": "snapshot_quality_invalid",
                    "data_quality_rejected_insight_count": 0,
                },
            )
        else:
            insight_batch = self.alpha_runtime.run(context, symbols_by_alpha=alpha_symbols_by_model)
        alpha_ms = _elapsed_ms(started)
        stage_decisions: dict[str, Any] = {
            "alpha": dict(insight_batch.metadata),
        }

        started = time.perf_counter()
        if invalid_snapshot:
            manager_update = self.insight_manager.expire(context.as_of)
            suppressed_active_insights = self.insight_manager.active(context.as_of, sleeve_id=self.sleeve_id)
            active_insights = ()
        else:
            manager_update = self.insight_manager.ingest(insight_batch, as_of=context.as_of)
            suppressed_active_insights = ()
            active_insights = self.insight_manager.active(context.as_of, sleeve_id=self.sleeve_id)
        insight_manager_ms = _elapsed_ms(started)
        if invalid_snapshot:
            stage_decisions["alpha"]["data_quality_suppressed_active_insight_count"] = len(suppressed_active_insights)

        started = time.perf_counter()
        portfolio_cadence = normalize_cadence(self.portfolio_engine.rebalance_policy.cadence)
        portfolio_context = PortfolioConstructionContext(
            sleeve_id=self.sleeve_id,
            data=data,
            portfolio=portfolio,
            active_insights=active_insights,
            managed_symbols=self.insight_manager.tracked_symbols(self.sleeve_id),
            model_state=self._model_state_view(),
        )
        if invalid_snapshot:
            portfolio_should_run = False
            portfolio_target_batch = PortfolioTargetBatch(
                sleeve_id=self.sleeve_id,
                generated_at=data.time,
                targets=(),
                model_name=type(self.portfolio_model).__name__,
                reason="snapshot_quality_invalid",
                metadata={"portfolio_skipped_reason": "snapshot_quality_invalid"},
            )
        else:
            portfolio_should_run = self._last_portfolio_target_batch is None or cadence_due(
                portfolio_cadence,
                data.time,
                self._last_portfolio_run_at,
            )
        if not invalid_snapshot and portfolio_should_run:
            raw_portfolio_target_batch = self.portfolio_engine.create_targets(portfolio_context)
            resolved_portfolio_target_batch = self._resolve_portfolio_targets(
                portfolio_context,
                raw_portfolio_target_batch,
                previous_batch=self._last_portfolio_target_batch,
            )
            portfolio_target_batch = self._apply_portfolio_blend(
                portfolio_context,
                resolved_portfolio_target_batch,
                previous_batch=self._last_portfolio_target_batch,
                market_session=market_session,
            )
            self._last_portfolio_target_batch = portfolio_target_batch
            self._last_portfolio_run_at = data.time
        elif not invalid_snapshot:
            reused_batch = _reuse_portfolio_target_batch(self._last_portfolio_target_batch, data.time)
            portfolio_target_batch = self._advance_portfolio_blend(
                portfolio_context,
                reused_batch,
                previous_batch=self._last_portfolio_target_batch,
                market_session=market_session,
            )
            self._last_portfolio_target_batch = portfolio_target_batch
        portfolio_ms = _elapsed_ms(started)
        stage_decisions["portfolio"] = {
            "cadence": portfolio_cadence,
            "ran": portfolio_should_run,
            "last_run_at": self._last_portfolio_run_at.isoformat() if self._last_portfolio_run_at else None,
            "reused_batch_id": (
                portfolio_target_batch.metadata.get("source_batch_id")
                if not portfolio_should_run
                else None
            ),
            "portfolio_blend": dict(portfolio_target_batch.metadata.get("portfolio_blend") or {}),
            "target_resolution": dict(portfolio_target_batch.metadata.get("portfolio_target_resolution") or {}),
        }

        started = time.perf_counter()
        order_sizing_batch = self.order_sizing_engine.size(
            OrderSizingContext(
                sleeve_id=self.sleeve_id,
                data=data,
                portfolio=portfolio,
                portfolio_targets=portfolio_target_batch,
            )
        )
        order_sizing_ms = _elapsed_ms(started)

        started = time.perf_counter()
        risk_decisions = self.risk_model.manage_risk(
            RiskManagementContext(
                sleeve_id=self.sleeve_id,
                data=data,
                portfolio=portfolio,
                targets=order_sizing_batch.targets,
                snapshot_quality=indicator_snapshot.quality_report,
                active_insights=active_insights,
                model_state=self._model_state_view(),
            )
        )
        risk_ms = _elapsed_ms(started)

        started = time.perf_counter()
        execution_batch = self.execution_engine.execute(
            ExecutionContext(
                sleeve_id=self.sleeve_id,
                generated_at=data.time,
                portfolio=portfolio,
                data=data,
                approved_targets=risk_decisions.approved_targets,
                market_session=market_session,
                market_sessions=dict(market_sessions or {}),
                model_state=self._model_state_view(),
            )
        )
        orders = execution_batch.order_intents
        execution_ms = _elapsed_ms(started)
        stage_decisions["execution"] = {
            "market_session": market_session.to_dict() if market_session is not None else None,
            "market_sessions": {
                scope: session.to_dict()
                for scope, session in sorted(dict(market_sessions or {}).items())
            },
        }
        state_patches = (
            *insight_batch.state_patches,
            *portfolio_target_batch.state_patches,
            *risk_decisions.state_patches,
            *execution_batch.state_patches,
        )
        state_events = self._commit_state_patches(state_patches, applied_at=data.time)
        stage_decisions["model_state"] = {
            "patch_count": len(state_patches),
            "event_count": len(state_events),
            "commit_enabled": self.runtime_state_store is not None and self.runtime_state_commit_enabled,
        }

        return FrameworkCycleResult(
            sleeve_id=self.sleeve_id,
            source_snapshot_id=indicator_snapshot.source_snapshot_id,
            indicator_snapshot_id=indicator_snapshot.snapshot_id,
            new_insight_batch=insight_batch,
            insight_manager_update=manager_update,
            active_insights=active_insights,
            portfolio_target_batch=portfolio_target_batch,
            order_sizing_batch=order_sizing_batch,
            risk_decisions=risk_decisions,
            execution_batch=execution_batch,
            order_intents=orders,
            timings=StageTiming(
                alpha_ms=alpha_ms,
                insight_manager_ms=insight_manager_ms,
                portfolio_ms=portfolio_ms,
                order_sizing_ms=order_sizing_ms,
                risk_ms=risk_ms,
                execution_ms=execution_ms,
            ),
            state_patches=state_patches,
            state_events=state_events,
            state_commit_enabled=self.runtime_state_store is not None and self.runtime_state_commit_enabled,
            stage_decisions=stage_decisions,
        )

    def _model_state_view(self) -> RuntimeModelStateView:
        return RuntimeModelStateView(store=self.runtime_state_store, default_sleeve_id=self.sleeve_id)

    def _apply_portfolio_blend(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None,
        market_session: MarketSession | None,
    ) -> PortfolioTargetBatch:
        if self.portfolio_blend_engine is None:
            return raw_batch
        decision = self.portfolio_blend_engine.apply(
            context,
            raw_batch,
            previous_batch=previous_batch,
            market_session=market_session,
        )
        if decision.targets == raw_batch.targets and not decision.state_patches:
            if not decision.metadata:
                return raw_batch
        return self.portfolio_engine.build_target_batch_from_targets(
            context,
            raw_batch,
            decision.targets,
            reason=decision.reason or raw_batch.reason,
            state_patches=(*raw_batch.state_patches, *decision.state_patches),
            metadata=decision.metadata,
        )

    def _resolve_portfolio_targets(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None,
    ) -> PortfolioTargetBatch:
        decision = self.portfolio_target_resolver.resolve(
            context,
            raw_batch,
            previous_batch=previous_batch,
        )
        return self.portfolio_engine.build_target_batch_from_targets(
            context,
            raw_batch,
            decision.targets,
            reason=decision.reason or raw_batch.reason,
            state_patches=raw_batch.state_patches,
            metadata=decision.metadata,
        )

    def _advance_portfolio_blend(
        self,
        context: PortfolioConstructionContext,
        reused_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None,
        market_session: MarketSession | None,
    ) -> PortfolioTargetBatch:
        if self.portfolio_blend_engine is None:
            return reused_batch
        decision = self.portfolio_blend_engine.advance(
            context,
            reused_batch,
            previous_batch=previous_batch,
            market_session=market_session,
        )
        if decision.targets == reused_batch.targets and not decision.state_patches:
            if not decision.metadata:
                return reused_batch
        return self.portfolio_engine.build_target_batch_from_targets(
            context,
            reused_batch,
            decision.targets,
            reason=decision.reason or reused_batch.reason,
            state_patches=decision.state_patches,
            metadata=decision.metadata,
        )

    def _commit_state_patches(
        self,
        patches: tuple[StatePatch, ...],
        *,
        applied_at: datetime,
    ) -> tuple[ModelStateEvent, ...]:
        if not patches or self.runtime_state_store is None or not self.runtime_state_commit_enabled:
            return ()
        return self.runtime_state_store.apply_patches(patches, applied_at=applied_at)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _snapshot_quality_invalid(context: SnapshotContext) -> bool:
    return (
        context.quality_report is not None
        and context.quality_report.status is SnapshotQualityStatus.INVALID
    )


def _reuse_portfolio_target_batch(batch: PortfolioTargetBatch, generated_at: datetime) -> PortfolioTargetBatch:
    metadata = dict(batch.metadata)
    metadata.update(
        {
            "reused": True,
            "source_batch_id": batch.batch_id,
            "source_generated_at": batch.generated_at.isoformat(),
        }
    )
    return replace(
        batch,
        generated_at=generated_at,
        reason=f"{batch.reason}:reused" if batch.reason else "portfolio_reused",
        metadata=metadata,
        state_patches=(),
    )

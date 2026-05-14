from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from importlib import import_module
import importlib.util
import inspect
import json
import logging
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Callable, Iterable, Mapping

from leaps_quant_engine.adapters.finance_datareader import FinanceDataReaderMarketDataProvider
from leaps_quant_engine.adapters.kis import KISCachedMarketDataProvider, MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader
from leaps_quant_engine.broker_routing import market_scope_for_symbol, market_scope_from_market
from leaps_quant_engine.execution import ExecutionEngine
from leaps_quant_engine.execution_model_loader import PythonExecutionModelLoader
from leaps_quant_engine.framework import (
    FrameworkCycleResult,
    FrameworkRunner,
    PortfolioBlendEngine,
    PortfolioBlendPolicy,
    PortfolioConstructionEngine,
    PythonPortfolioConstructionModelLoader,
    PythonRiskManagementModelLoader,
    RebalancePolicy,
)
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_rules import MarketSession, synthetic_domestic_market_session, synthetic_us_market_session
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio, PortfolioProvider, StaticPortfolioProvider
from leaps_quant_engine.portfolio_state import PortfolioEngineState
from leaps_quant_engine.runtime_state import RuntimeStateStore
from leaps_quant_engine.runtime_config import ActiveUniverseRuntimeConfig, ModuleReference, RuntimeConfigSnapshot, SleeveRuntimeConfig
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker, SnapshotWorkerRunReport
from leaps_quant_engine.snapshots import (
    IndicatorSnapshot,
    IndicatorSnapshotStore,
    SnapshotQualityReport,
    SnapshotQualityStatus,
)
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.fine import FineUniverseRefreshReport, FineUniverseRuntime
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.universe.runtime import ActiveUniverseResult, CompositeUniverseSelectionRuntime, UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import UniverseSelectionModel
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore
from leaps_quant_engine.warmup import WarmupPolicy, WarmupReport, run_daily_indicator_warmup


class RuntimeBootstrapError(RuntimeError):
    """Raised when a runtime config snapshot cannot be converted into executable objects."""


agent_status_logger = logging.getLogger("leaps_quant_engine.agent_status")

LiveProviderFactory = Callable[[UniverseDefinition, int | None], MarketDataProvider]
HistoryProviderFactory = Callable[[], MarketDataProvider]
UniverseLoader = Callable[[str | Path], UniverseDefinition]


@dataclass(frozen=True, slots=True)
class RuntimeBootstrapDependencies:
    load_universe: UniverseLoader = load_universe_definition
    live_provider_factory: LiveProviderFactory = None  # type: ignore[assignment]
    history_provider_factory: HistoryProviderFactory = None  # type: ignore[assignment]
    alpha_loader: PythonAlphaLoader = PythonAlphaLoader()
    portfolio_model_loader: PythonPortfolioConstructionModelLoader = PythonPortfolioConstructionModelLoader()
    risk_model_loader: PythonRiskManagementModelLoader = PythonRiskManagementModelLoader()
    execution_model_loader: PythonExecutionModelLoader = PythonExecutionModelLoader()
    portfolio_provider: PortfolioProvider | None = None
    indicator_engine: IndicatorEngine | None = None
    indicator_snapshot_stores: dict[str, IndicatorSnapshotStore] | None = None
    runtime_state_store: RuntimeStateStore | None = None
    runtime_state_commit_enabled: bool = True

    def __post_init__(self) -> None:
        if self.live_provider_factory is None:
            object.__setattr__(self, "live_provider_factory", _default_live_provider_factory)
        if self.history_provider_factory is None:
            object.__setattr__(self, "history_provider_factory", _default_history_provider_factory)


@dataclass(slots=True)
class RuntimeSleeveRuntime:
    snapshot: RuntimeConfigSnapshot
    sleeve_config: SleeveRuntimeConfig
    coarse_universe: UniverseDefinition
    selection_model: UniverseSelectionModel
    selection_models: tuple[UniverseSelectionModel, ...]
    live_provider: MarketDataProvider
    history_provider: MarketDataProvider
    alpha_runtime: AlphaRuntime
    framework_runner: FrameworkRunner
    portfolio_provider: PortfolioProvider
    portfolio: Portfolio
    worker: BackgroundSnapshotWorker
    active_result: ActiveUniverseResult
    selection_warmup_report: WarmupReport | None = None
    selection_indicator_snapshot: IndicatorSnapshot | None = None
    fine_runtime: FineUniverseRuntime | None = None
    fine_refresh_report: FineUniverseRefreshReport | None = None
    _pending_reload: RuntimeSleeveRuntime | None = field(default=None, init=False, repr=False)

    @property
    def runtime_id(self) -> str:
        return self.snapshot.config.runtime_id

    @property
    def config_version(self) -> str:
        return self.snapshot.version

    @property
    def sleeve_id(self) -> str:
        return self.sleeve_config.sleeve_id

    def run_once(self, *, warmup: bool | None = None) -> "RuntimeRunOnceReport":
        run_report = self.worker.run(
            max_cycles=1,
            warmup=self.sleeve_config.indicators.warmup_enabled if warmup is None else warmup,
            refresh_history=self.sleeve_config.indicators.refresh_history,
        )
        return self.build_run_once_report(run_report)

    def build_run_once_report(self, worker_report: SnapshotWorkerRunReport) -> "RuntimeRunOnceReport":
        framework_result = self._run_framework_once()
        portfolio_state = self._portfolio_engine_state(framework_result)
        report = RuntimeRunOnceReport(
            runtime_id=self.runtime_id,
            config_version=self.config_version,
            sleeve_id=self.sleeve_id,
            coarse_universe_id=self.coarse_universe.id,
            active_universe_id=self.active_result.active_universe.id,
            fine_refresh_report=self.fine_refresh_report,
            active_result=self.active_result,
            selection_warmup_report=self.selection_warmup_report,
            worker=worker_report,
            framework=framework_result,
            portfolio_state=portfolio_state,
        )
        status = self._agent_status(report)
        report = replace(report, agent_status=status)
        agent_status_logger.info(
            "engine_status %s",
            json.dumps(status, ensure_ascii=False, separators=(",", ":")),
            extra={"engine_status": status},
        )
        return report

    def stage_reload(
        self,
        snapshot: RuntimeConfigSnapshot,
        *,
        dependencies: RuntimeBootstrapDependencies | None = None,
        refresh_fine: bool = False,
    ) -> "RuntimeSleeveReloadReport":
        pending = bootstrap_sleeve_runtime(
            snapshot,
            self.sleeve_id,
            dependencies=dependencies,
            refresh_fine=refresh_fine,
            previous_live_symbols=tuple(self.active_result.active_universe.symbols),
            held_symbols=self.portfolio.held_symbols,
        )
        dry_run = self._dry_run_pending_reload(pending)
        self._pending_reload = pending
        return RuntimeSleeveReloadReport(
            sleeve_id=self.sleeve_id,
            previous_version=self.config_version,
            staged_version=snapshot.version,
            dry_run_framework_ran=dry_run is not None,
            dry_run_order_intent_count=len(dry_run.order_intents) if dry_run is not None else 0,
            activated=False,
        )

    def activate_staged_reload(self) -> "RuntimeSleeveReloadReport":
        pending = self._pending_reload
        if pending is None:
            return RuntimeSleeveReloadReport(
                sleeve_id=self.sleeve_id,
                previous_version=self.config_version,
                staged_version=self.config_version,
                dry_run_framework_ran=False,
                dry_run_order_intent_count=0,
                activated=False,
                reason="no_staged_reload",
            )
        previous_version = self.config_version
        self.snapshot = pending.snapshot
        self.sleeve_config = pending.sleeve_config
        self.coarse_universe = pending.coarse_universe
        self.selection_model = pending.selection_model
        self.selection_models = pending.selection_models
        self.live_provider = pending.live_provider
        self.history_provider = pending.history_provider
        self.alpha_runtime = pending.alpha_runtime
        self.framework_runner = pending.framework_runner
        self.portfolio_provider = pending.portfolio_provider
        self.portfolio = pending.portfolio
        self.worker = pending.worker
        self.active_result = pending.active_result
        self.selection_warmup_report = pending.selection_warmup_report
        self.selection_indicator_snapshot = pending.selection_indicator_snapshot
        self.fine_runtime = pending.fine_runtime
        self.fine_refresh_report = pending.fine_refresh_report
        self._pending_reload = None
        return RuntimeSleeveReloadReport(
            sleeve_id=self.sleeve_id,
            previous_version=previous_version,
            staged_version=self.config_version,
            dry_run_framework_ran=False,
            dry_run_order_intent_count=0,
            activated=True,
        )

    def _dry_run_pending_reload(self, pending: "RuntimeSleeveRuntime") -> FrameworkCycleResult | None:
        active_snapshot_store = self.worker.stores_by_sleeve.get(self.sleeve_id)
        active_snapshot = active_snapshot_store.active() if active_snapshot_store is not None else None
        if active_snapshot is None:
            return None
        market_sessions = pending._market_sessions()
        return pending.framework_runner.run_once(
            indicator_snapshot=active_snapshot,
            data=self._latest_data_slice(active_snapshot),
            portfolio=self.portfolio_provider.current_portfolio(self.sleeve_id),
            alpha_symbols_by_model=pending._alpha_symbols_by_model(),
            market_session=pending._primary_market_session(market_sessions),
            market_sessions=market_sessions,
        )

    def _run_framework_once(self) -> FrameworkCycleResult | None:
        indicator_snapshot = self.worker.stores_by_sleeve.get(self.sleeve_id, None)
        active_snapshot = indicator_snapshot.active() if indicator_snapshot is not None else None
        if active_snapshot is None:
            return None
        data = self._latest_data_slice(active_snapshot)
        self.portfolio = self.portfolio_provider.current_portfolio(self.sleeve_id)
        market_sessions = self._market_sessions()
        return self.framework_runner.run_once(
            indicator_snapshot=active_snapshot,
            data=data,
            portfolio=self.portfolio,
            alpha_symbols_by_model=self._alpha_symbols_by_model(),
            market_session=self._primary_market_session(market_sessions),
            market_sessions=market_sessions,
        )

    def _alpha_symbols_by_model(self) -> dict[str, tuple[Symbol, ...]] | None:
        if not self.sleeve_config.alpha.input_selections:
            return None
        return {
            alpha_id: _selection_symbols_for(self.active_result, selection_id)
            for alpha_id, selection_id in self.sleeve_config.alpha.input_selections.items()
        }

    def _portfolio_engine_state(self, framework: FrameworkCycleResult | None) -> PortfolioEngineState | None:
        if framework is None:
            return None
        indicator_snapshot = self.worker.stores_by_sleeve.get(self.sleeve_id, None)
        active_snapshot = indicator_snapshot.active() if indicator_snapshot is not None else None
        if active_snapshot is None:
            return None
        return PortfolioEngineState.from_cycle(
            cycle=framework,
            portfolio=self.portfolio,
            data=self._latest_data_slice(active_snapshot),
        )

    def _latest_data_slice(self, indicator_snapshot: IndicatorSnapshot) -> DataSlice:
        market_snapshot = self.worker.last_market_snapshot
        if market_snapshot is not None and market_snapshot.bars:
            return market_snapshot.as_data_slice()
        return _data_slice_from_indicator_snapshot(indicator_snapshot)

    def _primary_market_session(self, market_sessions: Mapping[str, MarketSession]) -> MarketSession | None:
        return market_sessions.get(market_scope_from_market(self.coarse_universe.market))

    def _market_sessions(self) -> dict[str, MarketSession]:
        scopes = {market_scope_from_market(self.coarse_universe.market)}
        scopes.update(str(scope).strip().lower() for scope in self.sleeve_config.broker_account_routes.keys())
        scopes.update(market_scope_for_symbol(symbol) for symbol in self.active_result.active_universe.symbols)
        scopes.update(market_scope_for_symbol(symbol) for symbol in self.portfolio.held_symbols)
        return {
            scope: _synthetic_market_session_for_scope(scope)
            for scope in sorted(scopes)
            if scope in {"domestic", "overseas"}
        }

    def _agent_status(self, report: "RuntimeRunOnceReport") -> dict[str, Any]:
        cycle = report.worker.cycles[-1] if report.worker.cycles else None
        framework = report.framework
        portfolio_state = report.portfolio_state
        data = None
        if cycle is not None:
            active_snapshot_store = self.worker.stores_by_sleeve.get(self.sleeve_id)
            active_snapshot = active_snapshot_store.active() if active_snapshot_store is not None else None
            data = self._latest_data_slice(active_snapshot) if active_snapshot is not None else None
        if data is not None:
            portfolio_equity_by_currency = self.portfolio.equity_by_currency(
                data,
                self.portfolio.currencies(data),
            )
        else:
            portfolio_equity_by_currency = dict(self.portfolio.cash_by_currency)
        portfolio_equity = (
            next(iter(portfolio_equity_by_currency.values()))
            if len(portfolio_equity_by_currency) == 1
            else 0.0
        )
        return {
            "event": "engine_status",
            "runtime_id": self.runtime_id,
            "config_version": self.config_version,
            "sleeve_id": self.sleeve_id,
            "coarse_universe_id": report.coarse_universe_id,
            "active_universe_id": report.active_universe_id,
            "cycle_completed": report.worker.cycles_completed,
            "snapshot": {
                "status": cycle.snapshot_quality.status.value if cycle is not None else "missing",
                "as_of": cycle.snapshot_as_of if cycle is not None else None,
                "updated_symbol_count": cycle.updated_symbol_count if cycle is not None else 0,
                "failed_symbol_count": cycle.failed_symbol_count if cycle is not None else 0,
                "complete_ratio": cycle.snapshot_quality.complete_ratio if cycle is not None else 0.0,
                "reasons": list(cycle.snapshot_quality.reasons) if cycle is not None else [],
            },
            "portfolio": {
                "cash": self.portfolio.cash,
                "cash_by_currency": dict(self.portfolio.cash_by_currency),
                "equity": portfolio_equity,
                "equity_by_currency": portfolio_equity_by_currency,
                "held_symbol_count": len(self.portfolio.held_symbols),
                "held_symbols": [symbol.key for symbol in self.portfolio.held_symbols],
            },
            "framework": {
                "ran": framework is not None,
                "active_insight_count": framework.active_insight_count if framework is not None else 0,
                "allocation_target_count": framework.portfolio_target_batch.target_count
                if framework is not None
                else 0,
                "allocation_plan_count": framework.portfolio_target_batch.plan_count
                if framework is not None
                else 0,
                "target_count": framework.order_sizing_batch.target_count if framework is not None else 0,
                "plan_count": framework.order_sizing_batch.plan_count if framework is not None else 0,
                "risk_decision_count": len(framework.risk_decisions.decisions) if framework is not None else 0,
                "approved_target_count": len(framework.risk_decisions.approved_targets) if framework is not None else 0,
                "order_intent_count": len(framework.order_intents) if framework is not None else 0,
                "model_state_patch_count": len(framework.state_patches) if framework is not None else 0,
                "model_state_event_count": len(framework.state_events) if framework is not None else 0,
                "model_state_commit_enabled": framework.state_commit_enabled if framework is not None else False,
            },
            "portfolio_engine_state": portfolio_state.to_dict(include_details=False)
            if portfolio_state is not None
            else None,
        }


@dataclass(frozen=True, slots=True)
class RuntimeRunOnceReport:
    runtime_id: str
    config_version: str
    sleeve_id: str
    coarse_universe_id: str
    active_universe_id: str
    fine_refresh_report: FineUniverseRefreshReport | None
    active_result: ActiveUniverseResult
    selection_warmup_report: WarmupReport | None
    worker: SnapshotWorkerRunReport
    framework: FrameworkCycleResult | None = None
    portfolio_state: PortfolioEngineState | None = None
    agent_status: dict[str, Any] = field(default_factory=dict)

    def to_dict(
        self,
        *,
        include_candidates: bool = True,
        include_warmup_symbols: bool = True,
        include_failures: bool = True,
        include_framework_details: bool = True,
    ) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "config_version": self.config_version,
            "sleeve_id": self.sleeve_id,
            "coarse_universe_id": self.coarse_universe_id,
            "fine_refresh": self.fine_refresh_report.to_dict(include_failures=include_failures)
            if self.fine_refresh_report is not None
            else None,
            "selection_warmup": self.selection_warmup_report.to_dict(include_symbols=include_warmup_symbols)
            if self.selection_warmup_report is not None
            else None,
            "active_universe_id": self.active_universe_id,
            "selection": self.active_result.selection.to_dict(include_candidates=include_candidates),
            "worker": self.worker.to_dict(
                include_warmup_symbols=include_warmup_symbols,
                include_failures=include_failures,
            ),
            "framework": self.framework.to_dict(include_details=include_framework_details)
            if self.framework is not None
            else None,
            "portfolio_state": self.portfolio_state.to_dict(include_details=include_framework_details)
            if self.portfolio_state is not None
            else None,
            "engine_status": dict(self.agent_status),
        }


@dataclass(frozen=True, slots=True)
class RuntimeSleeveReloadReport:
    sleeve_id: str
    previous_version: str
    staged_version: str
    dry_run_framework_ran: bool
    dry_run_order_intent_count: int
    activated: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "previous_version": self.previous_version,
            "staged_version": self.staged_version,
            "dry_run_framework_ran": self.dry_run_framework_ran,
            "dry_run_order_intent_count": self.dry_run_order_intent_count,
            "activated": self.activated,
            "reason": self.reason,
        }


def bootstrap_sleeve_runtime(
    snapshot: RuntimeConfigSnapshot,
    sleeve_id: str | None = None,
    *,
    dependencies: RuntimeBootstrapDependencies | None = None,
    refresh_fine: bool = True,
    previous_live_symbols: tuple[Symbol, ...] = (),
    held_symbols: tuple[Symbol, ...] = (),
    open_order_symbols: tuple[Symbol, ...] = (),
    exit_watch_symbols: tuple[Symbol, ...] = (),
    manual_symbols: tuple[Symbol, ...] = (),
    preselect_warmup: bool | None = None,
) -> RuntimeSleeveRuntime:
    deps = dependencies or RuntimeBootstrapDependencies()
    sleeve_config = _resolve_sleeve_config(snapshot, sleeve_id)
    coarse_universe = deps.load_universe(resolve_runtime_path(snapshot, sleeve_config.universe.coarse_path))
    live_provider = deps.live_provider_factory(
        coarse_universe,
        snapshot.config.market_data.rate_limit_per_second,
    )
    history_provider = deps.history_provider_factory()
    alpha_runtime = _build_alpha_runtime(snapshot, sleeve_config, deps.alpha_loader)
    portfolio_engine = _build_portfolio_engine(snapshot, sleeve_config, deps.portfolio_model_loader)
    portfolio_blend_engine = _build_portfolio_blend_engine(sleeve_config)
    risk_model = _build_risk_model(snapshot, sleeve_config, deps.risk_model_loader)
    execution_engine = _build_execution_engine(snapshot, sleeve_config, deps.execution_model_loader)
    selection_models = _build_selection_models(sleeve_config.universe.active, sleeve_config, snapshot)
    selection_model = selection_models[0]

    selection_base_universe = coarse_universe
    fine_runtime = None
    fine_refresh_report = None
    warmup_policy = WarmupPolicy(
        extra_bars=sleeve_config.indicators.extra_bars,
        min_ready_ratio=sleeve_config.indicators.min_ready_ratio,
    )
    should_preselect_warmup = sleeve_config.indicators.warmup_enabled if preselect_warmup is None else preselect_warmup
    indicator_engine = deps.indicator_engine or IndicatorEngine()
    indicator_snapshot_stores = deps.indicator_snapshot_stores if deps.indicator_snapshot_stores is not None else {}
    selection_warmup_report = None
    selection_indicator_snapshot = None
    portfolio_provider = deps.portfolio_provider or _build_portfolio_provider(snapshot, sleeve_config)
    portfolio = portfolio_provider.current_portfolio(sleeve_config.sleeve_id)
    held_symbols = _merge_symbols(portfolio.held_symbols, held_symbols)
    framework_runner = FrameworkRunner(
        sleeve_id=sleeve_config.sleeve_id,
        alpha_runtime=alpha_runtime,
        portfolio_engine=portfolio_engine,
        portfolio_blend_engine=portfolio_blend_engine,
        risk_model=risk_model,
        execution_engine=execution_engine,
        runtime_state_store=deps.runtime_state_store,
        runtime_state_commit_enabled=deps.runtime_state_commit_enabled,
    )
    if sleeve_config.universe.fine.enabled:
        fine_runtime = FineUniverseRuntime(
            universe=coarse_universe,
            provider=live_provider,
            source=snapshot.config.market_data.source,
            max_age_seconds=sleeve_config.universe.fine.max_age_seconds,
        )
        if refresh_fine:
            fine_refresh_report = fine_runtime.refresh_once(
                max_symbols=sleeve_config.universe.fine.max_symbols,
                min_success=sleeve_config.universe.fine.min_success,
            )
            selection_base_universe = fine_runtime.fine_universe_definition(
                universe_id=f"{coarse_universe.id}-fine",
            )

    if should_preselect_warmup:
        warmup_result = run_daily_indicator_warmup(
            selection_base_universe,
            history_provider,
            sleeve_id=sleeve_config.sleeve_id,
            refresh_history=sleeve_config.indicators.refresh_history,
            source=snapshot.config.market_data.history_source,
            policy=warmup_policy,
            indicator_engine=indicator_engine,
        )
        selection_warmup_report = warmup_result.report
        selection_indicator_snapshot = warmup_result.indicator_engine.snapshot(
            sleeve_config.sleeve_id,
            universe_id=selection_base_universe.id,
            source_snapshot_id=f"warmup:{selection_base_universe.id}",
            quality_report=_snapshot_quality_from_warmup(selection_warmup_report),
        )

    if len(selection_models) == 1:
        selection_runtime = UniverseSelectionRuntime(
            coarse_universe=selection_base_universe,
            selection_model=selection_model,
        )
    else:
        selection_runtime = CompositeUniverseSelectionRuntime(
            coarse_universe=selection_base_universe,
            selection_models=selection_models,
        )
    active_result = selection_runtime.select_active(
        sleeve_id=sleeve_config.sleeve_id,
        indicator_snapshot=selection_indicator_snapshot,
        as_of=selection_indicator_snapshot.as_of if selection_indicator_snapshot is not None else None,
        previous_live_symbols=previous_live_symbols,
        held_symbols=held_symbols,
        open_order_symbols=open_order_symbols,
        exit_watch_symbols=exit_watch_symbols,
        manual_symbols=manual_symbols,
        active_universe_id=f"{selection_base_universe.id}-active",
    )
    if should_preselect_warmup:
        indicator_engine.set_active_universe(sleeve_config.sleeve_id, active_result.active_universe)
    worker = BackgroundSnapshotWorker(
        universe=active_result.active_universe,
        sleeve_id=sleeve_config.sleeve_id,
        live_provider=live_provider,
        history_provider=history_provider,
        source=snapshot.config.market_data.source,
        history_source=snapshot.config.market_data.history_source,
        min_success=sleeve_config.worker.min_success,
        interval_seconds=sleeve_config.worker.cycle_interval_seconds,
        indicator_engine=indicator_engine,
        stores_by_sleeve=indicator_snapshot_stores,
        warmup_policy=warmup_policy,
        entry_block_reasons=_warmup_entry_block_reasons(selection_warmup_report),
    )
    return RuntimeSleeveRuntime(
        snapshot=snapshot,
        sleeve_config=sleeve_config,
        coarse_universe=coarse_universe,
        selection_model=selection_model,
        selection_models=selection_models,
        live_provider=live_provider,
        history_provider=history_provider,
        alpha_runtime=alpha_runtime,
        framework_runner=framework_runner,
        portfolio_provider=portfolio_provider,
        portfolio=portfolio,
        fine_runtime=fine_runtime,
        fine_refresh_report=fine_refresh_report,
        active_result=active_result,
        selection_warmup_report=selection_warmup_report,
        selection_indicator_snapshot=selection_indicator_snapshot,
        worker=worker,
    )


def _resolve_sleeve_config(snapshot: RuntimeConfigSnapshot, sleeve_id: str | None) -> SleeveRuntimeConfig:
    if sleeve_id is not None:
        return snapshot.config.sleeve(sleeve_id)
    if len(snapshot.config.sleeves) != 1:
        raise RuntimeBootstrapError("sleeve_id is required when runtime config has multiple sleeves.")
    return snapshot.config.sleeves[0]


def _build_alpha_runtime(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
    alpha_loader: PythonAlphaLoader,
) -> AlphaRuntime:
    models = []
    for module in sleeve_config.alpha.modules:
        models.append(alpha_loader.load(_resolve_sleeve_path(snapshot, sleeve_config, Path(module.ref))).model)
    return AlphaRuntime(active_models=tuple(models))


def _build_portfolio_engine(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
    portfolio_model_loader: PythonPortfolioConstructionModelLoader,
) -> PortfolioConstructionEngine:
    portfolio_config = sleeve_config.portfolio
    model_ref = _resolve_model_reference(snapshot, sleeve_config, portfolio_config.model.ref)
    model_result = portfolio_model_loader.load(model_ref, parameters=portfolio_config.parameters)
    rebalance_config = portfolio_config.rebalance
    return PortfolioConstructionEngine(
        model=model_result.model,
        rebalance_policy=RebalancePolicy(
            cash_reserve_pct=rebalance_config.cash_reserve_pct,
            min_order_notional=rebalance_config.min_order_notional,
            min_quantity_delta=rebalance_config.min_quantity_delta,
            allow_exit_below_min_notional=rebalance_config.allow_exit_below_min_notional,
            cadence=rebalance_config.cadence,
            reused_target_churn_guard=rebalance_config.reused_target_churn_guard,
            reused_target_churn_max_quantity_delta=rebalance_config.reused_target_churn_max_quantity_delta,
            reused_target_churn_lot_fraction=rebalance_config.reused_target_churn_lot_fraction,
            reused_target_churn_equity_bps=rebalance_config.reused_target_churn_equity_bps,
        ),
    )


def _build_portfolio_blend_engine(sleeve_config: SleeveRuntimeConfig) -> PortfolioBlendEngine | None:
    blend_config = sleeve_config.portfolio.blend
    if not blend_config.enabled:
        return None
    return PortfolioBlendEngine(
        policy=PortfolioBlendPolicy(
            enabled=blend_config.enabled,
            duration_minutes=blend_config.duration_minutes,
            target_drift_threshold_pct=blend_config.target_drift_threshold_pct,
            clock=blend_config.clock,
            missing_target_behavior=blend_config.missing_target_behavior,
            bypass_target_tag_tokens=blend_config.bypass_target_tag_tokens,
        )
    )


def _build_risk_model(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
    risk_model_loader: PythonRiskManagementModelLoader,
):
    risk_config = sleeve_config.risk
    model_ref = _resolve_model_reference(snapshot, sleeve_config, risk_config.model.ref)
    return risk_model_loader.load(model_ref, parameters=risk_config.parameters).model


def _build_execution_engine(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
    execution_model_loader: PythonExecutionModelLoader,
) -> ExecutionEngine:
    execution_config = sleeve_config.execution
    model_ref = _resolve_model_reference(snapshot, sleeve_config, execution_config.model.ref)
    model = execution_model_loader.load(model_ref, parameters=execution_config.parameters).model
    return ExecutionEngine(model=model)


def _warmup_entry_block_reasons(report: WarmupReport | None) -> tuple[str, ...]:
    if report is None or report.is_ready:
        return ()
    return ("warmup_not_ready",)


def _snapshot_quality_from_warmup(report: WarmupReport) -> SnapshotQualityReport:
    return SnapshotQualityReport(
        status=SnapshotQualityStatus.FRESH if report.is_ready else SnapshotQualityStatus.DEGRADED,
        complete_ratio=report.ready_ratio,
        age_seconds=0.0,
        collection_seconds=max(report.total_elapsed_ms / 1000.0, 0.0),
        requested_symbol_count=report.requested_symbol_count,
        collected_symbol_count=report.ready_symbol_count,
        failed_symbol_count=report.failed_symbol_count,
        reasons=() if report.is_ready else ("warmup_not_ready",),
    )


def _build_portfolio_provider(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
) -> PortfolioProvider:
    account_store_path = _portfolio_account_store_path(snapshot, sleeve_config)
    if account_store_path is None:
        default_cash_by_sleeve = {
            sleeve.sleeve_id: sleeve.cash
            for sleeve in snapshot.config.sleeves
        }
        return StaticPortfolioProvider(default_cash_by_sleeve=default_cash_by_sleeve)
    default_currency = "KRW"
    if sleeve_config.broker_account_id:
        try:
            default_currency = snapshot.config.broker_account(sleeve_config.broker_account_id).currency
        except KeyError:
            default_currency = "KRW"
    default_cash_by_sleeve = {
        sleeve.sleeve_id: float(dict(getattr(sleeve, "cash_by_currency", {}) or {}).get(default_currency, sleeve.cash))
        for sleeve in snapshot.config.sleeves
    }
    default_cash_by_currency_by_sleeve = {
        sleeve.sleeve_id: {
            str(currency).strip().upper(): float(amount)
            for currency, amount in dict(getattr(sleeve, "cash_by_currency", {}) or {}).items()
        }
        for sleeve in snapshot.config.sleeves
    }
    return VirtualSleeveAccountStore(
        resolve_runtime_path(snapshot, account_store_path),
        default_cash_by_sleeve=default_cash_by_sleeve,
        default_cash_by_currency_by_sleeve=default_cash_by_currency_by_sleeve,
        default_currency=default_currency,
    )


def _portfolio_account_store_path(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
) -> Path | None:
    if sleeve_config.broker_account_id:
        try:
            return snapshot.config.broker_account(sleeve_config.broker_account_id).account_store_path
        except KeyError:
            return sleeve_config.portfolio.account_store_path
    routes = dict(getattr(sleeve_config, "broker_account_routes", {}) or {})
    if len(set(routes.values())) == 1:
        try:
            return snapshot.config.broker_account(next(iter(routes.values()))).account_store_path
        except KeyError:
            return sleeve_config.portfolio.account_store_path
    return sleeve_config.portfolio.account_store_path


def _build_selection_model(
    reference: ModuleReference,
    sleeve_config: SleeveRuntimeConfig,
    snapshot: RuntimeConfigSnapshot | None = None,
) -> UniverseSelectionModel:
    resolved = reference
    if snapshot is not None:
        resolved = ModuleReference(_resolve_module_reference(snapshot, sleeve_config, reference.ref))
    loaded = _load_reference(resolved)
    if not inspect.isclass(loaded) and hasattr(loaded, "select"):
        return _validate_selection_model(loaded)
    if not callable(loaded):
        raise RuntimeBootstrapError(f"Selection model reference is not callable: {reference.ref}")
    kwargs = {}
    signature = inspect.signature(loaded)
    if "max_active_symbols" in signature.parameters:
        kwargs["max_active_symbols"] = sleeve_config.universe.active.max_symbols
    elif "max_symbols" in signature.parameters:
        kwargs["max_symbols"] = sleeve_config.universe.active.max_symbols
    return _validate_selection_model(loaded(**kwargs))


def _build_selection_models(
    active_config: ActiveUniverseRuntimeConfig,
    sleeve_config: SleeveRuntimeConfig,
    snapshot: RuntimeConfigSnapshot | None = None,
) -> tuple[UniverseSelectionModel, ...]:
    references = active_config.selection_models or (active_config.selection_model,)
    return tuple(_build_selection_model(reference, sleeve_config, snapshot) for reference in references)


def _validate_selection_model(model: Any) -> UniverseSelectionModel:
    if not callable(getattr(model, "select", None)):
        raise RuntimeBootstrapError("Selection model must provide select(context).")
    if not getattr(model, "selection_id", None):
        raise RuntimeBootstrapError("Selection model must provide selection_id.")
    return model


def _selection_symbols_for(active_result: ActiveUniverseResult, selection_id: str) -> tuple[Symbol, ...]:
    selection = active_result.selection
    if hasattr(selection, "selections") and hasattr(selection, "symbols_for_selection"):
        if selection_id not in selection.selections:
            raise RuntimeBootstrapError(f"Unknown alpha input selection_id: {selection_id}")
        return selection.symbols_for_selection(selection_id)
    if getattr(selection, "selection_id", None) == selection_id:
        return selection.selected_symbols
    raise RuntimeBootstrapError(f"Unknown alpha input selection_id: {selection_id}")


def _merge_symbols(*groups: Iterable[Symbol]) -> tuple[Symbol, ...]:
    merged: list[Symbol] = []
    seen: set[str] = set()
    for group in groups:
        for symbol in group:
            if symbol.key in seen:
                continue
            seen.add(symbol.key)
            merged.append(symbol)
    return tuple(merged)


def _load_reference(reference: ModuleReference) -> Any:
    text = reference.ref
    if ":" not in text:
        raise RuntimeBootstrapError(f"Module reference must use module:object format: {text}")
    module_name, object_name = text.rsplit(":", 1)
    module = _load_module(module_name)
    value = module
    for part in object_name.split("."):
        value = getattr(value, part)
    return value


def _load_module(module_name_or_path: str) -> ModuleType:
    path = Path(module_name_or_path)
    if path.suffix == ".py" or path.exists():
        resolved = path.resolve()
        spec = importlib.util.spec_from_file_location(resolved.stem, resolved)
        if spec is None or spec.loader is None:
            raise RuntimeBootstrapError(f"Cannot load module from {resolved}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    return import_module(module_name_or_path)


def _resolve_model_reference(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig, ref: str) -> str:
    return _resolve_module_reference(snapshot, sleeve_config, ref)


def _resolve_module_reference(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig, ref: str) -> str:
    if ":" in ref:
        module_ref, object_ref = ref.rsplit(":", 1)
        module_path = Path(module_ref)
        if module_path.suffix == ".py" or module_path.exists():
            return f"{_resolve_sleeve_path(snapshot, sleeve_config, module_path)}:{object_ref}"
    path = Path(ref)
    if path.suffix == ".py" or path.exists():
        return str(_resolve_sleeve_path(snapshot, sleeve_config, path))
    return ref


def _resolve_sleeve_path(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig, path: Path) -> Path:
    if path.is_absolute():
        return path
    if sleeve_config.workspace_path is not None:
        return _resolve_sleeve_workspace(snapshot, sleeve_config) / path
    return resolve_runtime_path(snapshot, path)


def _resolve_sleeve_workspace(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig) -> Path:
    if sleeve_config.workspace_path is None:
        return snapshot.source_path.parent
    return resolve_runtime_path(snapshot, sleeve_config.workspace_path)


def _data_slice_from_indicator_snapshot(snapshot: IndicatorSnapshot) -> DataSlice:
    bars: dict[str, Bar] = {}
    for symbol_key in snapshot.symbols:
        close = _snapshot_price(snapshot, symbol_key)
        if close is None or close <= 0:
            continue
        volume = snapshot.value(symbol_key, "volume", ready_only=False) or 0
        symbol = _symbol_from_key(symbol_key)
        bars[symbol.key] = Bar(
            symbol=symbol,
            time=snapshot.as_of,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=int(volume),
        )
    return DataSlice(time=snapshot.as_of, bars=bars)


def _snapshot_price(snapshot: IndicatorSnapshot, symbol_key: str) -> float | None:
    for name in ("close", "identity_close", "price"):
        value = snapshot.value(symbol_key, name, ready_only=False)
        if value is not None:
            return value
    return None


def _symbol_from_key(symbol_key: str) -> Symbol:
    market, ticker = symbol_key.split(":", 1)
    return Symbol(ticker=ticker, market=market)


def _synthetic_market_session_for_scope(market_scope: str) -> MarketSession:
    if market_scope == "overseas":
        return synthetic_us_market_session(datetime.now(timezone.utc))
    return synthetic_domestic_market_session(datetime.now(timezone(timedelta(hours=9))))


def resolve_runtime_path(snapshot: RuntimeConfigSnapshot, path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute() or path.exists():
        return path
    return snapshot.source_path.parent / path


def _default_live_provider_factory(
    universe: UniverseDefinition,
    rate_limit_per_second: int | None,
) -> MarketDataProvider:
    return MarketDataEngineLiveQuoteProvider.from_env(
        exchange_by_symbol=_exchange_map_from_universe(universe),
        rate_limit_per_second=rate_limit_per_second,
    )


def _default_history_provider_factory() -> MarketDataProvider:
    return _FallbackHistoryProvider(
        primary=KISCachedMarketDataProvider.from_env(),
        fallback=FinanceDataReaderMarketDataProvider(),
    )


@dataclass(frozen=True, slots=True)
class _FallbackHistoryProvider(MarketDataProvider):
    primary: MarketDataProvider
    fallback: MarketDataProvider

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        try:
            return self.primary.get_latest_bar(symbol)
        except Exception:  # noqa: BLE001 - runtime warmup should degrade to deterministic public history.
            return self.fallback.get_latest_bar(symbol)

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        try:
            bars = self.primary.get_history(symbol, start=start, end=end)
        except Exception:  # noqa: BLE001 - provider failures are surfaced by warmup if fallback also fails.
            bars = []
        if bars:
            return bars
        return self.fallback.get_history(symbol, start=start, end=end)


def _exchange_map_from_universe(universe: UniverseDefinition) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        exchange = universe.properties_for(symbol).get("exchange")
        if exchange:
            exchange_by_symbol[symbol.key] = str(exchange).strip().upper()
            exchange_by_symbol[symbol.ticker] = str(exchange).strip().upper()
    return exchange_by_symbol

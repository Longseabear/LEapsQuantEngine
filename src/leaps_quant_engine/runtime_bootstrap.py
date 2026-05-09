from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import importlib.util
import inspect
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from leaps_quant_engine.adapters.kis import KISCachedMarketDataProvider, MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader
from leaps_quant_engine.execution import ExecutionEngine
from leaps_quant_engine.execution_model_loader import PythonExecutionModelLoader
from leaps_quant_engine.framework import (
    FrameworkCycleResult,
    FrameworkRunner,
    PortfolioConstructionEngine,
    PythonPortfolioConstructionModelLoader,
    PythonRiskManagementModelLoader,
    RebalancePolicy,
)
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio, PortfolioProvider, StaticPortfolioProvider
from leaps_quant_engine.portfolio_state import PortfolioEngineState
from leaps_quant_engine.runtime_config import ModuleReference, RuntimeConfigSnapshot, SleeveRuntimeConfig
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker, SnapshotWorkerRunReport
from leaps_quant_engine.snapshots import IndicatorSnapshot
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.fine import FineUniverseRefreshReport, FineUniverseRuntime
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.universe.runtime import ActiveUniverseResult, UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import UniverseSelectionModel
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore
from leaps_quant_engine.warmup import WarmupPolicy


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
    live_provider: MarketDataProvider
    history_provider: MarketDataProvider
    alpha_runtime: AlphaRuntime
    framework_runner: FrameworkRunner
    portfolio_provider: PortfolioProvider
    portfolio: Portfolio
    worker: BackgroundSnapshotWorker
    active_result: ActiveUniverseResult
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
            worker=run_report,
            framework=framework_result,
            portfolio_state=portfolio_state,
        )
        status = self._agent_status(report)
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
        self.live_provider = pending.live_provider
        self.history_provider = pending.history_provider
        self.alpha_runtime = pending.alpha_runtime
        self.framework_runner = pending.framework_runner
        self.portfolio_provider = pending.portfolio_provider
        self.portfolio = pending.portfolio
        self.worker = pending.worker
        self.active_result = pending.active_result
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
        return pending.framework_runner.run_once(
            indicator_snapshot=active_snapshot,
            data=_data_slice_from_indicator_snapshot(active_snapshot),
            portfolio=self.portfolio_provider.current_portfolio(self.sleeve_id),
        )

    def _run_framework_once(self) -> FrameworkCycleResult | None:
        indicator_snapshot = self.worker.stores_by_sleeve.get(self.sleeve_id, None)
        active_snapshot = indicator_snapshot.active() if indicator_snapshot is not None else None
        if active_snapshot is None:
            return None
        data = _data_slice_from_indicator_snapshot(active_snapshot)
        self.portfolio = self.portfolio_provider.current_portfolio(self.sleeve_id)
        return self.framework_runner.run_once(
            indicator_snapshot=active_snapshot,
            data=data,
            portfolio=self.portfolio,
        )

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
            data=_data_slice_from_indicator_snapshot(active_snapshot),
        )

    def _agent_status(self, report: "RuntimeRunOnceReport") -> dict[str, Any]:
        cycle = report.worker.cycles[-1] if report.worker.cycles else None
        framework = report.framework
        portfolio_state = report.portfolio_state
        data = None
        if cycle is not None:
            active_snapshot_store = self.worker.stores_by_sleeve.get(self.sleeve_id)
            active_snapshot = active_snapshot_store.active() if active_snapshot_store is not None else None
            data = _data_slice_from_indicator_snapshot(active_snapshot) if active_snapshot is not None else None
        portfolio_equity = self.portfolio.equity(data) if data is not None else self.portfolio.cash
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
                "equity": portfolio_equity,
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
    worker: SnapshotWorkerRunReport
    framework: FrameworkCycleResult | None = None
    portfolio_state: PortfolioEngineState | None = None

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
    risk_model = _build_risk_model(snapshot, sleeve_config, deps.risk_model_loader)
    execution_engine = _build_execution_engine(snapshot, sleeve_config, deps.execution_model_loader)
    selection_model = _build_selection_model(sleeve_config.universe.active.selection_model, sleeve_config, snapshot)

    selection_base_universe = coarse_universe
    fine_runtime = None
    fine_refresh_report = None
    portfolio_provider = deps.portfolio_provider or _build_portfolio_provider(snapshot, sleeve_config)
    portfolio = portfolio_provider.current_portfolio(sleeve_config.sleeve_id)
    framework_runner = FrameworkRunner(
        sleeve_id=sleeve_config.sleeve_id,
        alpha_runtime=alpha_runtime,
        portfolio_engine=portfolio_engine,
        risk_model=risk_model,
        execution_engine=execution_engine,
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

    selection_runtime = UniverseSelectionRuntime(
        coarse_universe=selection_base_universe,
        selection_model=selection_model,
    )
    active_result = selection_runtime.select_active(
        sleeve_id=sleeve_config.sleeve_id,
        previous_live_symbols=previous_live_symbols,
        held_symbols=held_symbols,
        open_order_symbols=open_order_symbols,
        exit_watch_symbols=exit_watch_symbols,
        manual_symbols=manual_symbols,
        active_universe_id=f"{selection_base_universe.id}-active",
    )
    worker = BackgroundSnapshotWorker(
        universe=active_result.active_universe,
        sleeve_id=sleeve_config.sleeve_id,
        live_provider=live_provider,
        history_provider=history_provider,
        source=snapshot.config.market_data.source,
        history_source=snapshot.config.market_data.history_source,
        min_success=sleeve_config.worker.min_success,
        interval_seconds=sleeve_config.worker.cycle_interval_seconds,
        warmup_policy=WarmupPolicy(
            extra_bars=sleeve_config.indicators.extra_bars,
            min_ready_ratio=sleeve_config.indicators.min_ready_ratio,
        ),
    )
    return RuntimeSleeveRuntime(
        snapshot=snapshot,
        sleeve_config=sleeve_config,
        coarse_universe=coarse_universe,
        selection_model=selection_model,
        live_provider=live_provider,
        history_provider=history_provider,
        alpha_runtime=alpha_runtime,
        framework_runner=framework_runner,
        portfolio_provider=portfolio_provider,
        portfolio=portfolio,
        fine_runtime=fine_runtime,
        fine_refresh_report=fine_refresh_report,
        active_result=active_result,
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
        ),
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


def _build_portfolio_provider(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
) -> PortfolioProvider:
    if sleeve_config.portfolio.account_store_path is None:
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
    return VirtualSleeveAccountStore(
        resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
        default_cash_by_sleeve=default_cash_by_sleeve,
        default_currency=default_currency,
    )


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


def _validate_selection_model(model: Any) -> UniverseSelectionModel:
    if not callable(getattr(model, "select", None)):
        raise RuntimeBootstrapError("Selection model must provide select(context).")
    if not getattr(model, "selection_id", None):
        raise RuntimeBootstrapError("Selection model must provide selection_id.")
    return model


def _load_reference(reference: ModuleReference) -> Any:
    text = reference.ref
    if ":" not in text:
        raise RuntimeBootstrapError(f"Module reference must use module:object format: {text}")
    module_name, object_name = text.split(":", 1)
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
        spec.loader.exec_module(module)
        return module
    return import_module(module_name_or_path)


def _resolve_model_reference(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig, ref: str) -> str:
    return _resolve_module_reference(snapshot, sleeve_config, ref)


def _resolve_module_reference(snapshot: RuntimeConfigSnapshot, sleeve_config: SleeveRuntimeConfig, ref: str) -> str:
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
    return KISCachedMarketDataProvider.from_env()


def _exchange_map_from_universe(universe: UniverseDefinition) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        exchange = universe.properties_for(symbol).get("exchange")
        if exchange:
            exchange_by_symbol[symbol.key] = str(exchange).strip().upper()
            exchange_by_symbol[symbol.ticker] = str(exchange).strip().upper()
    return exchange_by_symbol

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from leaps_quant_engine.adapters.kis import KISCachedMarketDataProvider, MarketDataEngineLiveQuoteProvider
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader
from leaps_quant_engine.framework import (
    FrameworkCycleResult,
    FrameworkRunner,
    PortfolioConstructionEngine,
    PythonPortfolioConstructionModelLoader,
    RebalancePolicy,
)
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime_config import ModuleReference, RuntimeConfigSnapshot, SleeveRuntimeConfig
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker, SnapshotWorkerRunReport
from leaps_quant_engine.snapshots import IndicatorSnapshot
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.fine import FineUniverseRefreshReport, FineUniverseRuntime
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.universe.runtime import ActiveUniverseResult, UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import UniverseSelectionModel
from leaps_quant_engine.warmup import WarmupPolicy


class RuntimeBootstrapError(RuntimeError):
    """Raised when a runtime config snapshot cannot be converted into executable objects."""


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
    portfolio: Portfolio
    worker: BackgroundSnapshotWorker
    active_result: ActiveUniverseResult
    fine_runtime: FineUniverseRuntime | None = None
    fine_refresh_report: FineUniverseRefreshReport | None = None

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
        return RuntimeRunOnceReport(
            runtime_id=self.runtime_id,
            config_version=self.config_version,
            sleeve_id=self.sleeve_id,
            coarse_universe_id=self.coarse_universe.id,
            active_universe_id=self.active_result.active_universe.id,
            fine_refresh_report=self.fine_refresh_report,
            active_result=self.active_result,
            worker=run_report,
            framework=framework_result,
        )

    def _run_framework_once(self) -> FrameworkCycleResult | None:
        indicator_snapshot = self.worker.stores_by_sleeve.get(self.sleeve_id, None)
        active_snapshot = indicator_snapshot.active() if indicator_snapshot is not None else None
        if active_snapshot is None:
            return None
        data = _data_slice_from_indicator_snapshot(active_snapshot)
        return self.framework_runner.run_once(
            indicator_snapshot=active_snapshot,
            data=data,
            portfolio=self.portfolio,
        )


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
    coarse_universe = deps.load_universe(_resolve_path(snapshot, sleeve_config.universe.coarse_path))
    live_provider = deps.live_provider_factory(
        coarse_universe,
        snapshot.config.market_data.rate_limit_per_second,
    )
    history_provider = deps.history_provider_factory()
    alpha_runtime = _build_alpha_runtime(snapshot, sleeve_config, deps.alpha_loader)
    portfolio_engine = _build_portfolio_engine(snapshot, sleeve_config, deps.portfolio_model_loader)
    selection_model = _build_selection_model(sleeve_config.universe.active.selection_model, sleeve_config)

    selection_base_universe = coarse_universe
    fine_runtime = None
    fine_refresh_report = None
    portfolio = Portfolio(cash=sleeve_config.cash)
    framework_runner = FrameworkRunner(
        sleeve_id=sleeve_config.sleeve_id,
        alpha_runtime=alpha_runtime,
        portfolio_engine=portfolio_engine,
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
        models.append(alpha_loader.load(_resolve_path(snapshot, Path(module.ref))).model)
    return AlphaRuntime(active_models=tuple(models))


def _build_portfolio_engine(
    snapshot: RuntimeConfigSnapshot,
    sleeve_config: SleeveRuntimeConfig,
    portfolio_model_loader: PythonPortfolioConstructionModelLoader,
) -> PortfolioConstructionEngine:
    portfolio_config = sleeve_config.portfolio
    model_ref = _resolve_model_reference(snapshot, portfolio_config.model.ref)
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


def _build_selection_model(reference: ModuleReference, sleeve_config: SleeveRuntimeConfig) -> UniverseSelectionModel:
    loaded = _load_reference(reference)
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


def _resolve_path(snapshot: RuntimeConfigSnapshot, path: Path) -> Path:
    return resolve_runtime_path(snapshot, path)


def _resolve_model_reference(snapshot: RuntimeConfigSnapshot, ref: str) -> str:
    path = Path(ref)
    if path.suffix == ".py" or path.exists():
        return str(resolve_runtime_path(snapshot, path))
    return ref


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

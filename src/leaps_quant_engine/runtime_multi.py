from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import re
import statistics
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from leaps_quant_engine.framework import FileFrameworkRunnerStateStore
from leaps_quant_engine.indicators import IndicatorEngine, IndicatorUpdateReport
from leaps_quant_engine.market_data_snapshot import MarketDataSnapshot, MarketDataSnapshotEngine
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.runtime_bootstrap import (
    RuntimeBootstrapDependencies,
    RuntimeRunOnceReport,
    RuntimeSleeveRuntime,
    bootstrap_sleeve_runtime,
    _snapshot_store_for_config,
    _runtime_live_provider,
)
from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot
from leaps_quant_engine.snapshot_worker import (
    SnapshotWorkerCycleReport,
    SnapshotWorkerRunReport,
    _quality_with_entry_blocks,
)
from leaps_quant_engine.snapshots import IndicatorSnapshotStore, SnapshotFreshnessPolicy, SnapshotQualityReport
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(frozen=True, slots=True)
class MultiSleeveRuntimeOnceReport:
    runtime_id: str
    config_version: str
    sleeve_ids: tuple[str, ...]
    started_at: str
    completed_at: str
    source: str
    requested_symbol_count: int
    collected_symbol_count: int
    failed_symbol_count: int
    market_snapshot_id: str
    collection_elapsed_ms: float
    indicator_update_snapshot_ms: float
    reports: tuple[RuntimeRunOnceReport, ...]
    framework_state: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def order_count(self) -> int:
        return sum(len(report.framework.order_intents) for report in self.reports if report.framework is not None)

    def execution_batches(self):
        return tuple(
            report.framework.execution_batch
            for report in self.reports
            if report.framework is not None
        )

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
            "runner": "multi-sleeve-single-runner",
            "source": self.source,
            "sleeve_ids": list(self.sleeve_ids),
            "sleeve_count": len(self.sleeve_ids),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "market_snapshot": {
                "snapshot_id": self.market_snapshot_id,
                "requested_symbol_count": self.requested_symbol_count,
                "collected_symbol_count": self.collected_symbol_count,
                "failed_symbol_count": self.failed_symbol_count,
                "collection_elapsed_ms": self.collection_elapsed_ms,
                "indicator_update_snapshot_ms": self.indicator_update_snapshot_ms,
            },
            "order_count": self.order_count,
            "framework_state": {sleeve_id: dict(summary) for sleeve_id, summary in self.framework_state.items()},
            "reports": [
                report.to_dict(
                    include_candidates=include_candidates,
                    include_warmup_symbols=include_warmup_symbols,
                    include_failures=include_failures,
                    include_framework_details=include_framework_details,
                )
                for report in self.reports
            ],
        }


def run_multi_sleeve_once(
    snapshot: RuntimeConfigSnapshot,
    sleeve_ids: Sequence[str],
    *,
    dependencies: RuntimeBootstrapDependencies | None = None,
    refresh_fine: bool = True,
    warmup: bool | None = None,
    framework_state_dir: Path | None = None,
    framework_state_read_only: bool = False,
    source: str | None = None,
) -> MultiSleeveRuntimeOnceReport:
    if not sleeve_ids:
        raise ValueError("At least one sleeve_id is required for multi-sleeve runtime.")
    normalized_sleeve_ids = tuple(dict.fromkeys(str(sleeve_id).strip() for sleeve_id in sleeve_ids if str(sleeve_id).strip()))
    if not normalized_sleeve_ids:
        raise ValueError("At least one non-empty sleeve_id is required for multi-sleeve runtime.")

    shared_indicator_engine = IndicatorEngine()
    shared_stores: dict[str, IndicatorSnapshotStore] = {}
    deps = dependencies or RuntimeBootstrapDependencies()
    shared_deps = replace(
        deps,
        indicator_engine=shared_indicator_engine,
        indicator_snapshot_stores=shared_stores,
    )

    started_at = datetime.now()
    runtimes = tuple(
        bootstrap_sleeve_runtime(
            snapshot,
            sleeve_id,
            dependencies=shared_deps,
            refresh_fine=refresh_fine,
            preselect_warmup=False if warmup is False else None,
        )
        for sleeve_id in normalized_sleeve_ids
    )
    framework_state_summary = _restore_framework_state(
        runtimes,
        framework_state_dir=framework_state_dir,
        read_only=framework_state_read_only,
    )

    union_universe = _union_active_universe(snapshot.config.runtime_id, runtimes)
    live_provider = _runtime_live_provider(snapshot, union_universe, deps)
    snapshot_engine = MarketDataSnapshotEngine(
        provider=live_provider,
        indicator_engine=shared_indicator_engine,
        stores_by_sleeve=shared_stores,
        source=source or snapshot.config.market_data.source,
        snapshot_store=_snapshot_store_for_config(snapshot),
    )
    collection = snapshot_engine.collect_once_best_effort(list(union_universe.symbols))
    update_started = time.perf_counter()
    quality_by_sleeve = {
        runtime.sleeve_id: _quality_report_for_runtime(runtime, collection.snapshot, collection.report)
        for runtime in runtimes
    }
    indicator_snapshots = snapshot_engine.update_indicators(
        collection.snapshot,
        sleeve_ids=[runtime.sleeve_id for runtime in runtimes],
        universe_id_by_sleeve={runtime.sleeve_id: runtime.active_result.active_universe.id for runtime in runtimes},
        quality_report_by_sleeve=quality_by_sleeve,
    )
    indicator_update_reports = dict(snapshot_engine.last_indicator_update_report_by_sleeve)
    indicator_update_ms = (time.perf_counter() - update_started) * 1000

    reports: list[RuntimeRunOnceReport] = []
    completed_at = datetime.now()
    for runtime in runtimes:
        indicator_snapshot = runtime.worker._enrich_indicator_snapshot(indicator_snapshots[runtime.sleeve_id])
        runtime.worker.stores_by_sleeve.setdefault(runtime.sleeve_id, IndicatorSnapshotStore()).publish_active(
            indicator_snapshot
        )
        sleeve_snapshot = _filter_market_snapshot(collection.snapshot, runtime.active_result.active_universe.symbols)
        runtime.worker.last_market_snapshot = sleeve_snapshot
        runtime.worker.last_market_snapshot_by_lane[sleeve_snapshot.lane] = sleeve_snapshot
        worker_report = _worker_report_for_runtime(
            runtime,
            collection_snapshot=collection.snapshot,
            indicator_snapshot=indicator_snapshot,
            quality_report=quality_by_sleeve[runtime.sleeve_id],
            collection_report=collection.report,
            indicator_update_report=indicator_update_reports.get(runtime.sleeve_id, IndicatorUpdateReport()),
            indicator_update_ms=indicator_update_ms,
            started_at=started_at,
            completed_at=completed_at,
        )
        reports.append(runtime.build_run_once_report(worker_report))

    _save_framework_state(
        reports,
        runtimes,
        framework_state_dir=framework_state_dir,
        read_only=framework_state_read_only,
        summaries=framework_state_summary,
    )
    completed_at = datetime.now()
    return MultiSleeveRuntimeOnceReport(
        runtime_id=snapshot.config.runtime_id,
        config_version=snapshot.version,
        sleeve_ids=normalized_sleeve_ids,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        source=source or snapshot.config.market_data.source,
        requested_symbol_count=collection.report.requested_symbol_count,
        collected_symbol_count=collection.report.collected_symbol_count,
        failed_symbol_count=collection.report.failed_symbol_count,
        market_snapshot_id=collection.snapshot.snapshot_id,
        collection_elapsed_ms=collection.report.elapsed_ms,
        indicator_update_snapshot_ms=indicator_update_ms,
        reports=tuple(reports),
        framework_state=framework_state_summary,
    )


def _union_active_universe(runtime_id: str, runtimes: Iterable[RuntimeSleeveRuntime]) -> UniverseDefinition:
    symbols: list[Symbol] = []
    seen: set[str] = set()
    indicators = []
    indicator_keys: set[tuple[str, str, int, str, str]] = set()
    symbol_properties: dict[str, Mapping[str, Any]] = {}
    markets: set[str] = set()
    for runtime in runtimes:
        universe = runtime.active_result.active_universe
        markets.add(universe.market)
        for symbol in universe.symbols:
            if symbol.key not in seen:
                seen.add(symbol.key)
                symbols.append(symbol)
                properties = universe.properties_for(symbol)
                if properties:
                    symbol_properties[symbol.key] = dict(properties)
        for indicator in universe.indicators:
            key = (indicator.name, indicator.type, indicator.period, indicator.field, indicator.resolution)
            if key in indicator_keys:
                continue
            indicator_keys.add(key)
            indicators.append(indicator)
    market = next(iter(markets)) if len(markets) == 1 else "MIXED"
    return UniverseDefinition(
        id=f"{runtime_id}-multi-active-union",
        market=market,
        symbols=tuple(symbols),
        indicators=tuple(indicators),
        tags=("multi-sleeve-runtime",),
        symbol_properties=symbol_properties,
    )


def _quality_report_for_runtime(runtime: RuntimeSleeveRuntime, snapshot: MarketDataSnapshot, report) -> SnapshotQualityReport:
    requested_symbols = runtime.active_result.active_universe.symbols
    requested_keys = {symbol.key for symbol in requested_symbols}
    collected_count = sum(1 for symbol_key in requested_keys if symbol_key in snapshot.bars)
    relevant_failures = [failure for failure in report.failures if failure.symbol_key in requested_keys]
    failed_count = max(len(requested_keys) - collected_count, len(relevant_failures))
    quality = SnapshotFreshnessPolicy().evaluate(
        requested_symbol_count=len(requested_keys),
        collected_symbol_count=collected_count,
        failed_symbol_count=max(failed_count, 0),
        completed_at=report.completed_at,
        elapsed_ms=report.elapsed_ms,
    )
    return _quality_with_entry_blocks(
        quality,
        (*runtime.worker.entry_block_reasons, *_market_data_entry_block_reasons(snapshot, requested_keys)),
    )


def _worker_report_for_runtime(
    runtime: RuntimeSleeveRuntime,
    *,
    collection_snapshot: MarketDataSnapshot,
    indicator_snapshot,
    quality_report: SnapshotQualityReport,
    collection_report,
    indicator_update_report: IndicatorUpdateReport,
    indicator_update_ms: float,
    started_at: datetime,
    completed_at: datetime,
) -> SnapshotWorkerRunReport:
    requested_keys = {symbol.key for symbol in runtime.active_result.active_universe.symbols}
    relevant_failures = tuple(
        {"symbol": failure.symbol_key, "message": failure.message}
        for failure in collection_report.failures
        if failure.symbol_key in requested_keys
    )
    ready_counts = [
        len(indicator_snapshot.ready_values(symbol_key))
        for symbol_key in indicator_snapshot.symbols
    ]
    updated_count = sum(1 for symbol_key in requested_keys if symbol_key in collection_snapshot.bars)
    failed_count = max(len(requested_keys) - updated_count, len(relevant_failures))
    cycle = SnapshotWorkerCycleReport(
        cycle_index=1,
        source=collection_snapshot.source,
        sleeve_id=runtime.sleeve_id,
        universe_id=runtime.active_result.active_universe.id,
        requested_symbol_count=len(requested_keys),
        updated_symbol_count=updated_count,
        failed_symbol_count=failed_count,
        indicator_count_per_symbol=len(runtime.active_result.active_universe.indicators),
        indicator_updates_estimated=updated_count * len(runtime.active_result.active_universe.indicators),
        indicator_update_count=indicator_update_report.updated_count,
        indicator_resolution_mismatch_count=indicator_update_report.resolution_mismatch_count,
        market_snapshot_id=collection_snapshot.snapshot_id,
        market_snapshot_lane=collection_snapshot.lane,
        indicator_snapshot_id=indicator_snapshot.snapshot_id,
        indicator_snapshot_lane=indicator_snapshot.lane,
        snapshot_as_of=indicator_snapshot.as_of.isoformat(),
        snapshot_quality=quality_report,
        collection_elapsed_ms=collection_report.elapsed_ms,
        indicator_update_snapshot_ms=indicator_update_ms,
        ready_count_min=min(ready_counts, default=0),
        ready_count_avg=statistics.mean(ready_counts) if ready_counts else 0.0,
        ready_count_max=max(ready_counts, default=0),
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        failures=relevant_failures,
    )
    return SnapshotWorkerRunReport(
        sleeve_id=runtime.sleeve_id,
        universe_id=runtime.active_result.active_universe.id,
        source=collection_snapshot.source,
        cycles_requested=1,
        cycles_completed=1,
        started_at=started_at.isoformat(),
        completed_at=completed_at.isoformat(),
        warmup=None,
        cycles=(cycle,),
    )


def _filter_market_snapshot(snapshot: MarketDataSnapshot, symbols: Iterable[Symbol]) -> MarketDataSnapshot:
    keys = {symbol.key for symbol in symbols}
    bars: dict[str, Bar] = {key: bar for key, bar in snapshot.bars.items() if key in keys}
    return MarketDataSnapshot.from_bars(
        bars,
        source=snapshot.source,
        snapshot_id=f"{snapshot.snapshot_id}:filtered",
        time=snapshot.time,
        lane=snapshot.lane,
    )


def _market_data_entry_block_reasons(snapshot: MarketDataSnapshot, requested_keys: set[str]) -> tuple[str, ...]:
    reasons: list[str] = []
    for symbol_key, bar in snapshot.bars.items():
        if symbol_key not in requested_keys:
            continue
        metadata = dict(getattr(bar, "metadata", {}) or {})
        if metadata.get("live_price_usable") is False:
            reasons.append("live_price_unusable")
            price_quality_reason = str(metadata.get("price_quality_reason") or "").strip()
            if price_quality_reason:
                reasons.append(f"price_quality:{price_quality_reason}")
    return tuple(dict.fromkeys(reasons))


def _restore_framework_state(
    runtimes: Iterable[RuntimeSleeveRuntime],
    *,
    framework_state_dir: Path | None,
    read_only: bool,
) -> dict[str, dict[str, Any]]:
    summaries: dict[str, dict[str, Any]] = {}
    if framework_state_dir is None:
        return summaries
    framework_state_dir.mkdir(parents=True, exist_ok=True)
    for runtime in runtimes:
        path = _framework_state_path(framework_state_dir, runtime.sleeve_id)
        store = FileFrameworkRunnerStateStore(path)
        restored_state = store.load()
        runtime.framework_runner.restore_state(restored_state)
        summaries[runtime.sleeve_id] = {
            "path": str(path.resolve()),
            "restored": restored_state is not None,
            "read_only": read_only,
        }
    return summaries


def _save_framework_state(
    reports: Iterable[RuntimeRunOnceReport],
    runtimes: Sequence[RuntimeSleeveRuntime],
    *,
    framework_state_dir: Path | None,
    read_only: bool,
    summaries: dict[str, dict[str, Any]],
) -> None:
    if framework_state_dir is None or read_only:
        return
    runtime_by_sleeve = {runtime.sleeve_id: runtime for runtime in runtimes}
    for report in reports:
        runtime = runtime_by_sleeve[report.sleeve_id]
        path = _framework_state_path(framework_state_dir, report.sleeve_id)
        state_as_of = report.framework.new_insight_batch.generated_at if report.framework is not None else datetime.now()
        FileFrameworkRunnerStateStore(path).save(runtime.framework_runner.export_state(as_of=state_as_of))
        summaries.setdefault(report.sleeve_id, {"path": str(path.resolve()), "restored": False, "read_only": read_only})
        summaries[report.sleeve_id]["saved"] = True


def _framework_state_path(root: Path, sleeve_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", sleeve_id)
    return root / f"{safe}.json"

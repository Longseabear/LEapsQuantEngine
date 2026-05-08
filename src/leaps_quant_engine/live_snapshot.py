from __future__ import annotations

import logging
import statistics
import time
from typing import Any

from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.market_data_snapshot import MarketDataSnapshotEngine
from leaps_quant_engine.universe.definition import UniverseDefinition


logger = logging.getLogger(__name__)


def run_live_indicator_snapshot(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    sleeve_id: str,
    source: str,
    min_success: int | None = None,
    include_failures: bool = False,
) -> dict[str, Any]:
    provider_client = getattr(provider, "client", None)
    rate_limit_per_second = getattr(provider_client, "rate_limit_per_second", None)
    logger.info(
        "live_indicator_snapshot.start",
        extra={
            "source": source,
            "sleeve_id": sleeve_id,
            "universe_id": universe.id,
            "universe_size": len(universe.symbols),
            "indicator_count_per_symbol": len(universe.indicators),
            "min_success": min_success,
            "rate_limit_per_second": rate_limit_per_second,
        },
    )
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe(sleeve_id, universe)
    snapshot_engine = MarketDataSnapshotEngine(
        provider=provider,
        indicator_engine=indicator_engine,
        source=source,
    )

    collection = snapshot_engine.collect_once_best_effort(
        list(universe.symbols),
        min_success=min_success,
    )
    update_started = time.perf_counter()
    indicator_snapshots = snapshot_engine.update_indicators(
        collection.snapshot,
        sleeve_ids=[sleeve_id],
        universe_id_by_sleeve={sleeve_id: universe.id},
    )
    indicator_update_ms = (time.perf_counter() - update_started) * 1000

    indicator_snapshot = indicator_snapshots[sleeve_id]
    ready_counts = [len(indicator_snapshot.ready_values(symbol_key)) for symbol_key in indicator_snapshot.symbols]
    report = {
        "source": source,
        "measurement_scope": "MarketDataSnapshotEngine.collect_once_best_effort + IndicatorEngine.on_data",
        "rate_limit_per_second": rate_limit_per_second,
        "universe_id": universe.id,
        "sleeve_id": sleeve_id,
        "universe_size": len(universe.symbols),
        "requested_symbol_count": collection.report.requested_symbol_count,
        "updated_symbol_count": collection.report.collected_symbol_count,
        "failed_symbol_count": collection.report.failed_symbol_count,
        "indicator_count_per_symbol": len(universe.indicators),
        "indicator_updates_estimated": collection.report.collected_symbol_count * len(universe.indicators),
        "market_snapshot_id": collection.snapshot.snapshot_id,
        "indicator_snapshot_id": indicator_snapshot.snapshot_id,
        "snapshot_as_of": indicator_snapshot.as_of.isoformat(),
        "collection_elapsed_ms": collection.report.elapsed_ms,
        "indicator_update_snapshot_ms": indicator_update_ms,
        "ready_count_min": min(ready_counts, default=0),
        "ready_count_avg": statistics.mean(ready_counts) if ready_counts else 0.0,
        "ready_count_max": max(ready_counts, default=0),
    }
    if include_failures:
        report["failures"] = [
            {"symbol": failure.symbol_key, "message": failure.message}
            for failure in collection.report.failures
        ]
    logger.info(
        "live_indicator_snapshot.complete",
        extra={
            "source": source,
            "sleeve_id": sleeve_id,
            "universe_id": universe.id,
            "updated_symbol_count": report["updated_symbol_count"],
            "failed_symbol_count": report["failed_symbol_count"],
            "collection_elapsed_ms": report["collection_elapsed_ms"],
            "indicator_update_snapshot_ms": report["indicator_update_snapshot_ms"],
            "market_snapshot_id": report["market_snapshot_id"],
            "indicator_snapshot_id": report["indicator_snapshot_id"],
            "rate_limit_per_second": rate_limit_per_second,
        },
    )
    return report

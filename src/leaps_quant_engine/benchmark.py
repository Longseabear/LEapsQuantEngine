from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from math import ceil
from time import perf_counter
from typing import Any

from leaps_quant_engine.history import load_daily_history as _load_daily_history
from leaps_quant_engine.history import get_daily_history
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


MEASUREMENT_SCOPE = "IndicatorEngine.on_data"


def run_daily_indicator_benchmark(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    sleeve_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
    include_daily: bool = False,
    source: str = "kis-cache",
    clock: Callable[[], float] = perf_counter,
) -> dict[str, Any]:
    resolved_end = end or datetime.now()
    resolved_start = start or (resolved_end - timedelta(days=120))
    symbols = list(universe.symbols)

    history_start = clock()
    history_by_symbol = load_daily_history(
        provider,
        symbols,
        start=resolved_start,
        end=resolved_end,
        refresh_history=refresh_history,
    )
    history_load_ms = _elapsed_ms(history_start, clock())

    replay_start = clock()
    feed = build_replay_feed_from_history(history_by_symbol)
    replay_build_ms = _elapsed_ms(replay_start, clock())

    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe(sleeve_id, universe)

    daily: list[dict[str, Any]] = []
    elapsed_by_session_ms: list[float] = []
    bars_seen = 0
    for data in feed:
        cycle_start = clock()
        indicator_engine.on_data(data)
        elapsed_ms = _elapsed_ms(cycle_start, clock())
        elapsed_by_session_ms.append(elapsed_ms)
        bars_seen += len(data.bars)
        if include_daily:
            daily.append(
                {
                    "date": data.time.date().isoformat(),
                    "bar_count": len(data.bars),
                    "elapsed_ms": elapsed_ms,
                    "ready_symbol_count": _ready_symbol_count(indicator_engine, sleeve_id, symbols),
                }
            )

    indicator_count_per_symbol = len(universe.indicators)
    report: dict[str, Any] = {
        "sleeve_id": sleeve_id,
        "universe_id": universe.id,
        "universe_size": len(symbols),
        "updated_symbol_count": len(indicator_engine.active_symbols()),
        "indicator_count_per_symbol": indicator_count_per_symbol,
        "sessions": len(feed),
        "bars_seen": bars_seen,
        "indicator_updates_estimated": bars_seen * indicator_count_per_symbol,
        "measurement_scope": MEASUREMENT_SCOPE,
        "avg_ms": _average(elapsed_by_session_ms),
        "p50_ms": _percentile(elapsed_by_session_ms, 50),
        "p95_ms": _percentile(elapsed_by_session_ms, 95),
        "max_ms": max(elapsed_by_session_ms, default=0.0),
        "total_update_ms": sum(elapsed_by_session_ms),
        "history_load_ms": history_load_ms,
        "replay_build_ms": replay_build_ms,
        "start": resolved_start.date().isoformat(),
        "end": resolved_end.date().isoformat(),
        "source": source,
        "ready_symbol_count": _ready_symbol_count(indicator_engine, sleeve_id, symbols),
    }
    if include_daily:
        report["daily"] = daily
    return report


def load_daily_history(
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
) -> dict[str, list[Bar]]:
    return _load_daily_history(
        provider,
        symbols,
        start=start,
        end=end,
        refresh_history=refresh_history,
    )


def build_replay_feed_from_history(history_by_symbol: dict[str, list[Bar]]) -> list[DataSlice]:
    bars_by_time: dict[datetime, dict[str, Bar]] = {}
    for symbol_key, bars in history_by_symbol.items():
        for bar in bars:
            bars_by_time.setdefault(bar.time, {})[symbol_key] = bar
    return [
        DataSlice(time=time, bars=bars_by_time[time])
        for time in sorted(bars_by_time)
    ]


def _get_history(
    provider: MarketDataProvider,
    symbol: Symbol,
    *,
    start: datetime | None,
    end: datetime | None,
    refresh_history: bool,
) -> list[Bar]:
    return get_daily_history(
        provider,
        symbol,
        start=start,
        end=end,
        refresh_history=refresh_history,
    )


def _ready_symbol_count(indicator_engine: IndicatorEngine, sleeve_id: str, symbols: list[Symbol]) -> int:
    return sum(1 for symbol in symbols if indicator_engine.ready_values(sleeve_id, symbol))


def _elapsed_ms(start: float, end: float) -> float:
    return (end - start) * 1000.0


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = max(0, min(len(sorted_values) - 1, ceil((percentile / 100.0) * len(sorted_values)) - 1))
    return sorted_values[index]

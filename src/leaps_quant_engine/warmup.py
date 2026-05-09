from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any

from leaps_quant_engine.history import get_daily_history
from leaps_quant_engine.indicators import IndicatorEngine, create_indicator
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


MEASUREMENT_SCOPE = "IndicatorEngine.warm_up"


@dataclass(frozen=True, slots=True)
class WarmupPolicy:
    extra_bars: int = 0
    min_ready_ratio: float = 1.0
    default_calendar_days: int = 120

    def __post_init__(self) -> None:
        if self.extra_bars < 0:
            raise ValueError("extra_bars must be non-negative.")
        if not 0.0 <= self.min_ready_ratio <= 1.0:
            raise ValueError("min_ready_ratio must be between 0 and 1.")
        if self.default_calendar_days <= 0:
            raise ValueError("default_calendar_days must be positive.")

    def required_bars(self, universe: UniverseDefinition) -> int:
        warmup_period = max(
            (create_indicator(definition).warmup_period for definition in universe.indicators),
            default=0,
        )
        return warmup_period + self.extra_bars

    def default_start(self, end: datetime, required_bars: int) -> datetime:
        calendar_days = max(self.default_calendar_days, max(required_bars, 1) * 3)
        return end - timedelta(days=calendar_days)


@dataclass(frozen=True, slots=True)
class WarmupSymbolReport:
    symbol_key: str
    loaded_bar_count: int
    required_warmup_bars: int
    indicator_count: int
    ready_indicator_count: int
    is_ready: bool
    failed: bool = False
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "symbol": self.symbol_key,
            "loaded_bar_count": self.loaded_bar_count,
            "required_warmup_bars": self.required_warmup_bars,
            "indicator_count": self.indicator_count,
            "ready_indicator_count": self.ready_indicator_count,
            "is_ready": self.is_ready,
            "failed": self.failed,
        }
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True, slots=True)
class WarmupReport:
    sleeve_id: str
    universe_id: str
    requested_symbol_count: int
    loaded_symbol_count: int
    failed_symbol_count: int
    indicator_count_per_symbol: int
    required_warmup_bars: int
    ready_symbol_count: int
    ready_ratio: float
    is_ready: bool
    measurement_scope: str
    history_load_ms: float
    warmup_update_ms: float
    total_elapsed_ms: float
    start: str
    end: str
    source: str
    symbols: tuple[WarmupSymbolReport, ...] = field(default_factory=tuple)

    def to_dict(self, *, include_symbols: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "requested_symbol_count": self.requested_symbol_count,
            "loaded_symbol_count": self.loaded_symbol_count,
            "failed_symbol_count": self.failed_symbol_count,
            "indicator_count_per_symbol": self.indicator_count_per_symbol,
            "required_warmup_bars": self.required_warmup_bars,
            "ready_symbol_count": self.ready_symbol_count,
            "ready_ratio": self.ready_ratio,
            "is_ready": self.is_ready,
            "measurement_scope": self.measurement_scope,
            "history_load_ms": self.history_load_ms,
            "warmup_update_ms": self.warmup_update_ms,
            "total_elapsed_ms": self.total_elapsed_ms,
            "start": self.start,
            "end": self.end,
            "source": self.source,
        }
        if include_symbols:
            payload["symbols"] = [symbol.to_dict() for symbol in self.symbols]
        return payload


@dataclass(frozen=True, slots=True)
class WarmupResult:
    indicator_engine: IndicatorEngine
    report: WarmupReport


def run_daily_indicator_warmup(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    sleeve_id: str,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
    source: str = "kis-cache",
    policy: WarmupPolicy | None = None,
    indicator_engine: IndicatorEngine | None = None,
    clock: Callable[[], float] = perf_counter,
) -> WarmupResult:
    warmup_policy = policy or WarmupPolicy()
    required_bars = warmup_policy.required_bars(universe)
    resolved_end = end or datetime.now()
    resolved_start = start or warmup_policy.default_start(resolved_end, required_bars)
    symbols = list(universe.symbols)
    engine = indicator_engine or IndicatorEngine()
    engine.register_universe(sleeve_id, universe)

    total_start = clock()
    history_start = clock()
    history_by_symbol: dict[str, list[Bar]] = {}
    failures: dict[str, str] = {}
    for symbol in symbols:
        try:
            bars = get_daily_history(
                provider,
                symbol,
                start=resolved_start,
                end=resolved_end,
                refresh_history=refresh_history,
            )
            history_by_symbol[symbol.key] = _latest_bars(bars, required_bars)
        except Exception as exc:  # noqa: BLE001 - warmup must report partial readiness.
            failures[symbol.key] = str(exc)
    history_load_ms = _elapsed_ms(history_start, clock())

    update_start = clock()
    engine.warm_up(sleeve_id, _flatten_history(symbols, history_by_symbol))
    warmup_update_ms = _elapsed_ms(update_start, clock())
    total_elapsed_ms = _elapsed_ms(total_start, clock())

    symbol_reports = tuple(
        _build_symbol_report(
            engine,
            sleeve_id,
            symbol,
            loaded_bar_count=len(history_by_symbol.get(symbol.key, [])),
            required_bars=required_bars,
            indicator_count=len(universe.indicators),
            failure_message=failures.get(symbol.key),
        )
        for symbol in symbols
    )
    ready_symbol_count = sum(1 for symbol_report in symbol_reports if symbol_report.is_ready)
    requested_symbol_count = len(symbols)
    ready_ratio = ready_symbol_count / requested_symbol_count if requested_symbol_count else 1.0

    return WarmupResult(
        indicator_engine=engine,
        report=WarmupReport(
            sleeve_id=sleeve_id,
            universe_id=universe.id,
            requested_symbol_count=requested_symbol_count,
            loaded_symbol_count=requested_symbol_count - len(failures),
            failed_symbol_count=len(failures),
            indicator_count_per_symbol=len(universe.indicators),
            required_warmup_bars=required_bars,
            ready_symbol_count=ready_symbol_count,
            ready_ratio=ready_ratio,
            is_ready=ready_ratio >= warmup_policy.min_ready_ratio,
            measurement_scope=MEASUREMENT_SCOPE,
            history_load_ms=history_load_ms,
            warmup_update_ms=warmup_update_ms,
            total_elapsed_ms=total_elapsed_ms,
            start=resolved_start.date().isoformat(),
            end=resolved_end.date().isoformat(),
            source=source,
            symbols=symbol_reports,
        ),
    )


def _latest_bars(bars: list[Bar], required_bars: int) -> list[Bar]:
    sorted_bars = sorted(bars, key=lambda bar: bar.time)
    if required_bars <= 0:
        return sorted_bars
    return sorted_bars[-required_bars:]


def _flatten_history(symbols: list[Symbol], history_by_symbol: dict[str, list[Bar]]) -> list[Bar]:
    return [
        bar
        for symbol in symbols
        for bar in history_by_symbol.get(symbol.key, [])
    ]


def _build_symbol_report(
    indicator_engine: IndicatorEngine,
    sleeve_id: str,
    symbol: Symbol,
    *,
    loaded_bar_count: int,
    required_bars: int,
    indicator_count: int,
    failure_message: str | None,
) -> WarmupSymbolReport:
    ready_indicator_count = len(indicator_engine.ready_values(sleeve_id, symbol))
    failed = failure_message is not None
    return WarmupSymbolReport(
        symbol_key=symbol.key,
        loaded_bar_count=loaded_bar_count,
        required_warmup_bars=required_bars,
        indicator_count=indicator_count,
        ready_indicator_count=ready_indicator_count,
        is_ready=not failed and ready_indicator_count == indicator_count,
        failed=failed,
        message=failure_message,
    )


def _elapsed_ms(start: float, end: float) -> float:
    return (end - start) * 1000.0

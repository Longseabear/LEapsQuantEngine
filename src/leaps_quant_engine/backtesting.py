from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, time as dt_time
from pathlib import Path
import csv
import gzip
import hashlib
import json
import math
import time as perf_time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from leaps_quant_engine.broker_routing import market_scope_for_symbol, market_scope_from_market
from leaps_quant_engine.cycle_journal import CycleJournalEntry, CycleJournalStore
from leaps_quant_engine.framework import FrameworkCycleResult, FrameworkRunner
from leaps_quant_engine.fundamentals import PointInTimeFundamentalStore
from leaps_quant_engine.engine import Engine
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.history import get_daily_history
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.market_rules import synthetic_domestic_market_session, synthetic_us_market_session
from leaps_quant_engine.models import Bar, DataResolution, DataSlice, OrderIntent, OrderSide, Symbol
from leaps_quant_engine.market_data_snapshot import normalize_snapshot_lane
from leaps_quant_engine.orders import (
    FixedBpsSlippageModel,
    KisFeeModel,
    OrderCoordinator,
    OrderEvent,
    OrderIntentCollision,
    OrderTicket,
    SimulatedFillModel,
)
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol
from leaps_quant_engine.temporal_features import (
    TemporalFeatureWindowProvider,
    enrich_indicator_snapshot_with_temporal_features,
)
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.selection import (
    CompositeUniverseSelectionResult,
    UniverseSelectionContext,
    UniverseSelectionModel,
    UniverseSelectionResult,
    build_composite_universe_selection_result,
)


_SESSION_AWARE_BACKTEST_RESOLUTIONS = {
    DataResolution.MINUTE.value,
    DataResolution.LIVE.value,
    DataResolution.QUOTE.value,
}


@dataclass(slots=True)
class VirtualMarketDataProvider(MarketDataProvider):
    """In-memory market data provider for deterministic backtests."""

    history: dict[str, list[Bar]] = field(default_factory=dict)

    @classmethod
    def from_bars(cls, bars: list[Bar]) -> "VirtualMarketDataProvider":
        provider = cls()
        for bar in bars:
            provider.add_bar(bar)
        return provider

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        symbol: Symbol,
        time_column: str = "time",
    ) -> "VirtualMarketDataProvider":
        bars: list[Bar] = []
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                bars.append(
                    Bar(
                        symbol=symbol,
                        time=_parse_datetime(row[time_column]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    )
                )
        return cls.from_bars(bars)

    def add_bar(self, bar: Bar) -> None:
        bars = self.history.setdefault(bar.symbol.key, [])
        bars.append(bar)
        bars.sort(key=lambda item: item.time)

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        bars = self.history.get(symbol.key) or []
        if not bars:
            raise MarketDataError(f"No virtual bars for {symbol.key}")
        return bars[-1]

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        bars = self.history.get(symbol.key) or []
        return [
            bar
            for bar in bars
            if (start is None or bar.time >= start) and (end is None or bar.time <= end)
        ]


@dataclass(frozen=True, slots=True)
class BacktestSnapshot:
    time: datetime
    equity: float
    cash: float
    gross_exposure: float
    equity_by_currency: dict[str, float] = field(default_factory=dict)
    cash_by_currency: dict[str, float] = field(default_factory=dict)
    gross_exposure_by_currency: dict[str, float] = field(default_factory=dict)

    @property
    def exposure(self) -> float:
        return self.gross_exposure / self.equity if self.equity > 0 else 0.0


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    sleeve_id: str
    symbol: Symbol
    entry_time: datetime
    exit_time: datetime
    quantity: int
    average_entry_price: float
    exit_price: float
    pnl: float
    holding_days: float


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    initial_equity: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float
    mdd: float
    turnover: float
    avg_holding_days: float
    avg_exposure: float
    win_rate: float
    trade_count: int
    order_count: int
    slippage_cost: float = 0.0
    slippage_bps: float = 0.0
    fee_cost: float = 0.0
    total_friction_cost: float = 0.0

    def to_report(
        self,
        *,
        currency: str | None = None,
        currency_mode: str | None = None,
        valid_without_fx: bool | None = None,
    ) -> dict[str, float | int | str | bool]:
        report: dict[str, float | int | str | bool] = {
            "initial_equity": self.initial_equity,
            "final_equity": self.final_equity,
            "total_return": self.total_return,
            "cagr": self.cagr,
            "sharpe": self.sharpe,
            "mdd": self.mdd,
            "turnover": self.turnover,
            "avg_holding_days": self.avg_holding_days,
            "avg_exposure": self.avg_exposure,
            "win_rate": self.win_rate,
            "trade_count": self.trade_count,
            "order_count": self.order_count,
            "slippage_cost": self.slippage_cost,
            "slippage_bps": self.slippage_bps,
            "fee_cost": self.fee_cost,
            "total_friction_cost": self.total_friction_cost,
        }
        if currency:
            report["currency"] = currency
        if currency_mode:
            report["currency_mode"] = currency_mode
        if valid_without_fx is not None:
            report["valid_without_fx"] = valid_without_fx
        if valid_without_fx is False:
            report["warning"] = "cross_currency_metrics_are_native_currency_sums_without_fx"
        return report


@dataclass(frozen=True, slots=True)
class BacktestResult:
    orders: list[OrderIntent]
    order_tickets: list[OrderTicket]
    order_events: list[OrderEvent]
    order_collisions: list[OrderIntentCollision]
    final_cash_by_sleeve: dict[str, float]
    final_quantity_by_sleeve: dict[str, dict[str, int]]
    metrics: BacktestMetrics
    metrics_by_sleeve: dict[str, BacktestMetrics]
    snapshots_by_sleeve: dict[str, list[BacktestSnapshot]]
    trades_by_sleeve: dict[str, list[ClosedTrade]]

    def to_report(self) -> dict[str, object]:
        return {
            "metrics": self.metrics.to_report(),
            "metrics_by_sleeve": {
                sleeve_id: metrics.to_report()
                for sleeve_id, metrics in self.metrics_by_sleeve.items()
            },
            "final_cash_by_sleeve": self.final_cash_by_sleeve,
            "final_quantity_by_sleeve": self.final_quantity_by_sleeve,
            "order_ticket_count": len(self.order_tickets),
            "order_event_count": len(self.order_events),
            "order_collision_count": len(self.order_collisions),
            "order_collisions": [collision.to_dict() for collision in self.order_collisions],
        }


@dataclass(frozen=True, slots=True)
class FrameworkBacktestResult:
    sleeve_id: str
    universe_id: str
    orders: list[OrderIntent]
    order_tickets: list[OrderTicket]
    order_events: list[OrderEvent]
    order_collisions: list[OrderIntentCollision]
    framework_cycles: list[FrameworkCycleResult]
    final_cash: float
    final_cash_by_currency: dict[str, float]
    final_equity_by_currency: dict[str, float]
    final_quantity: dict[str, int]
    metrics: BacktestMetrics
    metrics_by_currency: dict[str, BacktestMetrics]
    snapshots: list[BacktestSnapshot]
    trades: list[ClosedTrade]
    selection_results: list[UniverseSelectionResult | CompositeUniverseSelectionResult]
    data_slice_count: int
    warmup_data_slice_count: int
    indicator_snapshot_count: int
    start: datetime | None
    end: datetime | None
    timings: Mapping[str, float] = field(default_factory=dict)

    @property
    def insight_count(self) -> int:
        return sum(cycle.new_insight_batch.insight_count for cycle in self.framework_cycles)

    @property
    def order_count(self) -> int:
        return len(self.orders)

    @property
    def model_state_patch_count(self) -> int:
        return sum(len(cycle.state_patches) for cycle in self.framework_cycles)

    @property
    def model_state_event_count(self) -> int:
        return sum(len(cycle.state_events) for cycle in self.framework_cycles)

    @property
    def framework_total_ms(self) -> float:
        return sum(cycle.timings.total_ms for cycle in self.framework_cycles)

    def to_report(
        self,
        *,
        include_orders: bool = True,
        include_insights: bool = False,
        include_selection_details: bool | None = None,
    ) -> dict[str, Any]:
        if include_selection_details is None:
            include_selection_details = include_orders
        payload: dict[str, Any] = {
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "data_slice_count": self.data_slice_count,
            "warmup_data_slice_count": self.warmup_data_slice_count,
            "indicator_snapshot_count": self.indicator_snapshot_count,
            "framework_cycle_count": len(self.framework_cycles),
            "insight_count": self.insight_count,
            "order_count": self.order_count,
            "order_ticket_count": len(self.order_tickets),
            "order_event_count": len(self.order_events),
            "order_collision_count": len(self.order_collisions),
            "model_state_patch_count": self.model_state_patch_count,
            "model_state_event_count": self.model_state_event_count,
            "framework_total_ms": self.framework_total_ms,
            "timings": dict(self.timings),
            "final_cash": self.final_cash,
            "final_cash_by_currency": dict(self.final_cash_by_currency),
            "final_equity_by_currency": dict(self.final_equity_by_currency),
            "final_quantity": dict(self.final_quantity),
            "metrics": _metrics_report_for_currencies(
                self.metrics,
                _metric_currency_codes(
                    self.final_cash_by_currency,
                    self.final_equity_by_currency,
                    self.metrics_by_currency,
                ),
            ),
            "metrics_by_currency": {
                currency: metrics.to_report(
                    currency=currency,
                    currency_mode="single_currency",
                    valid_without_fx=True,
                )
                for currency, metrics in self.metrics_by_currency.items()
            },
        }
        if include_orders:
            payload["orders"] = [_order_to_report(order) for order in self.orders]
            payload["order_collisions"] = [collision.to_dict() for collision in self.order_collisions]
        if include_insights:
            payload["insights"] = _insight_ledger_report(self.framework_cycles)
        if self.selection_results:
            payload["selection"] = _selection_report(
                self.selection_results,
                include_details=include_selection_details,
            )
        return payload


@dataclass(frozen=True, slots=True)
class CompiledMinuteReplayCacheReport:
    status: str
    path: str
    schema_version: str
    slice_count: int
    row_count: int
    start: datetime | None = None
    end: datetime | None = None
    source: str | None = None
    source_signature: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "path": self.path,
            "schema_version": self.schema_version,
            "slice_count": self.slice_count,
            "row_count": self.row_count,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "source": self.source,
            "source_signature": self.source_signature,
        }


@dataclass(frozen=True, slots=True)
class DailyWarmupCacheReport:
    status: str
    path: str | None
    schema_version: str
    row_count: int
    symbol_count: int
    start: datetime | None = None
    end: datetime | None = None
    source: str | None = None
    source_signature: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "path": self.path,
            "schema_version": self.schema_version,
            "row_count": self.row_count,
            "symbol_count": self.symbol_count,
            "start": self.start.isoformat() if self.start is not None else None,
            "end": self.end.isoformat() if self.end is not None else None,
            "source": self.source,
            "source_signature": self.source_signature,
        }


def build_replay_feed(
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
    include_opening_gap_context: bool = True,
    daily_bar_time: dt_time | None = None,
) -> list[DataSlice]:
    bars_by_symbol = {
        symbol.key: get_daily_history(
            provider,
            symbol,
            start=start,
            end=end,
            refresh_history=refresh_history,
        )
        for symbol in symbols
    }
    if daily_bar_time is not None:
        bars_by_symbol = {
            symbol_key: _with_daily_bar_time(series, daily_bar_time)
            for symbol_key, series in bars_by_symbol.items()
        }
    if include_opening_gap_context:
        bars_by_symbol = {
            symbol_key: _with_opening_gap_context(series)
            for symbol_key, series in bars_by_symbol.items()
        }
    bars_by_time: dict[datetime, dict[str, Bar]] = {}
    for symbol_key, series in bars_by_symbol.items():
        for bar in series:
            bars_by_time.setdefault(bar.time, {})[symbol_key] = bar
    return [
        DataSlice(time=time, bars=bars_by_time[time])
        for time in sorted(bars_by_time)
        if bars_by_time[time]
    ]


def _with_daily_bar_time(series: list[Bar], daily_bar_time: dt_time) -> list[Bar]:
    return [
        replace(
            bar,
            time=datetime.combine(
                bar.time.date(),
                daily_bar_time,
                tzinfo=bar.time.tzinfo,
            ),
        )
        for bar in series
    ]


def _with_opening_gap_context(series: list[Bar]) -> list[Bar]:
    ordered = sorted(series, key=lambda bar: bar.time)
    enriched: list[Bar] = []
    previous_close: float | None = None
    previous_time: datetime | None = None
    for bar in ordered:
        metadata = dict(bar.metadata)
        metadata.setdefault("opening_context_source", "daily_ohlc_proxy")
        metadata.setdefault("opening_context_available", False)
        if previous_close is not None and previous_close > 0 and bar.open > 0:
            metadata.update(
                {
                    "opening_context_available": True,
                    "previous_close": previous_close,
                    "previous_close_time": previous_time.isoformat() if previous_time is not None else "",
                    "opening_gap_pct": (float(bar.open) / previous_close) - 1.0,
                    "open_to_close_return_pct": (float(bar.close) / float(bar.open)) - 1.0,
                    "open_to_low_drawdown_pct": (float(bar.low) / float(bar.open)) - 1.0,
                    "open_to_high_runup_pct": (float(bar.high) / float(bar.open)) - 1.0,
                    "gap_filled": float(bar.low) <= previous_close <= float(bar.high),
                }
            )
        enriched.append(replace(bar, metadata=metadata))
        if bar.close > 0:
            previous_close = float(bar.close)
            previous_time = bar.time
    return enriched


def build_minute_replay_feed_from_bars(
    bars: Iterable[Bar],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[DataSlice]:
    bars_by_time: dict[datetime, dict[str, Bar]] = {}
    for bar in bars:
        if start is not None and bar.time < start:
            continue
        if end is not None and bar.time > end:
            continue
        minute_bar = replace(bar, resolution=DataResolution.MINUTE.value)
        bars_by_time.setdefault(minute_bar.time, {})[minute_bar.symbol.key] = minute_bar
    return [
        DataSlice(time=time, bars=bars_by_time[time], resolution=DataResolution.MINUTE.value)
        for time in sorted(bars_by_time)
        if bars_by_time[time]
    ]


def load_minute_replay_feed(
    path: str | Path,
    *,
    universe: UniverseDefinition | None = None,
    default_market: str = "KRX",
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[DataSlice]:
    """Load a local minute replay feed from CSV, JSON, or JSONL."""

    source_path = Path(path)
    universe_keys = set(universe.symbol_keys) if universe is not None else None
    market = getattr(universe, "market", default_market) if universe is not None else default_market
    return _build_minute_replay_feed_from_rows(
        _iter_minute_rows(source_path),
        universe_keys=universe_keys,
        default_market=market,
        start=start,
        end=end,
    )


def load_compiled_minute_replay_cache(path: str | Path) -> tuple[list[DataSlice], CompiledMinuteReplayCacheReport]:
    """Load a pre-grouped minute replay artifact without changing replay semantics."""

    source_path = Path(path)
    payload = _read_compiled_minute_replay_payload(source_path)
    schema_version = str(payload.get("schema_version") or "")
    if schema_version != "leaps_compiled_minute_replay.v1":
        raise ValueError(f"Unsupported compiled minute replay cache schema: {schema_version}")
    slices: list[DataSlice] = []
    row_count = 0
    for item in payload.get("slices") or ():
        if not isinstance(item, Mapping):
            raise ValueError(f"Compiled minute replay slices must be objects: {source_path}")
        slice_time = _parse_datetime(str(item["time"]))
        bars: dict[str, Bar] = {}
        for bar_item in item.get("bars") or ():
            if not isinstance(bar_item, Mapping):
                raise ValueError(f"Compiled minute replay bars must be objects: {source_path}")
            bar = _compiled_minute_bar_from_payload(bar_item, default_time=slice_time)
            bars[bar.symbol.key] = bar
            row_count += 1
        if bars:
            slices.append(DataSlice(time=slice_time, bars=bars, resolution=DataResolution.MINUTE.value))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    report = CompiledMinuteReplayCacheReport(
        status="hit",
        path=str(source_path),
        schema_version=schema_version,
        slice_count=len(slices),
        row_count=row_count,
        start=_parse_optional_datetime(metadata.get("start")),
        end=_parse_optional_datetime(metadata.get("end")),
        source=str(metadata.get("source") or "") or None,
        source_signature=str(metadata.get("source_signature") or "") or None,
    )
    return slices, report


def write_compiled_minute_replay_cache(
    path: str | Path,
    feed: list[DataSlice],
    *,
    source: str | None = None,
    source_signature: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> CompiledMinuteReplayCacheReport:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    slices_payload: list[dict[str, object]] = []
    for data in sorted(feed, key=lambda item: item.time):
        bars_payload = []
        for symbol_key in sorted(data.bars):
            bars_payload.append(_compiled_minute_bar_to_payload(data.bars[symbol_key]))
            row_count += 1
        if bars_payload:
            slices_payload.append({"time": data.time.replace(tzinfo=None).isoformat(), "bars": bars_payload})
    payload = {
        "schema_version": "leaps_compiled_minute_replay.v1",
        "metadata": {
            "source": source,
            "source_signature": source_signature,
            "start": start.isoformat() if start is not None else None,
            "end": end.isoformat() if end is not None else None,
            "created_at": datetime.now().isoformat(),
        },
        "slices": slices_payload,
    }
    _write_compiled_minute_replay_payload(destination, payload)
    return CompiledMinuteReplayCacheReport(
        status="written",
        path=str(destination),
        schema_version="leaps_compiled_minute_replay.v1",
        slice_count=len(slices_payload),
        row_count=row_count,
        start=start,
        end=end,
        source=source,
        source_signature=source_signature,
    )


def minute_replay_source_signature(path: str | Path | None) -> str | None:
    if path is None:
        return None
    source_path = Path(path)
    if not source_path.exists():
        return None
    stat = source_path.stat()
    payload = f"{source_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_daily_warmup_cache(path: str | Path) -> tuple[list[Bar], DailyWarmupCacheReport]:
    source_path = Path(path)
    payload = _read_compiled_minute_replay_payload(source_path)
    schema_version = str(payload.get("schema_version") or "")
    if schema_version != "leaps_daily_warmup_cache.v1":
        raise ValueError(f"Unsupported daily warmup cache schema: {schema_version}")
    bars = [
        _bar_from_cache_payload(item, default_resolution=DataResolution.DAILY.value)
        for item in payload.get("bars") or ()
        if isinstance(item, Mapping)
    ]
    bars = sorted(bars, key=lambda bar: (bar.time, bar.symbol.key))
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    report = DailyWarmupCacheReport(
        status="hit",
        path=str(source_path),
        schema_version=schema_version,
        row_count=len(bars),
        symbol_count=len({bar.symbol.key for bar in bars}),
        start=_parse_optional_datetime(metadata.get("start")),
        end=_parse_optional_datetime(metadata.get("end")),
        source=str(metadata.get("source") or "") or None,
        source_signature=str(metadata.get("source_signature") or "") or None,
    )
    return bars, report


def write_daily_warmup_cache(
    path: str | Path,
    bars: list[Bar],
    *,
    source: str | None = None,
    source_signature: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> DailyWarmupCacheReport:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted((replace(bar, resolution=DataResolution.DAILY.value) for bar in bars), key=lambda bar: (bar.time, bar.symbol.key))
    payload = {
        "schema_version": "leaps_daily_warmup_cache.v1",
        "metadata": {
            "source": source,
            "source_signature": source_signature,
            "start": start.isoformat() if start is not None else None,
            "end": end.isoformat() if end is not None else None,
            "created_at": datetime.now().isoformat(),
        },
        "bars": [_bar_to_cache_payload(bar) for bar in ordered],
    }
    _write_compiled_minute_replay_payload(destination, payload)
    return DailyWarmupCacheReport(
        status="written",
        path=str(destination),
        schema_version="leaps_daily_warmup_cache.v1",
        row_count=len(ordered),
        symbol_count=len({bar.symbol.key for bar in ordered}),
        start=start,
        end=end,
        source=source,
        source_signature=source_signature,
    )


def load_daily_warmup_bars_for_backtest(
    provider: MarketDataProvider,
    universe: UniverseDefinition,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
) -> list[Bar]:
    bars: list[Bar] = []
    for symbol in universe.symbols:
        bars.extend(
            get_daily_history(
                provider,
                symbol,
                start=start,
                end=end,
                refresh_history=refresh_history,
            )
        )
    return sorted(bars, key=lambda bar: (bar.time, bar.symbol.key))


def _build_minute_replay_feed_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    universe_keys: set[str] | None,
    default_market: str,
    start: datetime | None,
    end: datetime | None,
) -> list[DataSlice]:
    bars_by_time: dict[datetime, dict[str, Bar]] = {}
    for row in rows:
        bar = _minute_row_to_bar(row, default_market=default_market)
        if universe_keys is not None and bar.symbol.key not in universe_keys:
            continue
        if start is not None and bar.time < start:
            continue
        if end is not None and bar.time > end:
            continue
        minute_bar = replace(bar, resolution=DataResolution.MINUTE.value)
        bars_by_time.setdefault(minute_bar.time, {})[minute_bar.symbol.key] = minute_bar
    return [
        DataSlice(time=time, bars=bars_by_time[time], resolution=DataResolution.MINUTE.value)
        for time in sorted(bars_by_time)
        if bars_by_time[time]
    ]


def warm_up_daily_indicators_for_backtest(
    indicator_engine: IndicatorEngine,
    *,
    sleeve_id: str,
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
) -> int:
    if sleeve_id not in indicator_engine.registries_by_sleeve:
        indicator_engine.register_universe(sleeve_id, universe)
    bars = load_daily_warmup_bars_for_backtest(
        provider,
        universe,
        start=start,
        end=end,
        refresh_history=refresh_history,
    )
    indicator_engine.warm_up(sleeve_id, bars)
    return len(bars)


def universe_with_default_indicator_resolution(
    universe: UniverseDefinition,
    *,
    default_resolution: str,
) -> UniverseDefinition:
    default = str(default_resolution or "any").strip().lower() or "any"
    return replace(
        universe,
        indicators=tuple(
            replace(definition, resolution=default)
            if str(definition.resolution or "any").strip().lower() in {"", "any", "*"}
            else definition
            for definition in universe.indicators
        ),
    )


def simulated_fill_model_for_slippage_bps(slippage_bps: float | None) -> SimulatedFillModel:
    return simulated_fill_model_for_costs(slippage_bps=slippage_bps)


def simulated_fill_model_for_costs(
    *,
    slippage_bps: float | None = None,
    fee_model: str = "none",
) -> SimulatedFillModel:
    bps = float(slippage_bps or 0.0)
    fee_model_text = str(fee_model or "none").strip().lower()
    fee = KisFeeModel() if fee_model_text == "kis" else None
    if bps <= 0.0:
        return SimulatedFillModel(**({"fee_model": fee} if fee is not None else {}))
    return SimulatedFillModel(
        slippage_model=FixedBpsSlippageModel(bps=bps),
        **({"fee_model": fee} if fee is not None else {}),
    )


def run_framework_backtest(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    sleeve_id: str,
    framework_runner: FrameworkRunner,
    portfolio: Portfolio,
    start: datetime | None = None,
    end: datetime | None = None,
    warmup_start: datetime | None = None,
    indicator_engine: IndicatorEngine | None = None,
    refresh_history: bool = False,
    cycle_journal_store: CycleJournalStore | None = None,
    runtime_id: str = "framework-backtest",
    config_version: str = "",
    account_id: str | None = None,
    market_scope: str | None = None,
    fundamental_store: PointInTimeFundamentalStore | None = None,
    fundamental_names: tuple[str, ...] | None = None,
    alpha_symbols_by_model: Mapping[str, Iterable[Symbol | str]] | None = None,
    selection_models: tuple[UniverseSelectionModel, ...] = (),
    alpha_input_selections: Mapping[str, str] | None = None,
    fill_model: SimulatedFillModel | None = None,
    temporal_feature_provider: TemporalFeatureWindowProvider | None = None,
    cycle_journal_include_lineage: bool = True,
    daily_bar_time: dt_time | None = None,
) -> FrameworkBacktestResult:
    feed_started = perf_time.perf_counter()
    feed = build_replay_feed(
        provider,
        list(universe.symbols),
        start=warmup_start or start,
        end=end,
        refresh_history=refresh_history,
        daily_bar_time=daily_bar_time,
    )
    feed_build_ms = _perf_elapsed_ms(feed_started)
    indicator_engine = indicator_engine or IndicatorEngine()
    if sleeve_id not in indicator_engine.registries_by_sleeve:
        indicator_engine.register_universe(sleeve_id, universe)

    tracker = _SleeveBacktestTracker(
        sleeve_id=sleeve_id,
        initial_cash=portfolio.cash,
        initial_cash_by_currency=dict(portfolio.cash_by_currency),
    )
    orders: list[OrderIntent] = []
    order_tickets: list[OrderTicket] = []
    order_events: list[OrderEvent] = []
    order_collisions: list[OrderIntentCollision] = []
    framework_cycles: list[FrameworkCycleResult] = []
    selection_results: list[UniverseSelectionResult | CompositeUniverseSelectionResult] = []
    previous_live_symbols: tuple[Symbol, ...] = ()
    last_prices: dict[str, float] = {}
    coordinator = OrderCoordinator()
    fill_model = fill_model or SimulatedFillModel()
    warmup_data_slice_count = 0
    evaluated_data_slice_count = 0
    evaluated_start: datetime | None = None
    evaluated_end: datetime | None = None
    replay_stage_timings: dict[str, float] = {}

    replay_started = perf_time.perf_counter()
    for index, data in enumerate(feed, start=1):
        for bar in data.bars.values():
            last_prices[bar.symbol.key] = bar.close
        stage_started = perf_time.perf_counter()
        indicator_engine.on_data(data)
        _add_timing_ms(replay_stage_timings, "replay_indicator_update_ms", stage_started)
        if temporal_feature_provider is not None:
            stage_started = perf_time.perf_counter()
            temporal_feature_provider.update(data)
            _add_timing_ms(replay_stage_timings, "replay_temporal_update_ms", stage_started)
        if start is not None and data.time < start:
            warmup_data_slice_count += 1
            continue
        evaluated_start = data.time if evaluated_start is None else evaluated_start
        evaluated_end = data.time
        stage_started = perf_time.perf_counter()
        indicator_snapshot = indicator_engine.snapshot(
            sleeve_id,
            universe_id=universe.id,
            source_snapshot_id=f"backtest-{sleeve_id}-{index}",
            as_of=data.time,
            created_at=data.time,
            lane=normalize_snapshot_lane(data.resolution),
        )
        _add_timing_ms(replay_stage_timings, "replay_indicator_snapshot_ms", stage_started)
        stage_started = perf_time.perf_counter()
        indicator_snapshot = enrich_indicator_snapshot_with_temporal_features(
            indicator_snapshot,
            temporal_feature_provider,
        )
        _add_timing_ms(replay_stage_timings, "replay_temporal_enrich_ms", stage_started)
        evaluated_data_slice_count += 1
        stage_started = perf_time.perf_counter()
        fundamental_snapshot = _fundamental_snapshot(
            fundamental_store,
            sleeve_id=sleeve_id,
            universe=universe,
            as_of=data.time,
            names=fundamental_names,
            source_snapshot_id=f"backtest-{sleeve_id}-{index}",
        )
        _add_timing_ms(replay_stage_timings, "replay_fundamental_snapshot_ms", stage_started)
        stage_started = perf_time.perf_counter()
        selection_result = _select_backtest_universe(
            universe=universe,
            selection_models=selection_models,
            sleeve_id=sleeve_id,
            indicator_snapshot=indicator_snapshot,
            previous_live_symbols=previous_live_symbols,
            held_symbols=portfolio.held_symbols,
        )
        _add_timing_ms(replay_stage_timings, "replay_universe_selection_ms", stage_started)
        cycle_alpha_symbols = alpha_symbols_by_model
        if selection_result is not None:
            selection_results.append(selection_result)
            previous_live_symbols = selection_result.live_symbols
            cycle_alpha_symbols = _alpha_symbols_by_model_from_selection(
                selection_result,
                alpha_input_selections,
                fallback=alpha_symbols_by_model,
            )
        market_sessions = _market_sessions_for_backtest(universe, data)
        stage_started = perf_time.perf_counter()
        cycle = framework_runner.run_once(
            indicator_snapshot=indicator_snapshot,
            fundamental_snapshot=fundamental_snapshot,
            data=data,
            portfolio=portfolio,
            alpha_symbols_by_model=cycle_alpha_symbols,
            market_session=_primary_market_session(universe, market_sessions),
            market_sessions=market_sessions,
        )
        _add_timing_ms(replay_stage_timings, "replay_framework_runner_ms", stage_started)
        if cycle_journal_store is not None:
            stage_started = perf_time.perf_counter()
            cycle_journal_store.append(
                CycleJournalEntry.from_framework_cycle(
                    cycle,
                    runtime_id=runtime_id,
                    config_version=config_version,
                    account_id=account_id,
                    route_id=account_id,
                    market_scope=market_scope,
                    include_lineage=cycle_journal_include_lineage,
                )
            )
            _add_timing_ms(replay_stage_timings, "replay_journal_append_ms", stage_started)
        framework_cycles.append(cycle)
        orders.extend(cycle.order_intents)
        stage_started = perf_time.perf_counter()
        coordination = coordinator.coordinate((cycle.execution_batch,), generated_at=data.time)
        _add_timing_ms(replay_stage_timings, "replay_order_coordination_ms", stage_started)
        stage_started = perf_time.perf_counter()
        fill_events = fill_model.fill(coordination.tickets, occurred_at=data.time)
        _add_timing_ms(replay_stage_timings, "replay_fill_model_ms", stage_started)
        order_tickets.extend(coordination.tickets)
        order_events.extend(coordination.events)
        order_events.extend(fill_events)
        order_collisions.extend(coordination.collisions)
        stage_started = perf_time.perf_counter()
        for event in fill_events:
            tracker.record_fill_event(event)
            portfolio.apply_order_event(event)
        _add_timing_ms(replay_stage_timings, "replay_portfolio_apply_ms", stage_started)
        stage_started = perf_time.perf_counter()
        tracker.record_snapshot(data.time, portfolio.cash, dict(portfolio.cash_by_currency), portfolio.holdings, last_prices)
        _add_timing_ms(replay_stage_timings, "replay_snapshot_record_ms", stage_started)
    replay_ms = _perf_elapsed_ms(replay_started)

    return FrameworkBacktestResult(
        sleeve_id=sleeve_id,
        universe_id=universe.id,
        orders=orders,
        order_tickets=order_tickets,
        order_events=order_events,
        order_collisions=order_collisions,
        framework_cycles=framework_cycles,
        final_cash=portfolio.cash,
        final_cash_by_currency=dict(portfolio.cash_by_currency),
        final_equity_by_currency=tracker.snapshots[-1].equity_by_currency if tracker.snapshots else dict(portfolio.cash_by_currency),
        final_quantity={
            key: holding.quantity
            for key, holding in portfolio.holdings.items()
        },
        metrics=tracker.metrics(),
        metrics_by_currency=tracker.metrics_by_currency(),
        snapshots=tracker.snapshots,
        trades=tracker.closed_trades,
        selection_results=selection_results,
        data_slice_count=evaluated_data_slice_count,
        warmup_data_slice_count=warmup_data_slice_count,
        indicator_snapshot_count=len(framework_cycles),
        start=evaluated_start or start,
        end=evaluated_end or end,
        timings={
            "history_feed_build_ms": feed_build_ms,
            "framework_replay_wall_ms": replay_ms,
            **_rounded_timings(replay_stage_timings),
        },
    )


def run_framework_replay(
    feed: list[DataSlice],
    universe: UniverseDefinition,
    *,
    sleeve_id: str,
    framework_runner: FrameworkRunner,
    portfolio: Portfolio,
    indicator_engine: IndicatorEngine | None = None,
    fundamental_store: PointInTimeFundamentalStore | None = None,
    fundamental_names: tuple[str, ...] | None = None,
    alpha_symbols_by_model: Mapping[str, Iterable[Symbol | str]] | None = None,
    selection_models: tuple[UniverseSelectionModel, ...] = (),
    alpha_input_selections: Mapping[str, str] | None = None,
    fill_model: SimulatedFillModel | None = None,
    cycle_journal_store: CycleJournalStore | None = None,
    runtime_id: str = "framework-replay",
    config_version: str = "",
    account_id: str | None = None,
    market_scope: str | None = None,
    warmup_data_slice_count: int = 0,
    temporal_feature_provider: TemporalFeatureWindowProvider | None = None,
    cycle_journal_include_lineage: bool = True,
) -> FrameworkBacktestResult:
    replay_started = perf_time.perf_counter()
    indicator_engine = indicator_engine or IndicatorEngine()
    if sleeve_id not in indicator_engine.registries_by_sleeve:
        indicator_engine.register_universe(sleeve_id, universe)

    tracker = _SleeveBacktestTracker(
        sleeve_id=sleeve_id,
        initial_cash=portfolio.cash,
        initial_cash_by_currency=dict(portfolio.cash_by_currency),
    )
    orders: list[OrderIntent] = []
    order_tickets: list[OrderTicket] = []
    order_events: list[OrderEvent] = []
    order_collisions: list[OrderIntentCollision] = []
    framework_cycles: list[FrameworkCycleResult] = []
    selection_results: list[UniverseSelectionResult | CompositeUniverseSelectionResult] = []
    previous_live_symbols: tuple[Symbol, ...] = ()
    last_prices: dict[str, float] = {}
    coordinator = OrderCoordinator()
    fill_model = fill_model or SimulatedFillModel()
    replay_stage_timings: dict[str, float] = {}

    for index, data in enumerate(sorted(feed, key=lambda item: item.time), start=1):
        for bar in data.bars.values():
            last_prices[bar.symbol.key] = bar.close
        stage_started = perf_time.perf_counter()
        indicator_engine.on_data(data)
        _add_timing_ms(replay_stage_timings, "replay_indicator_update_ms", stage_started)
        if temporal_feature_provider is not None:
            stage_started = perf_time.perf_counter()
            temporal_feature_provider.update(data)
            _add_timing_ms(replay_stage_timings, "replay_temporal_update_ms", stage_started)
        stage_started = perf_time.perf_counter()
        indicator_snapshot = indicator_engine.snapshot(
            sleeve_id,
            universe_id=universe.id,
            source_snapshot_id=f"replay-{sleeve_id}-{index}",
            as_of=data.time,
            created_at=data.time,
            lane=normalize_snapshot_lane(data.resolution),
        )
        _add_timing_ms(replay_stage_timings, "replay_indicator_snapshot_ms", stage_started)
        stage_started = perf_time.perf_counter()
        indicator_snapshot = enrich_indicator_snapshot_with_temporal_features(
            indicator_snapshot,
            temporal_feature_provider,
        )
        _add_timing_ms(replay_stage_timings, "replay_temporal_enrich_ms", stage_started)
        stage_started = perf_time.perf_counter()
        fundamental_snapshot = _fundamental_snapshot(
            fundamental_store,
            sleeve_id=sleeve_id,
            universe=universe,
            as_of=data.time,
            names=fundamental_names,
            source_snapshot_id=f"replay-{sleeve_id}-{index}",
        )
        _add_timing_ms(replay_stage_timings, "replay_fundamental_snapshot_ms", stage_started)
        stage_started = perf_time.perf_counter()
        selection_result = _select_backtest_universe(
            universe=universe,
            selection_models=selection_models,
            sleeve_id=sleeve_id,
            indicator_snapshot=indicator_snapshot,
            previous_live_symbols=previous_live_symbols,
            held_symbols=portfolio.held_symbols,
        )
        _add_timing_ms(replay_stage_timings, "replay_universe_selection_ms", stage_started)
        cycle_alpha_symbols = alpha_symbols_by_model
        if selection_result is not None:
            selection_results.append(selection_result)
            previous_live_symbols = selection_result.live_symbols
            cycle_alpha_symbols = _alpha_symbols_by_model_from_selection(
                selection_result,
                alpha_input_selections,
                fallback=alpha_symbols_by_model,
            )
        market_sessions = _market_sessions_for_backtest(universe, data)
        stage_started = perf_time.perf_counter()
        cycle = framework_runner.run_once(
            indicator_snapshot=indicator_snapshot,
            fundamental_snapshot=fundamental_snapshot,
            data=data,
            portfolio=portfolio,
            alpha_symbols_by_model=cycle_alpha_symbols,
            market_session=_primary_market_session(universe, market_sessions),
            market_sessions=market_sessions,
        )
        _add_timing_ms(replay_stage_timings, "replay_framework_runner_ms", stage_started)
        if cycle_journal_store is not None:
            stage_started = perf_time.perf_counter()
            cycle_journal_store.append(
                CycleJournalEntry.from_framework_cycle(
                    cycle,
                    runtime_id=runtime_id,
                    config_version=config_version,
                    account_id=account_id,
                    route_id=account_id,
                    market_scope=market_scope,
                    include_lineage=cycle_journal_include_lineage,
                )
            )
            _add_timing_ms(replay_stage_timings, "replay_journal_append_ms", stage_started)
        framework_cycles.append(cycle)
        orders.extend(cycle.order_intents)
        stage_started = perf_time.perf_counter()
        coordination = coordinator.coordinate((cycle.execution_batch,), generated_at=data.time)
        _add_timing_ms(replay_stage_timings, "replay_order_coordination_ms", stage_started)
        stage_started = perf_time.perf_counter()
        fill_events = fill_model.fill(coordination.tickets, occurred_at=data.time)
        _add_timing_ms(replay_stage_timings, "replay_fill_model_ms", stage_started)
        order_tickets.extend(coordination.tickets)
        order_events.extend(coordination.events)
        order_events.extend(fill_events)
        order_collisions.extend(coordination.collisions)
        stage_started = perf_time.perf_counter()
        for event in fill_events:
            tracker.record_fill_event(event)
            portfolio.apply_order_event(event)
        _add_timing_ms(replay_stage_timings, "replay_portfolio_apply_ms", stage_started)
        stage_started = perf_time.perf_counter()
        tracker.record_snapshot(data.time, portfolio.cash, dict(portfolio.cash_by_currency), portfolio.holdings, last_prices)
        _add_timing_ms(replay_stage_timings, "replay_snapshot_record_ms", stage_started)
    replay_ms = _perf_elapsed_ms(replay_started)

    return FrameworkBacktestResult(
        sleeve_id=sleeve_id,
        universe_id=universe.id,
        orders=orders,
        order_tickets=order_tickets,
        order_events=order_events,
        order_collisions=order_collisions,
        framework_cycles=framework_cycles,
        final_cash=portfolio.cash,
        final_cash_by_currency=dict(portfolio.cash_by_currency),
        final_equity_by_currency=tracker.snapshots[-1].equity_by_currency if tracker.snapshots else dict(portfolio.cash_by_currency),
        final_quantity={
            key: holding.quantity
            for key, holding in portfolio.holdings.items()
        },
        metrics=tracker.metrics(),
        metrics_by_currency=tracker.metrics_by_currency(),
        snapshots=tracker.snapshots,
        trades=tracker.closed_trades,
        selection_results=selection_results,
        data_slice_count=len(feed),
        warmup_data_slice_count=warmup_data_slice_count,
        indicator_snapshot_count=len(framework_cycles),
        start=feed[0].time if feed else None,
        end=feed[-1].time if feed else None,
        timings={
            "framework_replay_wall_ms": replay_ms,
            **_rounded_timings(replay_stage_timings),
        },
    )


def run_backtest(
    engine: Engine,
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    fill_model: SimulatedFillModel | None = None,
) -> BacktestResult:
    feed = build_replay_feed(provider, symbols, start=start, end=end)
    engine.initialize()
    result_orders: list[OrderIntent] = []
    result_order_tickets: list[OrderTicket] = []
    result_order_events: list[OrderEvent] = []
    result_order_collisions: list[OrderIntentCollision] = []
    trackers = {
        sleeve.id: _SleeveBacktestTracker(
            sleeve_id=sleeve.id,
            initial_cash=sleeve.portfolio.cash,
            initial_cash_by_currency=dict(sleeve.portfolio.cash_by_currency),
        )
        for sleeve in engine.sleeves
    }
    sleeve_by_id = {sleeve.id: sleeve for sleeve in engine.sleeves}
    coordinator = OrderCoordinator()
    fill_model = fill_model or SimulatedFillModel()
    last_prices: dict[str, float] = {}
    for data in feed:
        for bar in data.bars.values():
            last_prices[bar.symbol.key] = bar.close
        batches: list[OrderIntentBatch] = []
        for sleeve in engine.sleeves:
            targets = sleeve.on_data(data)
            orders = engine.execution_model.create_orders(sleeve.id, sleeve.portfolio, data, targets)
            result_orders.extend(orders)
            if orders:
                batches.append(
                    OrderIntentBatch(
                        sleeve_id=sleeve.id,
                        generated_at=data.time,
                        order_intents=tuple(orders),
                        model_name=type(engine.execution_model).__name__,
                        reason="legacy_backtest_execution",
                    )
                )
        coordination = coordinator.coordinate(tuple(batches), generated_at=data.time)
        fill_events = fill_model.fill(coordination.tickets, occurred_at=data.time)
        result_order_tickets.extend(coordination.tickets)
        result_order_events.extend(coordination.events)
        result_order_events.extend(fill_events)
        result_order_collisions.extend(coordination.collisions)
        for event in fill_events:
            tracker = trackers[event.sleeve_id]
            tracker.record_fill_event(event)
            sleeve_by_id[event.sleeve_id].portfolio.apply_order_event(event)
        for sleeve in engine.sleeves:
            tracker = trackers[sleeve.id]
            tracker.record_snapshot(
                data.time,
                sleeve.portfolio.cash,
                dict(sleeve.portfolio.cash_by_currency),
                sleeve.portfolio.holdings,
                last_prices,
            )

    snapshots_by_sleeve = {
        sleeve_id: tracker.snapshots
        for sleeve_id, tracker in trackers.items()
    }
    trades_by_sleeve = {
        sleeve_id: tracker.closed_trades
        for sleeve_id, tracker in trackers.items()
    }
    metrics_by_sleeve = {
        sleeve_id: tracker.metrics()
        for sleeve_id, tracker in trackers.items()
    }
    return BacktestResult(
        orders=result_orders,
        order_tickets=result_order_tickets,
        order_events=result_order_events,
        order_collisions=result_order_collisions,
        final_cash_by_sleeve={sleeve.id: sleeve.portfolio.cash for sleeve in engine.sleeves},
        final_quantity_by_sleeve={
            sleeve.id: {
                key: holding.quantity
                for key, holding in sleeve.portfolio.holdings.items()
            }
            for sleeve in engine.sleeves
        },
        metrics=_aggregate_metrics(trackers),
        metrics_by_sleeve=metrics_by_sleeve,
        snapshots_by_sleeve=snapshots_by_sleeve,
        trades_by_sleeve=trades_by_sleeve,
    )


@dataclass(slots=True)
class _OpenLot:
    quantity: int
    price: float
    time: datetime


@dataclass(slots=True)
class _SleeveBacktestTracker:
    sleeve_id: str
    initial_cash: float
    initial_cash_by_currency: dict[str, float] = field(default_factory=dict)
    traded_notional: float = 0.0
    traded_notional_by_currency: dict[str, float] = field(default_factory=dict)
    slippage_cost: float = 0.0
    slippage_notional: float = 0.0
    slippage_cost_by_currency: dict[str, float] = field(default_factory=dict)
    slippage_notional_by_currency: dict[str, float] = field(default_factory=dict)
    fee_cost: float = 0.0
    fee_cost_by_currency: dict[str, float] = field(default_factory=dict)
    order_count: int = 0
    order_count_by_currency: dict[str, int] = field(default_factory=dict)
    lots_by_symbol: dict[str, list[_OpenLot]] = field(default_factory=dict)
    snapshots: list[BacktestSnapshot] = field(default_factory=list)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def record_fill(self, order: OrderIntent, time: datetime) -> None:
        self.order_count += 1
        self.traded_notional += order.notional
        currency = currency_for_symbol(order.symbol)
        self.traded_notional_by_currency[currency] = self.traded_notional_by_currency.get(currency, 0.0) + order.notional
        self.order_count_by_currency[currency] = self.order_count_by_currency.get(currency, 0) + 1
        if order.side is OrderSide.BUY:
            self.lots_by_symbol.setdefault(order.symbol.key, []).append(
                _OpenLot(quantity=order.quantity, price=order.reference_price, time=time)
            )
            return
        self._close_lots(order.symbol, order.quantity, order.reference_price, time)

    def record_fill_event(self, event: OrderEvent) -> None:
        if not event.is_fill or event.quantity <= 0 or event.fill_price is None:
            return
        self.order_count += 1
        self.traded_notional += event.notional
        currency = currency_for_symbol(event.symbol)
        self.traded_notional_by_currency[currency] = self.traded_notional_by_currency.get(currency, 0.0) + event.notional
        self.order_count_by_currency[currency] = self.order_count_by_currency.get(currency, 0) + 1
        slippage_cost, slippage_notional = _slippage_from_event(event)
        self.slippage_cost += slippage_cost
        self.slippage_notional += slippage_notional
        self.slippage_cost_by_currency[currency] = self.slippage_cost_by_currency.get(currency, 0.0) + slippage_cost
        self.slippage_notional_by_currency[currency] = self.slippage_notional_by_currency.get(currency, 0.0) + slippage_notional
        fee_cost = _safe_float(event.metadata.get("fee") if event.metadata else None) or 0.0
        self.fee_cost += fee_cost
        self.fee_cost_by_currency[currency] = self.fee_cost_by_currency.get(currency, 0.0) + fee_cost
        if event.side is OrderSide.BUY:
            self.lots_by_symbol.setdefault(event.symbol.key, []).append(
                _OpenLot(quantity=event.quantity, price=event.fill_price, time=event.occurred_at)
            )
            return
        self._close_lots(event.symbol, event.quantity, event.fill_price, event.occurred_at)

    def record_snapshot(
        self,
        time: datetime,
        cash: float,
        cash_by_currency: dict[str, float],
        holdings: dict[str, object],
        last_prices: dict[str, float],
    ) -> None:
        gross_exposure = 0.0
        gross_exposure_by_currency: dict[str, float] = {}
        for symbol_key, holding in holdings.items():
            price = last_prices.get(symbol_key)
            if price is None:
                continue
            value = abs(getattr(holding, "quantity")) * price
            gross_exposure += value
            symbol = getattr(holding, "symbol")
            currency = currency_for_symbol(symbol)
            gross_exposure_by_currency[currency] = gross_exposure_by_currency.get(currency, 0.0) + value
        currency_codes = set(cash_by_currency)
        currency_codes.update(gross_exposure_by_currency)
        equity_by_currency = {
            currency: cash_by_currency.get(currency, 0.0) + gross_exposure_by_currency.get(currency, 0.0)
            for currency in sorted(currency_codes)
        }
        self.snapshots.append(
            BacktestSnapshot(
                time=time,
                equity=cash + gross_exposure,
                cash=cash,
                gross_exposure=gross_exposure,
                equity_by_currency=equity_by_currency,
                cash_by_currency=dict(cash_by_currency),
                gross_exposure_by_currency=gross_exposure_by_currency,
            )
        )

    def metrics(self) -> BacktestMetrics:
        return _calculate_metrics(
            initial_equity=self.initial_cash,
            snapshots=self.snapshots,
            closed_trades=self.closed_trades,
            traded_notional=self.traded_notional,
            order_count=self.order_count,
            slippage_cost=self.slippage_cost,
            slippage_notional=self.slippage_notional,
            fee_cost=self.fee_cost,
        )

    def metrics_by_currency(self) -> dict[str, BacktestMetrics]:
        currencies = set(self.initial_cash_by_currency)
        for snapshot in self.snapshots:
            currencies.update(snapshot.equity_by_currency)
        reports: dict[str, BacktestMetrics] = {}
        for currency in sorted(currencies):
            initial_equity = float(self.initial_cash_by_currency.get(currency, 0.0))
            currency_snapshots = [
                BacktestSnapshot(
                    time=snapshot.time,
                    equity=float(snapshot.equity_by_currency.get(currency, 0.0)),
                    cash=float(snapshot.cash_by_currency.get(currency, 0.0)),
                    gross_exposure=float(snapshot.gross_exposure_by_currency.get(currency, 0.0)),
                )
                for snapshot in self.snapshots
                if currency in snapshot.equity_by_currency
            ]
            reports[currency] = _calculate_metrics(
                initial_equity=initial_equity,
                snapshots=currency_snapshots,
                closed_trades=[
                    trade for trade in self.closed_trades if currency_for_symbol(trade.symbol) == currency
                ],
                traded_notional=float(self.traded_notional_by_currency.get(currency, 0.0)),
                order_count=int(self.order_count_by_currency.get(currency, 0)),
                slippage_cost=float(self.slippage_cost_by_currency.get(currency, 0.0)),
                slippage_notional=float(self.slippage_notional_by_currency.get(currency, 0.0)),
                fee_cost=float(self.fee_cost_by_currency.get(currency, 0.0)),
            )
        return reports

    def _close_lots(self, symbol: Symbol, quantity: int, price: float, time: datetime) -> None:
        remaining = quantity
        lots = self.lots_by_symbol.get(symbol.key, [])
        total_cost = 0.0
        total_holding_days = 0.0
        closed_quantity = 0
        entry_time: datetime | None = None
        while remaining > 0 and lots:
            lot = lots[0]
            matched_quantity = min(remaining, lot.quantity)
            total_cost += matched_quantity * lot.price
            total_holding_days += matched_quantity * max(0.0, (time - lot.time).total_seconds() / 86400.0)
            closed_quantity += matched_quantity
            entry_time = lot.time if entry_time is None else min(entry_time, lot.time)
            remaining -= matched_quantity
            lot.quantity -= matched_quantity
            if lot.quantity == 0:
                lots.pop(0)
        if not lots:
            self.lots_by_symbol.pop(symbol.key, None)
        if closed_quantity == 0:
            return
        average_entry_price = total_cost / closed_quantity
        average_holding_days = total_holding_days / closed_quantity
        self.closed_trades.append(
            ClosedTrade(
                sleeve_id=self.sleeve_id,
                symbol=symbol,
                entry_time=entry_time or time,
                exit_time=time,
                quantity=closed_quantity,
                average_entry_price=average_entry_price,
                exit_price=price,
                pnl=(price * closed_quantity) - total_cost,
                holding_days=average_holding_days,
            )
        )


def _calculate_metrics(
    *,
    initial_equity: float,
    snapshots: list[BacktestSnapshot],
    closed_trades: list[ClosedTrade],
    traded_notional: float,
    order_count: int,
    slippage_cost: float = 0.0,
    slippage_notional: float = 0.0,
    fee_cost: float = 0.0,
) -> BacktestMetrics:
    final_equity = snapshots[-1].equity if snapshots else initial_equity
    total_return = (final_equity / initial_equity) - 1.0 if initial_equity > 0 else 0.0
    slippage_bps = (slippage_cost / slippage_notional) * 10_000.0 if slippage_notional > 0 else 0.0
    return BacktestMetrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        total_return=total_return,
        cagr=_cagr(initial_equity, final_equity, snapshots),
        sharpe=_sharpe(snapshots),
        mdd=_max_drawdown(snapshots),
        turnover=_turnover(traded_notional, snapshots, initial_equity),
        avg_holding_days=_avg_holding_days(closed_trades),
        avg_exposure=_avg_exposure(snapshots),
        win_rate=_win_rate(closed_trades),
        trade_count=len(closed_trades),
        order_count=order_count,
        slippage_cost=slippage_cost,
        slippage_bps=slippage_bps,
        fee_cost=fee_cost,
        total_friction_cost=slippage_cost + fee_cost,
    )


def _slippage_from_event(event: OrderEvent) -> tuple[float, float]:
    metadata = dict(event.metadata or {})
    reference_price = _safe_float(metadata.get("reference_price"))
    if reference_price is None or reference_price <= 0:
        return 0.0, 0.0
    quantity = max(int(event.quantity), 0)
    reference_notional = reference_price * quantity
    metadata_cost = _safe_float(metadata.get("slippage_cost"))
    if metadata_cost is not None:
        return metadata_cost, reference_notional
    if event.fill_price is None:
        return 0.0, reference_notional
    if event.side is OrderSide.BUY:
        per_share = event.fill_price - reference_price
    else:
        per_share = reference_price - event.fill_price
    return per_share * quantity, reference_notional


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _metric_currency_codes(
    cash_by_currency: Mapping[str, float],
    equity_by_currency: Mapping[str, float],
    metrics_by_currency: Mapping[str, BacktestMetrics],
) -> tuple[str, ...]:
    codes = {
        str(currency).strip().upper()
        for source in (cash_by_currency, equity_by_currency, metrics_by_currency)
        for currency in source
        if str(currency).strip()
    }
    return tuple(sorted(codes))


def _metrics_report_for_currencies(metrics: BacktestMetrics, currencies: tuple[str, ...]) -> dict[str, float | int | str | bool]:
    if len(currencies) == 1:
        return metrics.to_report(
            currency=currencies[0],
            currency_mode="single_currency",
            valid_without_fx=True,
        )
    if len(currencies) > 1:
        return metrics.to_report(
            currency_mode="multi_currency_native_sum",
            valid_without_fx=False,
        )
    return metrics.to_report(currency_mode="no_currency", valid_without_fx=True)


def _aggregate_metrics(trackers: dict[str, _SleeveBacktestTracker]) -> BacktestMetrics:
    initial_equity = sum(tracker.initial_cash for tracker in trackers.values())
    traded_notional = sum(tracker.traded_notional for tracker in trackers.values())
    slippage_cost = sum(tracker.slippage_cost for tracker in trackers.values())
    slippage_notional = sum(tracker.slippage_notional for tracker in trackers.values())
    fee_cost = sum(tracker.fee_cost for tracker in trackers.values())
    order_count = sum(tracker.order_count for tracker in trackers.values())
    closed_trades = [
        trade
        for tracker in trackers.values()
        for trade in tracker.closed_trades
    ]
    snapshots_by_time: dict[datetime, list[BacktestSnapshot]] = {}
    for tracker in trackers.values():
        for snapshot in tracker.snapshots:
            snapshots_by_time.setdefault(snapshot.time, []).append(snapshot)
    snapshots = [
        BacktestSnapshot(
            time=time,
            equity=sum(snapshot.equity for snapshot in snapshots),
            cash=sum(snapshot.cash for snapshot in snapshots),
            gross_exposure=sum(snapshot.gross_exposure for snapshot in snapshots),
        )
        for time, snapshots in sorted(snapshots_by_time.items())
    ]
    return _calculate_metrics(
        initial_equity=initial_equity,
        snapshots=snapshots,
        closed_trades=closed_trades,
        traded_notional=traded_notional,
        order_count=order_count,
        slippage_cost=slippage_cost,
        slippage_notional=slippage_notional,
        fee_cost=fee_cost,
    )


def _cagr(initial_equity: float, final_equity: float, snapshots: list[BacktestSnapshot]) -> float:
    if initial_equity <= 0 or final_equity <= 0 or len(snapshots) < 2:
        return 0.0
    days = (snapshots[-1].time - snapshots[0].time).total_seconds() / 86400.0
    if days <= 0:
        return 0.0
    return (final_equity / initial_equity) ** (365.25 / days) - 1.0


def _sharpe(snapshots: list[BacktestSnapshot]) -> float:
    returns = [
        (current.equity / previous.equity) - 1.0
        for previous, current in zip(snapshots, snapshots[1:])
        if previous.equity > 0
    ]
    if len(returns) < 2:
        return 0.0
    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    standard_deviation = math.sqrt(variance)
    return 0.0 if standard_deviation == 0 else (average / standard_deviation) * math.sqrt(252.0)


def _max_drawdown(snapshots: list[BacktestSnapshot]) -> float:
    peak = 0.0
    max_drawdown = 0.0
    for snapshot in snapshots:
        peak = max(peak, snapshot.equity)
        if peak <= 0:
            continue
        drawdown = (peak - snapshot.equity) / peak
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _turnover(traded_notional: float, snapshots: list[BacktestSnapshot], initial_equity: float) -> float:
    denominator = _average([snapshot.equity for snapshot in snapshots]) if snapshots else initial_equity
    return traded_notional / denominator if denominator > 0 else 0.0


def _avg_holding_days(closed_trades: list[ClosedTrade]) -> float:
    return _average([trade.holding_days for trade in closed_trades])


def _avg_exposure(snapshots: list[BacktestSnapshot]) -> float:
    return _average([snapshot.exposure for snapshot in snapshots])


def _win_rate(closed_trades: list[ClosedTrade]) -> float:
    return sum(1 for trade in closed_trades if trade.pnl > 0) / len(closed_trades) if closed_trades else 0.0


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _order_to_report(order: OrderIntent) -> dict[str, object]:
    return {
        "sleeve_id": order.sleeve_id,
        "symbol": order.symbol.key,
        "side": order.side.value,
        "quantity": order.quantity,
        "reference_price": order.reference_price,
        "notional": order.notional,
        "tag": order.tag,
    }


def _insight_ledger_report(framework_cycles: list[FrameworkCycleResult]) -> dict[str, object]:
    return {
        "cycle_count": len(framework_cycles),
        "insight_count": sum(cycle.new_insight_batch.insight_count for cycle in framework_cycles),
        "cycles": [
            {
                "cycle_index": index,
                "sleeve_id": cycle.sleeve_id,
                "source_snapshot_id": cycle.source_snapshot_id,
                "indicator_snapshot_id": cycle.indicator_snapshot_id,
                "generated_at": cycle.new_insight_batch.generated_at.isoformat(),
                "alpha_ids": list(cycle.new_insight_batch.alpha_ids),
                "new_insight_count": cycle.new_insight_batch.insight_count,
                "active_insight_count": cycle.active_insight_count,
                "insight_manager_update": cycle.insight_manager_update.to_dict(),
                "new_insights": [
                    insight.to_dict()
                    for insight in cycle.new_insight_batch.insights
                ],
                "active_insights": [
                    insight.to_dict()
                    for insight in cycle.active_insights
                ],
            }
            for index, cycle in enumerate(framework_cycles)
        ],
    }


def _select_backtest_universe(
    *,
    universe: UniverseDefinition,
    selection_models: tuple[UniverseSelectionModel, ...],
    sleeve_id: str,
    indicator_snapshot,
    previous_live_symbols: tuple[Symbol, ...],
    held_symbols: tuple[Symbol, ...],
) -> UniverseSelectionResult | CompositeUniverseSelectionResult | None:
    if not selection_models:
        return None
    context = UniverseSelectionContext(
        sleeve_id=sleeve_id,
        universe=universe,
        indicator_snapshot=indicator_snapshot,
        previous_live_symbols=previous_live_symbols,
        held_symbols=held_symbols,
    )
    if len(selection_models) == 1:
        return selection_models[0].select(context)
    return build_composite_universe_selection_result(
        context,
        tuple(model.select(context) for model in selection_models),
    )


def _market_sessions_for_backtest(universe: UniverseDefinition, data: DataSlice) -> dict[str, Any]:
    resolution = str(data.resolution or "").strip().lower()
    if resolution not in _SESSION_AWARE_BACKTEST_RESOLUTIONS:
        return {}

    scopes = {
        market_scope_for_symbol(bar.symbol)
        for bar in data.bars.values()
    }
    if not scopes:
        scopes.add(market_scope_from_market(universe.market))
    return {
        scope: _synthetic_market_session_for_backtest(scope, data.time)
        for scope in sorted(scopes)
    }


def _primary_market_session(universe: UniverseDefinition, sessions: Mapping[str, Any]):
    if not sessions:
        return None
    primary_scope = market_scope_from_market(universe.market)
    if primary_scope in sessions:
        return sessions[primary_scope]
    if "domestic" in sessions:
        return sessions["domestic"]
    return next(iter(sessions.values()))


def _synthetic_market_session_for_backtest(market_scope: str, when: datetime):
    scope = str(market_scope or "").strip().lower()
    if scope == "overseas":
        return synthetic_us_market_session(_with_default_timezone(when, ZoneInfo("America/New_York")))
    return synthetic_domestic_market_session(_with_default_timezone(when, ZoneInfo("Asia/Seoul")))


def _with_default_timezone(when: datetime, timezone: ZoneInfo) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone)
    return when.astimezone(timezone)


def _alpha_symbols_by_model_from_selection(
    selection: UniverseSelectionResult | CompositeUniverseSelectionResult,
    alpha_input_selections: Mapping[str, str] | None,
    *,
    fallback: Mapping[str, Iterable[Symbol | str]] | None,
) -> Mapping[str, Iterable[Symbol | str]] | None:
    if not alpha_input_selections:
        return fallback
    selected: dict[str, tuple[Symbol, ...]] = {}
    for alpha_id, selection_id in alpha_input_selections.items():
        selected[alpha_id] = _symbols_for_selection(selection, selection_id)
    return selected


def _symbols_for_selection(
    selection: UniverseSelectionResult | CompositeUniverseSelectionResult,
    selection_id: str,
) -> tuple[Symbol, ...]:
    if isinstance(selection, CompositeUniverseSelectionResult):
        if selection_id not in selection.selections:
            raise ValueError(f"Unknown alpha input selection_id: {selection_id}")
        return selection.symbols_for_selection(selection_id)
    if selection.selection_id != selection_id:
        raise ValueError(f"Unknown alpha input selection_id: {selection_id}")
    return selection.selected_symbols


def _selection_report(
    selection_results: list[UniverseSelectionResult | CompositeUniverseSelectionResult],
    *,
    include_details: bool,
) -> dict[str, object]:
    last = selection_results[-1]
    selection_ids = (
        list(last.selections)
        if isinstance(last, CompositeUniverseSelectionResult)
        else [last.selection_id]
    )
    payload: dict[str, object] = {
        "cycle_count": len(selection_results),
        "selection_ids": selection_ids,
        "last_selected_count": len(last.selected_symbols),
        "last_forced_count": len(last.forced_symbols),
        "last_live_count": len(last.live_symbols),
        "last_live_symbols": [symbol.key for symbol in last.live_symbols],
    }
    if include_details:
        payload["cycles"] = [
            selection.to_dict(include_candidates=False)
            for selection in selection_results
        ]
    return payload


def _fundamental_snapshot(
    store: PointInTimeFundamentalStore | None,
    *,
    sleeve_id: str,
    universe: UniverseDefinition,
    as_of: datetime,
    names: tuple[str, ...] | None,
    source_snapshot_id: str,
):
    if store is None:
        return None
    return store.snapshot(
        sleeve_id=sleeve_id,
        universe_id=universe.id,
        symbols=universe.symbols,
        as_of=as_of,
        names=names,
        source_snapshot_id=source_snapshot_id,
        created_at=as_of,
    )


def _load_minute_rows(path: Path) -> list[Mapping[str, Any]]:
    return list(_iter_minute_rows(path))


def _iter_minute_rows(path: Path):
    suffix = path.suffix.lower()
    if suffix == ".csv" or path.name.lower().endswith(".csv.gz"):
        opener = gzip.open if path.name.lower().endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                yield dict(row)
        return
    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    item = json.loads(text)
                    if not isinstance(item, Mapping):
                        raise ValueError(f"Minute replay JSONL rows must be objects: {path}")
                    yield item
        return
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, Mapping):
            for key in ("bars", "rows", "candles", "data", "output", "output2"):
                items = payload.get(key)
                if isinstance(items, list):
                    for item in items:
                        yield _ensure_row_mapping(item, path)
                    return
            raise ValueError(f"Minute replay JSON object must contain bars/rows/candles: {path}")
        if isinstance(payload, list):
            for item in payload:
                yield _ensure_row_mapping(item, path)
            return
    raise ValueError(f"Unsupported minute replay feed format: {path}")


def _ensure_row_mapping(item: Any, path: Path) -> Mapping[str, Any]:
    if not isinstance(item, Mapping):
        raise ValueError(f"Minute replay rows must be objects: {path}")
    return item


def _read_compiled_minute_replay_payload(path: Path) -> Mapping[str, Any]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, Mapping):
        raise ValueError(f"Compiled minute replay cache must be a JSON object: {path}")
    return payload


def _write_compiled_minute_replay_payload(path: Path, payload: Mapping[str, Any]) -> None:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8", newline="") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), default=str)


def _compiled_minute_bar_to_payload(bar: Bar) -> dict[str, object]:
    return _bar_to_cache_payload(bar)


def _bar_to_cache_payload(bar: Bar) -> dict[str, object]:
    return {
        "symbol": bar.symbol.key,
        "time": bar.time.replace(tzinfo=None).isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": int(bar.volume),
        "resolution": str(bar.resolution),
        "metadata": dict(bar.metadata),
    }


def _compiled_minute_bar_from_payload(item: Mapping[str, Any], *, default_time: datetime) -> Bar:
    return _bar_from_cache_payload(item, default_resolution=DataResolution.MINUTE.value, default_time=default_time)


def _bar_from_cache_payload(
    item: Mapping[str, Any],
    *,
    default_resolution: str,
    default_time: datetime | None = None,
) -> Bar:
    symbol = _minute_row_symbol(item, default_market="KRX")
    bar_time = _parse_optional_datetime(item.get("time")) or default_time
    if bar_time is None:
        raise ValueError("Cached bar requires time.")
    return Bar(
        symbol=symbol,
        time=bar_time,
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=int(float(item.get("volume") or 0)),
        resolution=str(item.get("resolution") or default_resolution),
        metadata=dict(item.get("metadata") or {}),
    )


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(str(value))


def _minute_row_to_bar(row: Mapping[str, Any], *, default_market: str) -> Bar:
    symbol = _minute_row_symbol(row, default_market=default_market)
    close = _required_row_float(row, ("close", "close_price", "last_price", "stck_prpr", "stck_clpr"))
    bar_time = _minute_row_time(row)
    return Bar(
        symbol=symbol,
        time=bar_time,
        open=_optional_row_float(row, ("open", "open_price", "stck_oprc"), default=close),
        high=_optional_row_float(row, ("high", "high_price", "stck_hgpr"), default=close),
        low=_optional_row_float(row, ("low", "low_price", "stck_lwpr"), default=close),
        close=close,
        volume=_optional_row_int(row, ("volume", "vol", "cntg_vol", "acml_vol"), default=0),
        resolution=DataResolution.MINUTE.value,
        metadata=_minute_row_session_metadata(row, symbol=symbol),
    )


def _minute_row_symbol(row: Mapping[str, Any], *, default_market: str) -> Symbol:
    raw_symbol = _first_row_text(row, ("symbol_key", "symbol", "ticker", "code", "stock_code"))
    if not raw_symbol:
        raise ValueError("Minute replay row requires symbol, ticker, code, or stock_code.")
    if ":" in raw_symbol:
        market, ticker = raw_symbol.split(":", 1)
        return Symbol(ticker=ticker.strip().upper(), market=market.strip().upper())
    market = _first_row_text(row, ("market", "market_code")) or default_market
    return Symbol(ticker=raw_symbol.strip().upper(), market=market.strip().upper())


def _minute_row_time(row: Mapping[str, Any]) -> datetime:
    raw = _first_row_text(row, ("datetime", "timestamp", "time_iso", "bar_time"))
    if raw:
        return _parse_datetime(raw)
    date_text = _first_row_text(row, ("date", "trade_date", "stck_bsop_date"))
    time_text = _first_row_text(row, ("time", "hhmmss", "stck_cntg_hour"))
    if not date_text or not time_text:
        fallback = _first_row_text(row, ("time",))
        if fallback:
            return _parse_datetime(fallback)
        raise ValueError("Minute replay row requires datetime/timestamp or date + time.")
    normalized_date = date_text.replace("-", "").replace("/", "").strip()
    normalized_time = time_text.replace(":", "").strip()
    if len(normalized_time) == 4:
        normalized_time += "00"
    if len(normalized_date) != 8 or len(normalized_time) != 6:
        raise ValueError(f"Invalid minute replay date/time: {date_text} {time_text}")
    return datetime.strptime(f"{normalized_date}{normalized_time}", "%Y%m%d%H%M%S")


def _first_row_text(row: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _minute_row_session_metadata(row: Mapping[str, Any], *, symbol: Symbol) -> dict[str, Any]:
    phase = _first_row_text(row, ("market_session_phase", "session_phase", "session"))
    scope = _first_row_text(row, ("market_session_scope", "market_scope")) or _minute_symbol_scope(symbol)
    if not phase and not any(name in row for name in ("is_regular_market_open", "is_orderable_session", "is_extended_market_hours", "session_source")):
        return {}
    metadata: dict[str, Any] = {
        "market_session_scope": scope,
        "market_session_phase": phase,
    }
    for key in ("is_regular_market_open", "is_orderable_session", "is_extended_market_hours"):
        if key in row and row[key] not in (None, ""):
            metadata[key] = _row_bool(row[key])
    source = _first_row_text(row, ("session_source",))
    if source:
        metadata["session_source"] = source
    return metadata


def _minute_symbol_scope(symbol: Symbol) -> str:
    return "overseas" if symbol.market.upper() in {"US", "NAS", "NYS", "NYSE", "NASDAQ", "AMEX", "AMS"} else "domestic"


def _row_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _required_row_float(row: Mapping[str, Any], names: tuple[str, ...]) -> float:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return float(value)
    raise ValueError(f"Minute replay row requires one of: {', '.join(names)}")


def _optional_row_float(row: Mapping[str, Any], names: tuple[str, ...], *, default: float) -> float:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return float(value)
    return default


def _optional_row_int(row: Mapping[str, Any], names: tuple[str, ...], *, default: int) -> int:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return int(float(value))
    return default


def _perf_elapsed_ms(started: float) -> float:
    return round((perf_time.perf_counter() - started) * 1000.0, 3)


def _add_timing_ms(bucket: dict[str, float], key: str, started: float) -> None:
    bucket[key] = bucket.get(key, 0.0) + ((perf_time.perf_counter() - started) * 1000.0)


def _rounded_timings(bucket: Mapping[str, float]) -> dict[str, float]:
    return {key: round(value, 3) for key, value in bucket.items()}


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)

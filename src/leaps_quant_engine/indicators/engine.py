from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from leaps_quant_engine.indicators.factory import create_indicator
from leaps_quant_engine.indicators.registry import IndicatorRegistry
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue, SnapshotQualityReport
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(slots=True)
class IndicatorEngine:
    registries_by_sleeve: dict[str, IndicatorRegistry] = field(default_factory=dict)
    active_symbols_by_sleeve: dict[str, set[str]] = field(default_factory=dict)
    sleeves_by_symbol: dict[str, set[str]] = field(default_factory=dict)

    def register_universe(self, sleeve_id: str, universe: UniverseDefinition) -> None:
        registry = self.registries_by_sleeve.setdefault(sleeve_id, IndicatorRegistry())
        active_symbols = self.active_symbols_by_sleeve.setdefault(sleeve_id, set())
        for symbol in universe.symbols:
            active_symbols.add(symbol.key)
            self.sleeves_by_symbol.setdefault(symbol.key, set()).add(sleeve_id)
            for definition in universe.indicators:
                registry.add(symbol, create_indicator(definition))

    def warm_up(self, sleeve_id: str, bars: list[Bar]) -> None:
        registry = self._registry(sleeve_id)
        active_symbols = self.active_symbols_by_sleeve.get(sleeve_id, set())
        registry.update_many([bar for bar in bars if bar.symbol.key in active_symbols])

    def warm_up_from_provider(
        self,
        sleeve_id: str,
        provider: MarketDataProvider,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        bars: list[Bar] = []
        for symbol in self.symbols_for_sleeve(sleeve_id):
            bars.extend(provider.get_history(symbol, start=start, end=end))
        self.warm_up(sleeve_id, bars)

    def on_data(self, data: DataSlice) -> None:
        for bar in data.bars.values():
            for sleeve_id in self.sleeves_by_symbol.get(bar.symbol.key, set()):
                self._registry(sleeve_id).update(bar)

    def update_from_provider(self, provider: MarketDataProvider) -> DataSlice:
        bars: dict[str, Bar] = {}
        for symbol in self.active_symbols():
            bar = provider.get_latest_bar(symbol)
            bars[bar.symbol.key] = bar
        data = DataSlice(
            time=max((bar.time for bar in bars.values()), default=datetime.now()),
            bars=bars,
        )
        self.on_data(data)
        return data

    def value(self, sleeve_id: str, symbol: Symbol, name: str) -> float | None:
        indicator = self._registry(sleeve_id).get(symbol, name)
        if not indicator.is_ready or indicator.current is None:
            return None
        return indicator.current.value

    def is_ready(self, sleeve_id: str, symbol: Symbol, name: str) -> bool:
        return self._registry(sleeve_id).get(symbol, name).is_ready

    def ready_values(self, sleeve_id: str, symbol: Symbol) -> dict[str, float]:
        return self._registry(sleeve_id).ready_values(symbol)

    def values_for(
        self,
        sleeve_id: str,
        symbols: list[Symbol],
        names: list[str] | None = None,
        *,
        ready_only: bool = True,
    ) -> dict[str, dict[str, float | None]]:
        result: dict[str, dict[str, float | None]] = {}
        registry = self._registry(sleeve_id)
        for symbol in symbols:
            values: dict[str, float | None] = {}
            symbol_indicators = registry.indicators_for(symbol)
            selected_names = names or list(symbol_indicators)
            for name in selected_names:
                indicator = symbol_indicators.get(name)
                if indicator is None:
                    values[name] = None
                    continue
                if ready_only and not indicator.is_ready:
                    values[name] = None
                    continue
                values[name] = indicator.current.value if indicator.current is not None else None
            result[symbol.key] = values
        return result

    def snapshot(
        self,
        sleeve_id: str,
        *,
        universe_id: str | None = None,
        source_snapshot_id: str | None = None,
        as_of: datetime | None = None,
        created_at: datetime | None = None,
        quality_report: SnapshotQualityReport | None = None,
    ) -> IndicatorSnapshot:
        registry = self._registry(sleeve_id)
        symbols = self.symbols_for_sleeve(sleeve_id)
        values: dict[str, dict[str, IndicatorValue]] = {}
        for symbol in symbols:
            values[symbol.key] = {}
            for name, indicator in registry.indicators_for(symbol).items():
                values[symbol.key][name] = IndicatorValue(
                    name=name,
                    value=indicator.current.value if indicator.current is not None else None,
                    is_ready=indicator.is_ready,
                    samples=indicator.samples,
                    time=indicator.current.time if indicator.current is not None else None,
                )
        return IndicatorSnapshot(
            snapshot_id=f"indicator-{uuid4()}",
            sleeve_id=sleeve_id,
            universe_id=universe_id,
            source_snapshot_id=source_snapshot_id,
            as_of=as_of or _latest_indicator_time(values) or datetime.now(),
            created_at=created_at or datetime.now(),
            symbols=tuple(symbol.key for symbol in symbols),
            values=values,
            quality_report=quality_report,
        )

    def symbols_for_sleeve(self, sleeve_id: str) -> list[Symbol]:
        self._registry(sleeve_id)
        return [_symbol_from_key(key) for key in sorted(self.active_symbols_by_sleeve.get(sleeve_id, set()))]

    def active_symbols(self) -> list[Symbol]:
        return [_symbol_from_key(key) for key in sorted(self.sleeves_by_symbol)]

    def _registry(self, sleeve_id: str) -> IndicatorRegistry:
        try:
            return self.registries_by_sleeve[sleeve_id]
        except KeyError as exc:
            raise KeyError(f"Unknown sleeve_id: {sleeve_id}") from exc


def _symbol_from_key(key: str) -> Symbol:
    market, ticker = key.split(":", 1)
    return Symbol(ticker=ticker, market=market)


def _latest_indicator_time(values: dict[str, dict[str, IndicatorValue]]) -> datetime | None:
    times = [
        indicator_value.time
        for symbol_values in values.values()
        for indicator_value in symbol_values.values()
        if indicator_value.time is not None
    ]
    return max(times, default=None)

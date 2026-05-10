from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from leaps_quant_engine.indicators.core import Indicator
from leaps_quant_engine.models import Bar, Symbol


@dataclass(slots=True)
class IndicatorRegistry:
    _indicators: dict[str, dict[str, Indicator]] = field(default_factory=lambda: defaultdict(dict))
    _indicator_resolutions: dict[str, dict[str, str]] = field(default_factory=lambda: defaultdict(dict))

    def add(self, symbol: Symbol, indicator: Indicator, *, resolution: str = "any") -> Indicator:
        self._indicators[symbol.key][indicator.name] = indicator
        self._indicator_resolutions[symbol.key][indicator.name] = _normalize_resolution(resolution)
        return indicator

    def get(self, symbol: Symbol, name: str) -> Indicator:
        return self._indicators[symbol.key][name]

    def indicators_for(self, symbol: Symbol) -> dict[str, Indicator]:
        return dict(self._indicators.get(symbol.key, {}))

    def resolution_for(self, symbol: Symbol, name: str) -> str:
        return self._indicator_resolutions.get(symbol.key, {}).get(name, "any")

    def update(self, bar: Bar) -> None:
        for name, indicator in self._indicators.get(bar.symbol.key, {}).items():
            indicator_resolution = self._indicator_resolutions.get(bar.symbol.key, {}).get(name, "any")
            if not _resolution_matches(indicator_resolution, bar.resolution):
                continue
            indicator.update(bar)

    def update_many(self, bars: list[Bar]) -> None:
        for bar in sorted(bars, key=lambda item: item.time):
            self.update(bar)

    def ready_values(self, symbol: Symbol) -> dict[str, float]:
        return {
            name: indicator.current.value
            for name, indicator in self._indicators.get(symbol.key, {}).items()
            if indicator.is_ready and indicator.current is not None
        }


def _normalize_resolution(value: str | None) -> str:
    normalized = str(value or "any").strip().lower()
    return normalized or "any"


def _resolution_matches(indicator_resolution: str | None, bar_resolution: str | None) -> bool:
    indicator = _normalize_resolution(indicator_resolution)
    bar = _normalize_resolution(bar_resolution)
    if indicator in {"any", "*"}:
        return True
    if bar in {"any", "*", "unknown"}:
        return True
    if indicator in {"daily", "daily_confirmed"}:
        return bar in {"daily", "daily_confirmed"}
    if indicator in {"live", "quote"}:
        return bar in {"live", "quote", "minute", "intraday", "second", "tick"}
    if indicator in {"minute", "intraday"}:
        return bar in {"minute", "intraday"}
    return indicator == bar

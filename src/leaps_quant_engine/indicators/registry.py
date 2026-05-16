from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from leaps_quant_engine.indicators.core import Indicator
from leaps_quant_engine.models import Bar, Symbol


@dataclass(slots=True)
class IndicatorUpdateReport:
    updated_count: int = 0
    resolution_mismatch_count: int = 0

    def combine(self, other: "IndicatorUpdateReport") -> "IndicatorUpdateReport":
        return IndicatorUpdateReport(
            updated_count=self.updated_count + other.updated_count,
            resolution_mismatch_count=self.resolution_mismatch_count + other.resolution_mismatch_count,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "updated_count": self.updated_count,
            "resolution_mismatch_count": self.resolution_mismatch_count,
        }


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

    def update(self, bar: Bar) -> IndicatorUpdateReport:
        updated_count = 0
        resolution_mismatch_count = 0
        for name, indicator in self._indicators.get(bar.symbol.key, {}).items():
            indicator_resolution = self._indicator_resolutions.get(bar.symbol.key, {}).get(name, "any")
            if not _resolution_matches(indicator_resolution, bar.resolution):
                resolution_mismatch_count += 1
                continue
            indicator.update(bar)
            updated_count += 1
        return IndicatorUpdateReport(
            updated_count=updated_count,
            resolution_mismatch_count=resolution_mismatch_count,
        )

    def update_many(self, bars: list[Bar]) -> IndicatorUpdateReport:
        report = IndicatorUpdateReport()
        for bar in sorted(bars, key=lambda item: item.time):
            report = report.combine(self.update(bar))
        return report

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
        return False
    if indicator in {"daily", "daily_confirmed"}:
        return bar in {"daily", "daily_confirmed"}
    if indicator in {"live", "quote"}:
        return bar in {"live", "quote", "minute", "intraday", "second", "tick"}
    if indicator in {"minute", "intraday"}:
        return bar in {"minute", "intraday"}
    return indicator == bar

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from leaps_quant_engine.indicators.core import Indicator
from leaps_quant_engine.models import Bar, Symbol


@dataclass(slots=True)
class IndicatorRegistry:
    _indicators: dict[str, dict[str, Indicator]] = field(default_factory=lambda: defaultdict(dict))

    def add(self, symbol: Symbol, indicator: Indicator) -> Indicator:
        self._indicators[symbol.key][indicator.name] = indicator
        return indicator

    def get(self, symbol: Symbol, name: str) -> Indicator:
        return self._indicators[symbol.key][name]

    def indicators_for(self, symbol: Symbol) -> dict[str, Indicator]:
        return dict(self._indicators.get(symbol.key, {}))

    def update(self, bar: Bar) -> None:
        for indicator in self._indicators.get(bar.symbol.key, {}).values():
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

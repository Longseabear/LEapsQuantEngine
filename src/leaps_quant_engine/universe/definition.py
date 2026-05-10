from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from leaps_quant_engine.models import Symbol


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    name: str
    type: str
    period: int
    field: str = "close"
    parameters: dict[str, Any] | None = None
    resolution: str = "any"


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    id: str
    market: str
    symbols: tuple[Symbol, ...]
    indicators: tuple[IndicatorDefinition, ...]
    tags: tuple[str, ...] = ()
    symbol_properties: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    @property
    def symbol_keys(self) -> tuple[str, ...]:
        return tuple(symbol.key for symbol in self.symbols)

    def properties_for(self, symbol: Symbol | str) -> Mapping[str, Any]:
        key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.symbol_properties.get(key, {})

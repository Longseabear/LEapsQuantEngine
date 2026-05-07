from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from leaps_quant_engine.models import Symbol


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    name: str
    type: str
    period: int
    field: str = "close"
    parameters: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class UniverseDefinition:
    id: str
    market: str
    symbols: tuple[Symbol, ...]
    indicators: tuple[IndicatorDefinition, ...]
    tags: tuple[str, ...] = ()

    @property
    def symbol_keys(self) -> tuple[str, ...]:
        return tuple(symbol.key for symbol in self.symbols)

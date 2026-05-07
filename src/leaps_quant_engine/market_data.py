from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from leaps_quant_engine.models import Bar, Symbol


class MarketDataProvider(Protocol):
    """LEAN-style market data boundary for live, paper, and replay providers."""

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        """Return the latest normalized bar/quote-like price for a symbol."""

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Return normalized daily history for a symbol."""


@dataclass(frozen=True, slots=True)
class MarketDataError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message

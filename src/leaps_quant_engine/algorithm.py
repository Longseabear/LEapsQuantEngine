from __future__ import annotations

from abc import ABC, abstractmethod

from leaps_quant_engine.models import DataSlice, PortfolioTarget
from leaps_quant_engine.portfolio import PortfolioView


class Algorithm(ABC):
    """Minimal LEAN-like strategy boundary."""

    def initialize(self) -> None:
        pass

    @abstractmethod
    def on_data(self, data: DataSlice, portfolio: PortfolioView) -> list[PortfolioTarget]:
        """Return desired absolute sleeve-level holdings."""

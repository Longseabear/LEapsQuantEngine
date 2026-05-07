from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.algorithm import Algorithm
from leaps_quant_engine.models import DataSlice, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio, PortfolioView


@dataclass(frozen=True, slots=True)
class SleevePolicy:
    max_position_pct: float = 1.0


@dataclass(slots=True)
class Sleeve:
    id: str
    algorithm: Algorithm
    portfolio: Portfolio
    policy: SleevePolicy

    def initialize(self) -> None:
        self.algorithm.initialize()

    def on_data(self, data: DataSlice) -> list[PortfolioTarget]:
        view = PortfolioView.from_portfolio(self.portfolio)
        targets = self.algorithm.on_data(data, view)
        return self._apply_policy(data, targets)

    def _apply_policy(self, data: DataSlice, targets: list[PortfolioTarget]) -> list[PortfolioTarget]:
        approved: list[PortfolioTarget] = []
        max_notional = self.portfolio.cash * self.policy.max_position_pct
        for target in targets:
            bar = data.get(target.symbol)
            if bar is None or target.quantity <= 0:
                approved.append(target)
                continue
            capped_quantity = int(max_notional // bar.close)
            approved.append(
                PortfolioTarget(
                    symbol=target.symbol,
                    quantity=min(target.quantity, capped_quantity),
                    tag=target.tag,
                )
            )
        return approved

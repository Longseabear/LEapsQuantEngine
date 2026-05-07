from __future__ import annotations

from dataclasses import dataclass, field

from leaps_quant_engine.execution import ImmediateExecutionModel
from leaps_quant_engine.models import DataSlice, OrderIntent
from leaps_quant_engine.sleeve import Sleeve


@dataclass(slots=True)
class EngineResult:
    orders: list[OrderIntent] = field(default_factory=list)


@dataclass(slots=True)
class Engine:
    sleeves: list[Sleeve]
    execution_model: ImmediateExecutionModel = field(default_factory=ImmediateExecutionModel)

    def initialize(self) -> None:
        for sleeve in self.sleeves:
            sleeve.initialize()

    def run(self, feed: list[DataSlice], fill_immediately: bool = False) -> EngineResult:
        self.initialize()
        result = EngineResult()
        for data in feed:
            for sleeve in self.sleeves:
                targets = sleeve.on_data(data)
                orders = self.execution_model.create_orders(sleeve.id, sleeve.portfolio, data, targets)
                result.orders.extend(orders)
                if fill_immediately:
                    for order in orders:
                        sleeve.portfolio.apply_fill(order)
        return result

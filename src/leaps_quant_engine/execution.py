from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    sleeve_id: str
    generated_at: datetime
    portfolio: Portfolio
    data: DataSlice
    approved_targets: tuple[PortfolioTarget, ...]


class ExecutionModel(Protocol):
    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
    ) -> list[OrderIntent]:
        """Convert approved portfolio targets into order intents."""


@dataclass(frozen=True, slots=True)
class OrderIntentBatch:
    sleeve_id: str
    generated_at: datetime
    order_intents: tuple[OrderIntent, ...]
    model_name: str = ""
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    batch_id: str = field(default_factory=lambda: f"order-intents-{uuid4()}")

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def order_count(self) -> int:
        return len(self.order_intents)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sleeve_id": self.sleeve_id,
            "generated_at": self.generated_at.isoformat(),
            "model_name": self.model_name,
            "reason": self.reason,
            "order_count": self.order_count,
            "orders": [
                {
                    "sleeve_id": order.sleeve_id,
                    "symbol": order.symbol.key,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "reference_price": order.reference_price,
                    "tag": order.tag,
                    "notional": order.notional,
                }
                for order in self.order_intents
            ],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ImmediateExecutionModel:
    """Transforms approved targets into order intents without broker submission."""

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
    ) -> list[OrderIntent]:
        orders: list[OrderIntent] = []
        for target in targets:
            current_quantity = portfolio.quantity(target.symbol)
            delta = target.quantity - current_quantity
            if delta == 0:
                continue
            bar = data.get(target.symbol)
            if bar is None:
                continue
            orders.append(
                OrderIntent(
                    sleeve_id=sleeve_id,
                    symbol=target.symbol,
                    side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                    quantity=abs(delta),
                    reference_price=bar.close,
                    tag=target.tag,
                )
            )
        return orders


@dataclass(frozen=True, slots=True)
class ExecutionEngine:
    model: ExecutionModel = field(default_factory=ImmediateExecutionModel)
    reason: str = "execution"

    def execute(self, context: ExecutionContext) -> OrderIntentBatch:
        orders = tuple(
            self.model.create_orders(
                context.sleeve_id,
                context.portfolio,
                context.data,
                list(context.approved_targets),
            )
        )
        return OrderIntentBatch(
            sleeve_id=context.sleeve_id,
            generated_at=context.generated_at,
            order_intents=orders,
            model_name=type(self.model).__name__,
            reason=self.reason,
            metadata={
                "approved_target_count": len(context.approved_targets),
                "created_order_count": len(orders),
            },
        )

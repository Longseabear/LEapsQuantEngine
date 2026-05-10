from __future__ import annotations

from leaps_quant_engine.execution import StandardExecutionModel
from leaps_quant_engine.models import DataSlice, OrderIntent, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol


class UsEtfRotationExecutionModel:
    def __init__(
        self,
        tag_prefix: str = "us_etf_rotation",
        order_type: str = "limit",
        time_in_force: str = "day",
        limit_offset_bps: float = 0.0,
    ) -> None:
        self.tag_prefix = tag_prefix
        self.base_model = StandardExecutionModel(
            order_type=order_type,
            time_in_force=time_in_force,
            limit_offset_bps=limit_offset_bps,
        )

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
    ) -> list[OrderIntent]:
        base_orders = self.base_model.create_orders(sleeve_id, portfolio, data, targets)
        return [
            OrderIntent(
                sleeve_id=order.sleeve_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                reference_price=order.reference_price,
                tag=f"{self.tag_prefix}:{currency_for_symbol(order.symbol).lower()}:{order.tag}",
                order_type=order.order_type,
                limit_price=order.limit_price,
                time_in_force=order.time_in_force,
                metadata=dict(order.metadata),
            )
            for order in base_orders
        ]


def create_execution_model(params):
    return UsEtfRotationExecutionModel(
        tag_prefix=str(params.get("tag_prefix", "us_etf_rotation")),
        order_type=str(params.get("order_type", "limit")),
        time_in_force=str(params.get("time_in_force", "day")),
        limit_offset_bps=float(params.get("limit_offset_bps", 0.0)),
    )

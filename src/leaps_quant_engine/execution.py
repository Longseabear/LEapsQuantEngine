from __future__ import annotations

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio


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

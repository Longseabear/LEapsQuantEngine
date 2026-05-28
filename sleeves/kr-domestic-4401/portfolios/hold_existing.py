from dataclasses import dataclass

from leaps_quant_engine.alpha import InsightDirection
from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
)


@dataclass(frozen=True, slots=True)
class HoldExistingPortfolioConstructionModel:
    """Safe starter portfolio.

    It never creates new exposure by itself. If active UP insights are later
    added, it can target their insight weight. Existing holdings without an
    explicit exit are carried at their current percentage to avoid accidental
    liquidation from an empty alpha cycle.
    """

    max_target_percent: float = 0.2
    hold_existing: bool = True

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        targets: dict[str, PortfolioAllocationTarget] = {}
        for insight in context.active_insights:
            if not insight.symbol.key.startswith("KRX:"):
                continue
            if insight.direction is InsightDirection.UP:
                weight = insight.weight if insight.weight is not None else self.max_target_percent
                targets[insight.symbol.key] = PortfolioAllocationTarget(
                    symbol=insight.symbol,
                    target_percent=max(0.0, min(float(weight), self.max_target_percent)),
                    tag=f"kr-domestic-4401:up:{insight.alpha_id}",
                )
            elif insight.direction in {InsightDirection.FLAT, InsightDirection.DOWN}:
                targets[insight.symbol.key] = PortfolioAllocationTarget(
                    symbol=insight.symbol,
                    target_percent=0.0,
                    tag=f"kr-domestic-4401:exit:{insight.alpha_id}",
                )

        if self.hold_existing:
            for symbol in context.held_symbols:
                if symbol.key in targets or not symbol.key.startswith("KRX:"):
                    continue
                current_value = context.portfolio.position_value(symbol, context.data)
                target_value = context.target_value_for_symbol(symbol)
                current_percent = current_value / target_value if target_value > 0 else 0.0
                if current_percent > 0:
                    targets[symbol.key] = PortfolioAllocationTarget(
                        symbol=symbol,
                        target_percent=max(0.0, min(current_percent, 1.0)),
                        tag="kr-domestic-4401:hold_existing",
                    )
        return tuple(targets.values())


def create_portfolio_model(params):
    return HoldExistingPortfolioConstructionModel(
        max_target_percent=float(params.get("max_target_percent", 0.2)),
        hold_existing=bool(params.get("hold_existing", True)),
    )

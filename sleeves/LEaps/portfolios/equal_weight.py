from __future__ import annotations

from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel


def create_portfolio_model(
    max_portfolio_pct: float = 1.0,
    long_only: bool = True,
) -> EqualWeightPortfolioConstructionModel:
    return EqualWeightPortfolioConstructionModel(
        max_portfolio_pct=max_portfolio_pct,
        long_only=long_only,
    )

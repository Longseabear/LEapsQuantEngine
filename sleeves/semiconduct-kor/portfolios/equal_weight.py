from __future__ import annotations

from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel


def create_portfolio_model(params):
    return EqualWeightPortfolioConstructionModel(
        max_portfolio_pct=float(params.get("max_portfolio_pct", 0.92)),
        long_only=bool(params.get("long_only", True)),
    )

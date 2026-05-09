from __future__ import annotations

from typing import Any, Mapping

from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> EqualWeightPortfolioConstructionModel:
    values = dict(params or {})
    return EqualWeightPortfolioConstructionModel(
        max_portfolio_pct=float(values.get("max_portfolio_pct", 1.0)),
        long_only=bool(values.get("long_only", True)),
    )

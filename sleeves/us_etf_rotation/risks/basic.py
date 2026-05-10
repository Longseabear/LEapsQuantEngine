from __future__ import annotations

from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits


def create_risk_model(params):
    return BasicRiskManagementModel(
        limits=RiskLimits(
            long_only=bool(params.get("long_only", True)),
            max_position_pct=float(params.get("max_position_pct", 0.30)),
            max_total_exposure_pct=float(params.get("max_total_exposure_pct", 0.95)),
            cash_buffer_pct=float(params.get("cash_buffer_pct", 0.05)),
            require_fresh_for_entries=bool(params.get("require_fresh_for_entries", True)),
            reject_invalid_snapshot=bool(params.get("reject_invalid_snapshot", True)),
        )
    )

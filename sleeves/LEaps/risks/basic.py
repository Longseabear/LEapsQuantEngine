from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits


def create_risk_model(params):
    return BasicRiskManagementModel(
        limits=RiskLimits(
            long_only=bool(params.get("long_only", True)),
            max_position_pct=float(params.get("max_position_pct", 0.35)),
            cash_buffer_pct=float(params.get("cash_buffer_pct", 0.03)),
        )
    )

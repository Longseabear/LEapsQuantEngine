from __future__ import annotations

from typing import Any, Mapping

from leaps_quant_engine.rl import ReinforcementLearningPortfolioConstructionModel


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> ReinforcementLearningPortfolioConstructionModel:
    values = dict(params or {})
    levels = values.get("exposure_levels") or (0.0, 0.25, 0.50, 0.75, 0.95)
    policy_paths = values.get("policy_paths") or values.get("ensemble_policy_paths") or ()
    return ReinforcementLearningPortfolioConstructionModel(
        policy_path=values.get("policy_path"),
        policy_paths=tuple(str(path) for path in policy_paths),
        metadata_path=values.get("metadata_path"),
        exposure_levels=tuple(float(value) for value in levels),
        fallback_action=int(values.get("fallback_action", 3)),
        max_position_pct=float(values.get("max_position_pct", 0.35)),
        min_position_pct=float(values.get("min_position_pct", 0.0)),
        long_only=bool(values.get("long_only", True)),
        model_name=str(values.get("model_name", "attention_ppo")),
        top_k=int(values.get("top_k", 32)),
        weight_temperature=float(values.get("weight_temperature", 0.35)),
        min_signal_action=int(values.get("min_signal_action", 0)),
        allocation_mode=str(values.get("allocation_mode", "equal")),
        fallback_gross_exposure=float(values.get("fallback_gross_exposure", 0.75)),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", False)),
        target_smoothing_alpha=float(values.get("target_smoothing_alpha", 1.0)),
        target_drift_threshold_pct=float(values.get("target_drift_threshold_pct", 0.0)),
    )

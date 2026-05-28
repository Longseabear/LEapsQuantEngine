from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from leaps_quant_engine.alpha import InsightDirection
from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext


ALPHA_ID = "kr-lowvol-defensive-alpha"


@dataclass(frozen=True, slots=True)
class LowVolInverseVolPortfolioConstructionModel:
    top_k: int = 15
    core_gross_exposure: float = 1.00
    neutral_gross_exposure: float = 0.90
    defensive_gross_exposure: float = 0.75
    max_position_pct: float = 0.22
    min_position_pct: float = 0.015
    emit_zero_for_missing_held_targets: bool = True
    stale_target_max_age_days: int = 28
    flat_stale_days: int = 14
    flat_return_band_pct: float = 0.015
    min_lot_value_multiple: float = 1.10
    alpha_id: str = ALPHA_ID

    @property
    def model_name(self) -> str:
        return type(self).__name__

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        candidates = _latest_up_candidates(context.active_insights, self.alpha_id)
        if not candidates:
            return ()

        candidates.sort(key=lambda item: (float(item["score"]), item["symbol_key"]), reverse=True)
        selected = candidates[: max(1, self.top_k)]
        candidates_by_symbol = {str(item["symbol_key"]): item for item in candidates}
        gross = _gross_exposure(selected, self)
        raw_scores = [_allocation_score(item) for item in selected]
        score_sum = sum(raw_scores)
        targets: dict[str, PortfolioAllocationTarget] = {}
        for item, raw_score in zip(selected, raw_scores):
            if score_sum <= 0:
                target_percent = gross / len(selected)
            else:
                target_percent = gross * (raw_score / score_sum)
            target_percent = min(max(target_percent, 0.0), self.max_position_pct)
            insight = item["insight"]
            target_percent = _lot_stable_target_percent(context, insight.symbol, target_percent, self)
            if target_percent < self.min_position_pct:
                continue
            targets[insight.symbol_key] = PortfolioAllocationTarget(
                symbol=insight.symbol,
                target_percent=target_percent,
                tag=(
                    f"lowvol_v2_inverse_vol:{self.alpha_id}:rank={int(item['rank'])}:"
                    f"vol={float(item['volatility']):.3f}:"
                    f"crowd={float(item['crowding_penalty']):.2f}:"
                    f"lottery={float(item['lottery_penalty']):.2f}:gross={gross:.2f}"
                ),
            )

        selected_keys = set(targets)
        for symbol in context.held_symbols:
            if symbol.key in selected_keys:
                continue
            if self.emit_zero_for_missing_held_targets:
                targets[symbol.key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=0.0,
                    tag=f"lowvol_inverse_vol:{self.alpha_id}:missing_target_zero",
                )
                continue
            stale_reason = _stale_zero_reason(context, symbol.key, candidates_by_symbol.get(symbol.key), self)
            if stale_reason:
                targets[symbol.key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=0.0,
                    tag=f"lowvol_inverse_vol:{self.alpha_id}:stale_missing_target_zero:{stale_reason}",
                )
        return tuple(targets.values())


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> LowVolInverseVolPortfolioConstructionModel:
    values = dict(params or {})
    return LowVolInverseVolPortfolioConstructionModel(
        top_k=int(values.get("top_k", 15)),
        core_gross_exposure=float(values.get("core_gross_exposure", 1.00)),
        neutral_gross_exposure=float(values.get("neutral_gross_exposure", 0.90)),
        defensive_gross_exposure=float(values.get("defensive_gross_exposure", 0.75)),
        max_position_pct=float(values.get("max_position_pct", 0.22)),
        min_position_pct=float(values.get("min_position_pct", 0.015)),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", True)),
        stale_target_max_age_days=int(values.get("stale_target_max_age_days", 28)),
        flat_stale_days=int(values.get("flat_stale_days", 14)),
        flat_return_band_pct=float(values.get("flat_return_band_pct", 0.015)),
        min_lot_value_multiple=float(values.get("min_lot_value_multiple", 1.10)),
        alpha_id=str(values.get("alpha_id", ALPHA_ID)),
    )


def _latest_up_candidates(insights: tuple[Any, ...], alpha_id: str) -> list[dict[str, Any]]:
    latest: dict[str, Any] = {}
    for insight in insights:
        if str(getattr(insight, "alpha_id", "")) != alpha_id:
            continue
        if getattr(insight, "direction", None) is not InsightDirection.UP:
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[insight.symbol_key] = insight

    candidates: list[dict[str, Any]] = []
    for insight in latest.values():
        metadata = dict(getattr(insight, "metadata", {}) or {})
        volatility = _safe_float(metadata.get("normalized_volatility"), default=0.08)
        score = _safe_float(getattr(insight, "score", None), default=0.0)
        rank = int(metadata.get("rank") or 999)
        crowding_penalty = _safe_float(metadata.get("crowding_penalty"), default=0.0)
        lottery_penalty = _safe_float(metadata.get("lottery_penalty"), default=0.0)
        turnover_shock_penalty = _safe_float(metadata.get("turnover_shock_penalty"), default=0.0)
        candidates.append(
            {
                "symbol_key": insight.symbol_key,
                "insight": insight,
                "volatility": max(volatility, 0.01),
                "score": score,
                "rank": rank,
                "risk_bucket": str(metadata.get("risk_bucket") or "normal"),
                "quality_score": _safe_float(metadata.get("quality_score"), default=0.50),
                "value_score": _safe_float(metadata.get("value_score"), default=0.50),
                "dividend_score": _safe_float(metadata.get("dividend_score"), default=0.35),
                "crowding_penalty": _clamp_unit(crowding_penalty),
                "lottery_penalty": _clamp_unit(lottery_penalty),
                "turnover_shock_penalty": _clamp_unit(turnover_shock_penalty),
            }
        )
    return candidates


def _allocation_score(item: dict[str, Any]) -> float:
    volatility = max(float(item["volatility"]), 0.01)
    score = max(float(item["score"]), 0.0)
    quality_boost = 0.70 + _clamp_unit(float(item["quality_score"])) * 0.30
    value_boost = 0.82 + _clamp_unit(float(item["value_score"])) * 0.18
    dividend_boost = 0.90 + _clamp_unit(float(item["dividend_score"])) * 0.10
    heat_penalty = (
        _clamp_unit(float(item["crowding_penalty"])) * 0.45
        + _clamp_unit(float(item["lottery_penalty"])) * 0.35
        + _clamp_unit(float(item["turnover_shock_penalty"])) * 0.25
    )
    defensive_multiplier = max(0.10, 1.0 - heat_penalty)
    return (1.0 / volatility) * (0.65 + score) * quality_boost * value_boost * dividend_boost * defensive_multiplier


def _lot_stable_target_percent(
    context: PortfolioConstructionContext,
    symbol: Any,
    target_percent: float,
    model: LowVolInverseVolPortfolioConstructionModel,
) -> float:
    multiple = max(float(model.min_lot_value_multiple), 0.0)
    if multiple <= 0:
        return target_percent
    target_value = context.target_value_for_symbol(symbol)
    price = context.portfolio.mark_price(symbol, context.data)
    if target_value <= 0 or price is None or price <= 0:
        return target_percent
    if target_value * target_percent >= float(price) * multiple:
        return target_percent
    return 0.0


def _stale_zero_reason(
    context: PortfolioConstructionContext,
    symbol_key: str,
    item: dict[str, Any] | None,
    model: LowVolInverseVolPortfolioConstructionModel,
) -> str:
    if item is None:
        return "no_active_up_insight"
    age_days = _insight_age_days(context.data.time, item.get("insight"))
    if age_days is None:
        return ""
    if age_days >= model.stale_target_max_age_days:
        return f"age={age_days:.0f}d"
    holding_return = _holding_return(context, symbol_key)
    if (
        holding_return is not None
        and age_days >= model.flat_stale_days
        and holding_return <= model.flat_return_band_pct
    ):
        return f"flat_age={age_days:.0f}d:return={holding_return:.3f}"
    return ""


def _insight_age_days(as_of: datetime, insight: Any) -> float | None:
    generated_at = getattr(insight, "generated_at", None)
    if generated_at is None:
        return None
    return max(0.0, (as_of - generated_at).total_seconds() / 86400.0)


def _holding_return(context: PortfolioConstructionContext, symbol_key: str) -> float | None:
    holding = context.portfolio.holdings.get(symbol_key)
    if holding is None or holding.average_price <= 0:
        return None
    price = context.portfolio.mark_price(holding.symbol, context.data)
    if price is None or price <= 0:
        return None
    return (float(price) / float(holding.average_price)) - 1.0


def _gross_exposure(candidates: list[dict[str, Any]], model: LowVolInverseVolPortfolioConstructionModel) -> float:
    if not candidates:
        return 0.0
    average_volatility = sum(float(item["volatility"]) for item in candidates) / len(candidates)
    defensive_count = sum(1 for item in candidates if str(item["risk_bucket"]) == "defensive")
    defensive_ratio = defensive_count / len(candidates)
    average_heat = sum(
        float(item["crowding_penalty"]) * 0.42
        + float(item["lottery_penalty"]) * 0.38
        + float(item["turnover_shock_penalty"]) * 0.20
        for item in candidates
    ) / len(candidates)
    if average_volatility <= 0.050 and defensive_ratio <= 0.25:
        base = model.core_gross_exposure
    elif average_volatility <= 0.085 and defensive_ratio <= 0.50:
        base = model.neutral_gross_exposure
    else:
        base = model.defensive_gross_exposure
    if average_heat >= 0.55:
        base = min(base, model.defensive_gross_exposure)
    return _clamp_pct(max(0.40, base * (1.0 - average_heat * 0.35)))


def _safe_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_pct(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)


def _clamp_unit(value: float) -> float:
    return min(max(float(value), 0.0), 1.0)

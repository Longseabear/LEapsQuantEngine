from dataclasses import dataclass
from typing import Any, Mapping

from leaps_quant_engine.alpha import InsightDirection
from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
)


ALPHA_ID = "kr-domestic-4401-core-regime"


@dataclass(frozen=True, slots=True)
class RegimeBudgetedInverseVolPortfolioConstructionModel:
    alpha_id: str = ALPHA_ID
    max_risk_position_pct: float = 0.10
    max_core_etf_pct: float = 0.18
    max_defensive_pct: float = 0.30
    max_hedge_pct: float = 0.06
    min_position_pct: float = 0.015
    min_return_volatility: float = 0.012
    max_gross_increase_pct: float = 0.35
    max_symbol_increase_pct: float = 0.08
    whole_share_floor_enabled: bool = True
    whole_share_floor_min_fraction: float = 0.35
    emit_zero_for_missing_held_targets: bool = False

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        latest = _latest_insights(context.active_insights, self.alpha_id)
        if not latest:
            return _carry_or_zero_missing_holdings(context, {}, self)

        targets: dict[str, PortfolioAllocationTarget] = {}
        exit_symbols: set[str] = set()
        candidates_by_bucket: dict[str, list[dict[str, Any]]] = {"risk": [], "defensive": [], "hedge": []}
        regime = "neutral"
        for insight in latest.values():
            metadata = dict(getattr(insight, "metadata", {}) or {})
            regime = str(metadata.get("regime") or regime)
            bucket = str(metadata.get("bucket") or "")
            if insight.direction in {InsightDirection.FLAT, InsightDirection.DOWN}:
                targets[insight.symbol.key] = PortfolioAllocationTarget(
                    symbol=insight.symbol,
                    target_percent=0.0,
                    tag=f"kr-domestic-4401:exit:{self.alpha_id}:{bucket or 'flat'}",
                )
                exit_symbols.add(insight.symbol.key)
                continue
            if insight.direction is not InsightDirection.UP:
                continue
            if bucket not in candidates_by_bucket:
                continue
            candidates_by_bucket[bucket].append(_candidate(insight, metadata))

        budget = _regime_budget(regime)
        for bucket, candidates in candidates_by_bucket.items():
            bucket_targets = _bucket_targets(
                candidates,
                bucket_budget=float(budget.get(bucket, 0.0)),
                model=self,
            )
            for item, target_percent in bucket_targets:
                if item["symbol_key"] in exit_symbols:
                    continue
                if target_percent < self.min_position_pct:
                    continue
                targets[item["symbol_key"]] = PortfolioAllocationTarget(
                    symbol=item["insight"].symbol,
                    target_percent=target_percent,
                    tag=(
                        f"kr-domestic-4401:regime_inverse_vol:{regime}:"
                        f"{item['bucket']}:vol={item['volatility']:.3f}:score={item['score']:.2f}"
                    ),
                )

        positive = _apply_ramp(context, targets, self)
        positive.update({symbol_key: target for symbol_key, target in targets.items() if target.target_percent == 0.0})
        return tuple(_carry_or_zero_missing_holdings(context, positive, self).values())


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> RegimeBudgetedInverseVolPortfolioConstructionModel:
    values = dict(params or {})
    return RegimeBudgetedInverseVolPortfolioConstructionModel(
        alpha_id=str(values.get("alpha_id", ALPHA_ID)),
        max_risk_position_pct=float(values.get("max_risk_position_pct", 0.10)),
        max_core_etf_pct=float(values.get("max_core_etf_pct", 0.18)),
        max_defensive_pct=float(values.get("max_defensive_pct", 0.30)),
        max_hedge_pct=float(values.get("max_hedge_pct", 0.06)),
        min_position_pct=float(values.get("min_position_pct", 0.015)),
        min_return_volatility=float(values.get("min_return_volatility", 0.012)),
        max_gross_increase_pct=float(values.get("max_gross_increase_pct", 0.35)),
        max_symbol_increase_pct=float(values.get("max_symbol_increase_pct", 0.08)),
        whole_share_floor_enabled=bool(values.get("whole_share_floor_enabled", True)),
        whole_share_floor_min_fraction=float(values.get("whole_share_floor_min_fraction", 0.35)),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", False)),
    )


def _latest_insights(insights: tuple[Any, ...], alpha_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for insight in insights:
        if str(getattr(insight, "alpha_id", "")) != alpha_id:
            continue
        previous = latest.get(insight.symbol.key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[insight.symbol.key] = insight
    return latest


def _candidate(insight: Any, metadata: Mapping[str, Any]) -> dict[str, Any]:
    bucket = str(metadata.get("bucket") or "")
    return {
        "symbol_key": insight.symbol.key,
        "insight": insight,
        "bucket": bucket,
        "role": str(metadata.get("role") or ""),
        "score": max(_safe_float(getattr(insight, "score", None), 0.0), 0.01),
        "volatility": _candidate_volatility(metadata),
    }


def _candidate_volatility(metadata: Mapping[str, Any]) -> float:
    return max(
        _safe_float(metadata.get("return_volatility"), 0.0),
        _safe_float(metadata.get("volatility"), 0.0),
        0.0,
    )


def _regime_budget(regime: str) -> dict[str, float]:
    return {
        "strong_risk_on": {"risk": 0.86, "defensive": 0.06, "hedge": 0.0},
        "risk_on": {"risk": 0.78, "defensive": 0.08, "hedge": 0.0},
        "neutral": {"risk": 0.46, "defensive": 0.30, "hedge": 0.0},
        "risk_off": {"risk": 0.0, "defensive": 0.72, "hedge": 0.0},
        "shock": {"risk": 0.0, "defensive": 0.82, "hedge": 0.06},
    }.get(regime, {"risk": 0.40, "defensive": 0.35, "hedge": 0.0})


def _bucket_targets(
    candidates: list[dict[str, Any]],
    *,
    bucket_budget: float,
    model: RegimeBudgetedInverseVolPortfolioConstructionModel,
) -> list[tuple[dict[str, Any], float]]:
    if bucket_budget <= 0.0 or not candidates:
        return []
    raw_scores = [
        item["score"] / max(float(item["volatility"]), model.min_return_volatility)
        for item in candidates
    ]
    total = sum(raw_scores)
    if total <= 0.0:
        raw_targets = [bucket_budget / len(candidates) for _ in candidates]
    else:
        raw_targets = [bucket_budget * raw_score / total for raw_score in raw_scores]
    capped = [
        min(max(raw_target, 0.0), _position_cap(item, model))
        for item, raw_target in zip(candidates, raw_targets)
    ]
    return list(zip(candidates, capped))


def _position_cap(item: dict[str, Any], model: RegimeBudgetedInverseVolPortfolioConstructionModel) -> float:
    if item["bucket"] == "defensive":
        return model.max_defensive_pct
    if item["bucket"] == "hedge":
        return model.max_hedge_pct
    if item["symbol_key"] in {"KRX:069500", "KRX:102110"} or item["role"] == "risk_proxy":
        return model.max_core_etf_pct
    return model.max_risk_position_pct


def _apply_ramp(
    context: PortfolioConstructionContext,
    targets: dict[str, PortfolioAllocationTarget],
    model: RegimeBudgetedInverseVolPortfolioConstructionModel,
) -> dict[str, PortfolioAllocationTarget]:
    positive = {key: target for key, target in targets.items() if target.target_percent > 0.0}
    if not positive:
        return {}
    current_by_symbol = {
        key: _current_percent(context, target.symbol)
        for key, target in positive.items()
    }
    increased: dict[str, PortfolioAllocationTarget] = {}
    total_increase = 0.0
    for key, target in positive.items():
        current = current_by_symbol.get(key, 0.0)
        capped = min(target.target_percent, current + model.max_symbol_increase_pct)
        capped = _apply_whole_share_floor(context, target, capped, current, model)
        total_increase += max(0.0, capped - current)
        increased[key] = PortfolioAllocationTarget(target.symbol, capped, tag=target.tag)
    if total_increase <= model.max_gross_increase_pct:
        return increased
    scale = model.max_gross_increase_pct / total_increase
    scaled: dict[str, PortfolioAllocationTarget] = {}
    for key, target in increased.items():
        current = current_by_symbol.get(key, 0.0)
        next_percent = current + max(0.0, target.target_percent - current) * scale
        if next_percent >= model.min_position_pct:
            scaled[key] = PortfolioAllocationTarget(target.symbol, next_percent, tag=f"{target.tag}:ramp")
    return scaled


def _apply_whole_share_floor(
    context: PortfolioConstructionContext,
    target: PortfolioAllocationTarget,
    target_percent: float,
    current_percent: float,
    model: RegimeBudgetedInverseVolPortfolioConstructionModel,
) -> float:
    if not model.whole_share_floor_enabled:
        return target_percent
    if current_percent > 0.0 or target_percent <= 0.0:
        return target_percent

    price = context.portfolio.mark_price(target.symbol, context.data)
    target_value = context.target_value_for_symbol(target.symbol)
    if price is None or price <= 0.0 or target_value <= 0.0:
        return target_percent

    one_share_percent = price / target_value
    if one_share_percent <= target_percent:
        return target_percent
    if one_share_percent > min(_cap_from_tag(target.tag, model), model.max_symbol_increase_pct):
        return target_percent
    if target_percent / one_share_percent < model.whole_share_floor_min_fraction:
        return target_percent
    return one_share_percent


def _cap_from_tag(tag: str, model: RegimeBudgetedInverseVolPortfolioConstructionModel) -> float:
    if ":defensive:" in tag:
        return model.max_defensive_pct
    if ":hedge:" in tag:
        return model.max_hedge_pct
    if "KRX:069500" in tag or "KRX:102110" in tag:
        return model.max_core_etf_pct
    return model.max_risk_position_pct


def _carry_or_zero_missing_holdings(
    context: PortfolioConstructionContext,
    targets: dict[str, PortfolioAllocationTarget],
    model: RegimeBudgetedInverseVolPortfolioConstructionModel,
) -> dict[str, PortfolioAllocationTarget]:
    result = dict(targets)
    for symbol in context.held_symbols:
        if symbol.key in result or not symbol.key.startswith("KRX:"):
            continue
        if model.emit_zero_for_missing_held_targets:
            result[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=0.0,
                tag=f"kr-domestic-4401:regime_inverse_vol:{model.alpha_id}:missing_target_zero",
            )
            continue
        current = _current_percent(context, symbol)
        if current > 0.0:
            result[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=current,
                tag=f"kr-domestic-4401:regime_inverse_vol:{model.alpha_id}:hold_existing",
            )
    return result


def _current_percent(context: PortfolioConstructionContext, symbol: Any) -> float:
    target_value = context.target_value_for_symbol(symbol)
    if target_value <= 0.0:
        return 0.0
    return max(0.0, context.portfolio.position_value(symbol, context.data) / target_value)


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed

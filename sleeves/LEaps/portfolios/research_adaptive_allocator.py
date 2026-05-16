from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext
from leaps_quant_engine.portfolio import currency_for_symbol


ALPHA_WEIGHTS = {
    "leaps-kospi-conviction": 1.00,
    "leaps-kospi-pullback-reversion": 0.70,
}
ETF_SAFETY_ALPHA_ID = "leaps-krx-etf-safety"


class ResearchAdaptivePortfolioConstructionModel:
    def __init__(
        self,
        *,
        top_k: int = 8,
        gross_exposure: float = 0.82,
        neutral_gross_exposure: float = 0.55,
        weak_gross_exposure: float = 0.32,
        cash_bias: float = 0.22,
        max_position_pct: float = 0.18,
        min_position_pct: float = 0.015,
        score_temperature: float = 0.28,
        volatility_penalty: float = 0.75,
        drawdown_penalty: float = 0.25,
        recent_momentum_weight: float = 0.18,
        trend_weight: float = 0.08,
        multi_alpha_bonus: float = 0.08,
        max_normalized_volatility: float = 0.20,
        high_vol_momentum_exception: float = 0.45,
        hold_existing_buffer: float = 0.82,
        emit_zero_for_missing_held_targets: bool = True,
        long_only: bool = True,
        enable_etf_safety_bucket: bool = False,
        etf_safety_alpha_id: str = ETF_SAFETY_ALPHA_ID,
        etf_safety_max_total_pct: float = 0.65,
        model_name: str = "research_adaptive_allocator",
    ) -> None:
        self.top_k = top_k
        self.gross_exposure = gross_exposure
        self.neutral_gross_exposure = neutral_gross_exposure
        self.weak_gross_exposure = weak_gross_exposure
        self.cash_bias = cash_bias
        self.max_position_pct = max_position_pct
        self.min_position_pct = min_position_pct
        self.score_temperature = score_temperature
        self.volatility_penalty = volatility_penalty
        self.drawdown_penalty = drawdown_penalty
        self.recent_momentum_weight = recent_momentum_weight
        self.trend_weight = trend_weight
        self.multi_alpha_bonus = multi_alpha_bonus
        self.max_normalized_volatility = max_normalized_volatility
        self.high_vol_momentum_exception = high_vol_momentum_exception
        self.hold_existing_buffer = hold_existing_buffer
        self.emit_zero_for_missing_held_targets = emit_zero_for_missing_held_targets
        self.long_only = long_only
        self.enable_etf_safety_bucket = enable_etf_safety_bucket
        self.etf_safety_alpha_id = etf_safety_alpha_id
        self.etf_safety_max_total_pct = etf_safety_max_total_pct
        self.model_name = model_name

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        stock_insights = tuple(
            insight
            for insight in context.active_insights
            if str(getattr(insight, "alpha_id", "")) != self.etf_safety_alpha_id
        )
        blocked = _latest_non_up_symbol_keys(stock_insights)
        grouped = _group_latest_up_insights(stock_insights, blocked)
        candidates = [
            candidate
            for candidate in (_candidate_from_insights(symbol_key, insights, self) for symbol_key, insights in grouped.items())
            if candidate is not None
        ]
        safety_targets, stock_gross_cap = _etf_safety_targets(context, self)
        if not candidates:
            return _zero_missing_held_targets(context, safety_targets, self)

        candidates.sort(key=lambda item: (item["quality"], item["symbol_key"]), reverse=True)
        selected = self._selected_candidates(context, candidates)
        gross = self._gross_exposure(candidates)
        if stock_gross_cap is not None:
            gross = min(gross, stock_gross_cap)
        target_map = dict(safety_targets)
        for symbol_key, target in self._target_map(selected, gross).items():
            target_map.setdefault(symbol_key, target)
        return _zero_missing_held_targets(context, target_map, self)

    def _selected_candidates(
        self,
        context: PortfolioConstructionContext,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        selected = candidates[: max(1, self.top_k)]
        cutoff = float(selected[-1]["quality"]) if selected else 0.0
        held_keys = {symbol.key for symbol in context.portfolio.held_symbols}
        for candidate in candidates[self.top_k :]:
            if candidate["symbol_key"] not in held_keys:
                continue
            if float(candidate["quality"]) < cutoff * self.hold_existing_buffer:
                continue
            selected.append(candidate)
        selected.sort(key=lambda item: (item["quality"], item["symbol_key"]), reverse=True)
        return selected[: max(1, self.top_k)]

    def _gross_exposure(self, candidates: list[dict[str, Any]]) -> float:
        breadth_values = [float(item["market_breadth"]) for item in candidates if item.get("market_breadth") is not None]
        momentum_values = [max(float(item["momentum"]), 0.0) for item in candidates if item.get("momentum") is not None]
        volatility_values = [float(item["volatility"]) for item in candidates if item.get("volatility") is not None]
        breadth = _average(breadth_values)
        momentum = _average(momentum_values)
        volatility = _average(volatility_values)
        if volatility >= 0.18 or breadth < 0.25:
            return _clamp_pct(self.weak_gross_exposure)
        if breadth >= 0.55 and momentum >= 0.18 and volatility <= 0.16:
            return _clamp_pct(self.gross_exposure)
        if breadth >= 0.38 and momentum >= 0.10 and volatility <= 0.17:
            return _clamp_pct((self.gross_exposure + self.neutral_gross_exposure) / 2.0)
        return _clamp_pct(self.neutral_gross_exposure)

    def _target_map(
        self,
        candidates: list[dict[str, Any]],
        gross: float,
    ) -> dict[str, PortfolioAllocationTarget]:
        if not candidates or gross <= 0:
            return {}
        qualities = np.asarray([float(item["quality"]) for item in candidates], dtype=np.float64)
        qualities = np.maximum(qualities, 0.0)
        if not np.any(qualities > 0):
            weights = np.full(len(candidates), 1.0 / len(candidates), dtype=np.float64)
            cash_weight = 0.0
        else:
            logits = qualities / max(float(self.score_temperature), 1e-6)
            logits = logits - float(np.max(logits))
            raw = np.exp(logits)
            cash_weight = math.exp(max(float(self.cash_bias), 0.0) / max(float(self.score_temperature), 1e-6))
            weights = raw / (float(np.sum(raw)) + cash_weight)
        scale = gross
        result: dict[str, PortfolioAllocationTarget] = {}
        for candidate, weight in zip(candidates, weights):
            target_percent = min(float(weight) * scale, self.max_position_pct)
            target_percent = _clamp_pct(target_percent)
            if target_percent < self.min_position_pct:
                continue
            symbol = candidate["symbol"]
            result[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=target_percent,
                tag=f"adaptive:{self.model_name}:q={float(candidate['quality']):.3f}:gross={gross:.2f}",
            )
        return result


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> ResearchAdaptivePortfolioConstructionModel:
    values = dict(params or {})
    return ResearchAdaptivePortfolioConstructionModel(
        top_k=int(values.get("top_k", 8)),
        gross_exposure=float(values.get("gross_exposure", 0.82)),
        neutral_gross_exposure=float(values.get("neutral_gross_exposure", 0.55)),
        weak_gross_exposure=float(values.get("weak_gross_exposure", 0.32)),
        cash_bias=float(values.get("cash_bias", 0.22)),
        max_position_pct=float(values.get("max_position_pct", 0.18)),
        min_position_pct=float(values.get("min_position_pct", 0.015)),
        score_temperature=float(values.get("score_temperature", 0.28)),
        volatility_penalty=float(values.get("volatility_penalty", 0.75)),
        drawdown_penalty=float(values.get("drawdown_penalty", 0.25)),
        recent_momentum_weight=float(values.get("recent_momentum_weight", 0.18)),
        trend_weight=float(values.get("trend_weight", 0.08)),
        multi_alpha_bonus=float(values.get("multi_alpha_bonus", 0.08)),
        max_normalized_volatility=float(values.get("max_normalized_volatility", 0.20)),
        high_vol_momentum_exception=float(values.get("high_vol_momentum_exception", 0.45)),
        hold_existing_buffer=float(values.get("hold_existing_buffer", 0.82)),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", True)),
        long_only=bool(values.get("long_only", True)),
        enable_etf_safety_bucket=bool(values.get("enable_etf_safety_bucket", False)),
        etf_safety_alpha_id=str(values.get("etf_safety_alpha_id", ETF_SAFETY_ALPHA_ID)),
        etf_safety_max_total_pct=float(values.get("etf_safety_max_total_pct", 0.65)),
        model_name=str(values.get("model_name", "research_adaptive_allocator")),
    )


def _group_latest_up_insights(insights: tuple[Any, ...], blocked_symbol_keys: set[str]) -> dict[str, list[Any]]:
    latest: dict[tuple[str, str], Any] = {}
    for insight in insights:
        if getattr(getattr(insight, "direction", None), "value", "") != "up":
            continue
        if insight.symbol_key in blocked_symbol_keys:
            continue
        if not _is_plausible(insight):
            continue
        key = (insight.symbol_key, str(getattr(insight, "alpha_id", "")))
        previous = latest.get(key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[key] = insight
    grouped: dict[str, list[Any]] = {}
    for (symbol_key, _alpha_id), insight in latest.items():
        grouped.setdefault(symbol_key, []).append(insight)
    return grouped


def _candidate_from_insights(
    symbol_key: str,
    insights: list[Any],
    model: ResearchAdaptivePortfolioConstructionModel,
) -> dict[str, Any] | None:
    if not insights:
        return None
    best = max(insights, key=lambda insight: _safe_float(getattr(insight, "score", None)) or 0.0)
    metadata = _merged_metadata(insights)
    momentum = _safe_float(metadata.get("momentum")) or 0.0
    momentum_5 = _safe_float(metadata.get("momentum_5")) or 0.0
    trend_strength = _safe_float(metadata.get("trend_strength")) or 0.0
    volatility = _safe_float(metadata.get("volatility")) or 0.0
    pullback = _safe_float(metadata.get("pullback_from_high")) or 0.0
    if volatility > model.max_normalized_volatility and momentum < model.high_vol_momentum_exception:
        return None

    source_score = 0.0
    confidence_sum = 0.0
    for insight in insights:
        alpha_id = str(getattr(insight, "alpha_id", ""))
        alpha_weight = ALPHA_WEIGHTS.get(alpha_id, 0.45)
        score = _safe_float(getattr(insight, "score", None))
        if score is None:
            score = _safe_float(getattr(insight, "magnitude", None)) or momentum
        confidence = _safe_float(getattr(insight, "confidence", None)) or 0.5
        source_score += max(score, 0.0) * alpha_weight * confidence
        confidence_sum += alpha_weight
    source_score = source_score / confidence_sum if confidence_sum > 0 else 0.0
    quality = (
        source_score
        + model.recent_momentum_weight * max(momentum_5, 0.0)
        + model.trend_weight * max(trend_strength, 0.0)
        + model.multi_alpha_bonus * max(len(insights) - 1, 0)
        - model.volatility_penalty * max(volatility - 0.10, 0.0)
        - model.drawdown_penalty * max(pullback, 0.0)
    )
    if quality <= 0:
        return None
    return {
        "symbol": best.symbol,
        "symbol_key": symbol_key,
        "quality": quality,
        "momentum": momentum,
        "volatility": volatility,
        "market_breadth": _safe_float(metadata.get("market_breadth")),
    }


def _zero_missing_held_targets(
    context: PortfolioConstructionContext,
    targets: dict[str, PortfolioAllocationTarget],
    model: ResearchAdaptivePortfolioConstructionModel,
) -> tuple[PortfolioAllocationTarget, ...]:
    if model.emit_zero_for_missing_held_targets:
        target_currencies = {currency_for_symbol(target.symbol) for target in targets.values()}
        for symbol in context.portfolio.held_symbols:
            if symbol.key in targets:
                continue
            if target_currencies and currency_for_symbol(symbol) not in target_currencies:
                continue
            targets[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=0.0,
                tag=f"adaptive:{model.model_name}:no_longer_in_target_portfolio",
            )
    return tuple(targets.values())


def _etf_safety_targets(
    context: PortfolioConstructionContext,
    model: ResearchAdaptivePortfolioConstructionModel,
) -> tuple[dict[str, PortfolioAllocationTarget], float | None]:
    if not model.enable_etf_safety_bucket:
        return {}, None

    latest: dict[str, Any] = {}
    for insight in context.active_insights:
        if str(getattr(insight, "alpha_id", "")) != model.etf_safety_alpha_id:
            continue
        if getattr(getattr(insight, "direction", None), "value", "") != "up":
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at >= previous.generated_at:
            latest[insight.symbol_key] = insight

    targets: dict[str, PortfolioAllocationTarget] = {}
    stock_gross_cap: float | None = None
    for insight in latest.values():
        metadata = dict(getattr(insight, "metadata", {}) or {})
        target_pct = _safe_float(metadata.get("target_bucket_pct"))
        if target_pct is None:
            target_pct = _safe_float(getattr(insight, "weight", None))
        if target_pct is None or target_pct <= 0:
            continue
        target_role = str(metadata.get("target_role") or "etf").strip() or "etf"
        stock_cap = _safe_float(metadata.get("stock_gross_cap"))
        if stock_cap is not None:
            stock_gross_cap = stock_cap if stock_gross_cap is None else min(stock_gross_cap, stock_cap)
        targets[insight.symbol_key] = PortfolioAllocationTarget(
            symbol=insight.symbol,
            target_percent=_clamp_pct(target_pct),
            tag=(
                f"adaptive:{model.model_name}:etf_safety:{target_role}:"
                f"regime={metadata.get('safety_regime', 'unknown')}"
            ),
        )

    total = sum(target.target_percent for target in targets.values())
    max_total = _clamp_pct(model.etf_safety_max_total_pct)
    if total > max_total > 0:
        scale = max_total / total
        targets = {
            symbol_key: PortfolioAllocationTarget(
                symbol=target.symbol,
                target_percent=_clamp_pct(target.target_percent * scale),
                tag=f"{target.tag}:scaled={scale:.3f}",
            )
            for symbol_key, target in targets.items()
        }
    return targets, stock_gross_cap


def _latest_non_up_symbol_keys(insights: tuple[Any, ...]) -> set[str]:
    latest: dict[str, Any] = {}
    for insight in insights:
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at >= previous.generated_at:
            latest[insight.symbol_key] = insight
    return {
        symbol_key
        for symbol_key, insight in latest.items()
        if getattr(getattr(insight, "direction", None), "value", "") != "up"
    }


def _merged_metadata(insights: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for insight in sorted(insights, key=lambda item: item.generated_at):
        merged.update(dict(getattr(insight, "metadata", {}) or {}))
    return merged


def _is_plausible(insight: Any) -> bool:
    metadata = getattr(insight, "metadata", {}) or {}
    for key in ("momentum", "momentum_5", "momentum_60", "trend_strength"):
        value = _safe_float(metadata.get(key))
        if value is not None and (not math.isfinite(value) or abs(value) > 3.0):
            return False
    score = _safe_float(getattr(insight, "score", None))
    return score is None or (math.isfinite(score) and abs(score) <= 3.0)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _clamp_pct(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None

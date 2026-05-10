from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-etf-rotation"
VERSION = "0.2.0"
HORIZON = timedelta(days=20)
MAX_SELECTED = 4
MIN_RISK_ADJUSTED_SCORE = 0.0
EMIT_FLAT_FOR_UNSELECTED = True


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    rejected_symbols: list[str] = []
    for symbol_key in context.symbol_keys:
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        slow_average = _first_value(context, symbol_key, ("sma_20_close", "sma_5_close"))
        momentum = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close", "momentum_5_close"))
        volatility = _first_value(context, symbol_key, ("stddev_20_close", "volatility_20_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_1"))
        if close is None or slow_average is None or momentum is None:
            rejected_symbols.append(symbol_key)
            continue
        if close < slow_average:
            rejected_symbols.append(symbol_key)
            continue
        normalized_volatility = 0.0 if volatility is None or close <= 0 else volatility / close
        volatility_penalty = min(normalized_volatility, 0.30) * 0.45
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.05)
        score = momentum - volatility_penalty + liquidity_bonus
        if score < MIN_RISK_ADJUSTED_SCORE:
            rejected_symbols.append(symbol_key)
            continue
        candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "moving_average": slow_average,
                "momentum": momentum,
                "volatility": normalized_volatility,
                "liquidity": liquidity or 0.0,
                "score": score,
            }
        )

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    selected_keys = {str(item["symbol_key"]) for item in selected}
    total_score = sum(max(float(item["score"]), 0.0) for item in selected)

    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        symbol_key = str(item["symbol_key"])
        score = float(item["score"])
        selected_weight = (max(score, 0.0) / total_score) if total_score > 0 else (1.0 / len(selected))
        insights.append(
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + HORIZON,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=ALPHA_ID,
                alpha_version=VERSION,
                magnitude=float(item["momentum"]),
                confidence=min(0.9, 0.55 + max(score, 0.0) * 2.0),
                weight=selected_weight,
                score=score,
                reason="risk_adjusted_etf_rotation_score",
                metadata={
                    "close": item["close"],
                    "moving_average": item["moving_average"],
                    "momentum": item["momentum"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "rank": rank,
                    "rank_count": len(selected),
                },
            )
        )

    if EMIT_FLAT_FOR_UNSELECTED:
        for symbol_key in context.symbol_keys:
            if symbol_key in selected_keys:
                continue
            insights.append(
                Insight(
                    sleeve_id=context.sleeve_id,
                    symbol=context.symbol(symbol_key),
                    direction=InsightDirection.FLAT,
                    generated_at=context.as_of,
                    expires_at=context.as_of + HORIZON,
                    source_snapshot_id=context.source_snapshot_id,
                    alpha_id=ALPHA_ID,
                    alpha_version=VERSION,
                    confidence=0.6,
                    weight=0.0,
                    score=0.0,
                    reason="not_selected_by_etf_rotation",
                    metadata={"rejected_due_to_missing_or_negative_momentum": symbol_key in rejected_symbols},
                )
            )
    return insights


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

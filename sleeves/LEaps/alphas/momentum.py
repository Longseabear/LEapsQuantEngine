from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-momentum"
VERSION = "0.2.0"
HORIZON = timedelta(days=5)
MIN_RISK_ADJUSTED_SCORE = 0.005
MAX_PER_CURRENCY = 6
EMIT_FLAT_FOR_REJECTED = False


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    rejected: dict[str, str] = {}
    for symbol_key in context.symbol_keys:
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_5_close", "sma_3_close"))
        slow_average = _first_value(context, symbol_key, ("sma_20_close", "sma_5_close", "sma_3_close"))
        momentum_5 = _first_value(context, symbol_key, ("momentum_5_close", "momentum_2_close"))
        momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close", "momentum_5_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_2", "dollar_volume_1"))
        if close is None or fast_average is None or slow_average is None or momentum_20 is None:
            rejected[symbol_key] = "missing_required_indicator"
            continue
        if close <= slow_average or fast_average < slow_average:
            rejected[symbol_key] = "trend_filter_failed"
            continue

        acceleration = 0.0 if momentum_5 is None else momentum_5
        volatility = _normalized_volatility(context, symbol_key, close)
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 2_000_000_000.0, 0.04)
        risk_penalty = min(max(volatility, 0.0), 0.30) * 0.35
        score = (momentum_20 * 0.70) + (acceleration * 0.30) + liquidity_bonus - risk_penalty
        if score < MIN_RISK_ADJUSTED_SCORE:
            rejected[symbol_key] = "risk_adjusted_score_too_low"
            continue
        candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "fast_average": fast_average,
                "slow_average": slow_average,
                "momentum": momentum_20,
                "momentum_5": acceleration,
                "volatility": volatility,
                "liquidity": liquidity or 0.0,
                "score": score,
            }
        )

    selected_keys: set[str] = set()
    insights: list[Insight] = []
    for bucket in _group_by_currency(candidates).values():
        selected = sorted(bucket, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
            :MAX_PER_CURRENCY
        ]
        for rank, item in enumerate(selected, start=1):
            symbol_key = str(item["symbol_key"])
            selected_keys.add(symbol_key)
            score = float(item["score"])
            momentum = float(item["momentum"])
            volatility = float(item["volatility"])
            confidence = min(0.92, 0.55 + max(score, 0.0) * 2.5)
            weight = min(0.18, max(0.03, score / max(volatility, 0.03)))
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
                    magnitude=momentum,
                    confidence=confidence,
                    weight=weight,
                    score=score,
                    reason="risk_adjusted_trend_momentum",
                    metadata={
                        "close": item["close"],
                        "moving_average": item["slow_average"],
                        "fast_average": item["fast_average"],
                        "momentum": momentum,
                        "momentum_5": item["momentum_5"],
                        "volatility": volatility,
                        "liquidity": item["liquidity"],
                        "rank": rank,
                        "bucket_rank_count": len(selected),
                    },
                )
            )

    if EMIT_FLAT_FOR_REJECTED:
        for symbol_key, reason in rejected.items():
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
                    confidence=0.55,
                    weight=0.0,
                    score=0.0,
                    reason=reason,
                    metadata={"rejected_by": ALPHA_ID},
                )
            )
    return insights


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float | None) -> float:
    if close is None or close <= 0:
        return 0.0
    values = []
    stddev = _first_value(context, symbol_key, ("stddev_20_close", "volatility_20_close"))
    atr = _first_value(context, symbol_key, ("atr_14", "average_true_range_14"))
    if stddev is not None:
        values.append(stddev / close)
    if atr is not None:
        values.append(atr / close)
    return max(values) if values else 0.0


def _group_by_currency(candidates: list[dict[str, float | str]]) -> dict[str, list[dict[str, float | str]]]:
    grouped: dict[str, list[dict[str, float | str]]] = {}
    for item in candidates:
        symbol_key = str(item["symbol_key"])
        grouped.setdefault("KRW" if symbol_key.startswith("KRX:") else "USD", []).append(item)
    return grouped


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "us_etf_rotation"
VERSION = "0.3.0"
EVALUATION_CADENCE = "once_per_month"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=40)
MAX_SELECTED = 3
MIN_RISK_ADJUSTED_SCORE = 0.0
EMIT_FLAT_FOR_UNSELECTED = True
DEFENSIVE_TICKERS = {"TLT", "IEF", "GLD", "USMV", "XLP", "XLU"}


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    rejected_symbols: list[str] = []
    risk_on = _market_risk_on(context)
    for symbol_key in context.symbol_keys:
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        trend_average = _first_value(context, symbol_key, ("sma_200_close", "sma_100_close", "sma_20_close"))
        momentum_3m = _first_value(context, symbol_key, ("roc_63_close", "roc_60_close", "roc_20_close"))
        momentum_6m = _first_value(context, symbol_key, ("roc_126_close", "roc_120_close", "roc_63_close"))
        momentum_12m = _first_value(context, symbol_key, ("roc_252_close", "roc_240_close", "roc_126_close"))
        volatility = _first_value(context, symbol_key, ("stddev_63_close", "stddev_20_close", "volatility_20_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_1"))
        if close is None or trend_average is None or momentum_3m is None or momentum_6m is None:
            rejected_symbols.append(symbol_key)
            continue

        defensive = _is_defensive(symbol_key)
        trend_confirmed = close > trend_average
        composite_momentum = (0.45 * momentum_6m) + (0.35 * momentum_3m) + (0.20 * (momentum_12m or momentum_6m))
        if not trend_confirmed:
            rejected_symbols.append(symbol_key)
            continue
        if not defensive and not risk_on:
            rejected_symbols.append(symbol_key)
            continue
        if composite_momentum <= 0 and not defensive:
            rejected_symbols.append(symbol_key)
            continue

        normalized_volatility = 0.0 if volatility is None or close <= 0 else volatility / close
        volatility_penalty = min(normalized_volatility, 0.30) * (0.55 if defensive else 0.75)
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.05)
        defensive_bonus = 0.05 if defensive and not risk_on else 0.0
        score = composite_momentum - volatility_penalty + liquidity_bonus + defensive_bonus
        if score < MIN_RISK_ADJUSTED_SCORE:
            rejected_symbols.append(symbol_key)
            continue
        candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "moving_average": trend_average,
                "momentum": composite_momentum,
                "momentum_3m": momentum_3m,
                "momentum_6m": momentum_6m,
                "momentum_12m": momentum_12m or 0.0,
                "volatility": normalized_volatility,
                "liquidity": liquidity or 0.0,
                "risk_on": 1.0 if risk_on else 0.0,
                "defensive": 1.0 if defensive else 0.0,
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
                    "momentum_3m": item["momentum_3m"],
                    "momentum_6m": item["momentum_6m"],
                    "momentum_12m": item["momentum_12m"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "risk_on": bool(item["risk_on"]),
                    "defensive": bool(item["defensive"]),
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


def _market_risk_on(context: SnapshotContext) -> bool:
    close = _first_value(context, "US:SPY", ("identity_close", "close"))
    trend = _first_value(context, "US:SPY", ("sma_200_close", "sma_100_close", "sma_20_close"))
    momentum = _first_value(context, "US:SPY", ("roc_126_close", "roc_63_close", "roc_20_close"))
    if close is None or trend is None or momentum is None:
        return False
    return close > trend and momentum > 0


def _is_defensive(symbol_key: str) -> bool:
    ticker = symbol_key.split(":", 1)[-1].upper()
    return ticker in DEFENSIVE_TICKERS


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-us-stability-hedge"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=10)
MAX_SELECTED = 3
MIN_SCORE = -0.02

DEFENSIVE_SEGMENT_BONUS = {
    "minimum_volatility": 0.08,
    "dividend_quality": 0.07,
    "short_duration_treasury": 0.06,
    "intermediate_treasury": 0.05,
    "gold": 0.04,
    "us_large_cap": 0.03,
    "technology": -0.02,
    "semiconductor": -0.04,
    "us_growth": -0.03,
    "us_small_cap": -0.02,
}


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("US:"):
            continue
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_5_close"))
        slow_average = _first_value(context, symbol_key, ("sma_20_close", "sma_5_close"))
        momentum = _first_value(context, symbol_key, ("roc_20_close", "momentum_5_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "volume"))
        if close is None or fast_average is None or slow_average is None or momentum is None:
            continue

        volatility = _normalized_volatility(context, symbol_key, close)
        trend_score = 0.04 if close >= slow_average and fast_average >= slow_average else -0.03
        segment = _segment(symbol_key)
        segment_bonus = DEFENSIVE_SEGMENT_BONUS.get(segment, 0.0)
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 2_000_000_000.0, 0.03)
        score = segment_bonus + trend_score + min(momentum, 0.12) * 0.35 + liquidity_bonus - volatility * 1.25
        if score < MIN_SCORE:
            continue
        candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "fast_average": fast_average,
                "slow_average": slow_average,
                "momentum": momentum,
                "volatility": volatility,
                "liquidity": liquidity or 0.0,
                "segment": segment,
                "score": score,
            }
        )

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        score = float(item["score"])
        symbol_key = str(item["symbol_key"])
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
                confidence=min(0.88, 0.58 + max(score, 0.0) * 1.8),
                weight=min(0.30, max(0.08, score / max(float(item["volatility"]), 0.05))),
                score=score,
                group_id="usd-stability",
                reason="us_stability_hedge_score",
                metadata={
                    "role": "usd_stability_hedge",
                    "segment": item["segment"],
                    "close": item["close"],
                    "fast_average": item["fast_average"],
                    "slow_average": item["slow_average"],
                    "momentum": item["momentum"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "rank": rank,
                    "selected_count": len(selected),
                },
            )
        )
    return insights


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float | None) -> float:
    if close is None or close <= 0:
        return 0.0
    stddev = _first_value(context, symbol_key, ("stddev_20_close",))
    atr = _first_value(context, symbol_key, ("atr_14",))
    values = []
    if stddev is not None:
        values.append(stddev / close)
    if atr is not None:
        values.append(atr / close)
    return max(values) if values else 0.0


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _segment(symbol_key: str) -> str:
    ticker = symbol_key.split(":", 1)[1]
    return {
        "USMV": "minimum_volatility",
        "SPLV": "minimum_volatility",
        "VIG": "dividend_quality",
        "SCHD": "dividend_quality",
        "SHY": "short_duration_treasury",
        "IEF": "intermediate_treasury",
        "TLT": "intermediate_treasury",
        "GLD": "gold",
        "SPY": "us_large_cap",
        "QQQ": "us_growth",
        "IWM": "us_small_cap",
        "SMH": "semiconductor",
        "XLK": "technology",
    }.get(ticker, "single_stock")

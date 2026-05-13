from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-kospi-conviction"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=10)
MAX_SELECTED = 5
MIN_SCORE = 0.025
KOSPI_BIAS_BONUS = 0.04
MAX_NORMALIZED_VOLATILITY = 0.16
EXTREME_NORMALIZED_VOLATILITY = 0.22
HIGH_VOL_MOMENTUM_EXCEPTION = 0.45
HIGH_VOL_TREND_EXCEPTION = 0.18


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    raw_candidates: list[dict[str, float | str]] = []
    krx_count = 0
    trend_count = 0
    positive_momentum_sum = 0.0
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("KRX:"):
            continue
        krx_count += 1
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_5_close"))
        slow_average = _first_value(context, symbol_key, ("sma_20_close", "sma_5_close"))
        momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
        momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close", "momentum_5_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "volume"))
        if close is None or fast_average is None or slow_average is None or momentum_20 is None:
            continue

        trend_strength = (close / slow_average) - 1.0 if slow_average > 0 else 0.0
        if trend_strength <= 0.0 or fast_average < slow_average:
            continue
        trend_count += 1
        positive_momentum_sum += max(momentum_20, 0.0)

        volatility = _normalized_volatility(context, symbol_key, close)
        if _volatility_blocks_entry(
            volatility=volatility,
            momentum=momentum_20,
            trend_strength=trend_strength,
        ):
            continue
        acceleration = momentum_5 or 0.0
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 4_000_000_000_000.0, 0.04)
        raw_candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "fast_average": fast_average,
                "slow_average": slow_average,
                "momentum": momentum_20,
                "momentum_5": acceleration,
                "trend_strength": trend_strength,
                "volatility": volatility,
                "liquidity": liquidity or 0.0,
                "liquidity_bonus": liquidity_bonus,
            }
        )

    market_breadth = trend_count / krx_count if krx_count else 0.0
    average_positive_momentum = positive_momentum_sum / trend_count if trend_count else 0.0
    market_conviction_bonus = min(max((market_breadth - 0.50) * 0.08, 0.0), 0.04)
    market_conviction_bonus += min(max(average_positive_momentum, 0.0) * 0.15, 0.03)

    candidates: list[dict[str, float | str]] = []
    for item in raw_candidates:
        momentum_20 = float(item["momentum"])
        acceleration = float(item["momentum_5"])
        trend_strength = float(item["trend_strength"])
        volatility = float(item["volatility"])
        score = (
            KOSPI_BIAS_BONUS
            + market_conviction_bonus
            + (momentum_20 * 0.55)
            + (acceleration * 0.25)
            + (trend_strength * 0.20)
            + float(item["liquidity_bonus"])
            - min(volatility, 0.35) * 0.55
        )
        if score < MIN_SCORE:
            continue
        candidate = dict(item)
        candidate["score"] = score
        candidate["market_breadth"] = market_breadth
        candidate["average_positive_momentum"] = average_positive_momentum
        candidate["market_conviction_bonus"] = market_conviction_bonus
        candidates.append(candidate)

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        score = float(item["score"])
        momentum = float(item["momentum"])
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
                magnitude=momentum,
                confidence=min(0.97, 0.62 + max(score, 0.0) * 1.35),
                weight=min(0.45, max(0.10, score / max(float(item["volatility"]), 0.07))),
                score=score,
                group_id="krw-growth",
                reason="kospi_conviction_breadth_trend_momentum",
                metadata={
                    "role": "krw_growth_engine",
                    "close": item["close"],
                    "fast_average": item["fast_average"],
                    "slow_average": item["slow_average"],
                    "momentum": momentum,
                    "momentum_5": item["momentum_5"],
                    "trend_strength": item["trend_strength"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "rank": rank,
                    "selected_count": len(selected),
                    "kospi_bias_bonus": KOSPI_BIAS_BONUS,
                    "market_breadth": item["market_breadth"],
                    "average_positive_momentum": item["average_positive_momentum"],
                    "market_conviction_bonus": item["market_conviction_bonus"],
                    "volatility_filter": "passed",
                },
            )
        )
    return insights


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float | None) -> float:
    if close is None or close <= 0:
        return 0.0
    values = []
    stddev = _first_value(context, symbol_key, ("stddev_20_close",))
    atr = _first_value(context, symbol_key, ("atr_14",))
    if stddev is not None:
        values.append(stddev / close)
    if atr is not None:
        values.append(atr / close)
    return max(values) if values else 0.0


def _volatility_blocks_entry(*, volatility: float, momentum: float, trend_strength: float) -> bool:
    if volatility >= EXTREME_NORMALIZED_VOLATILITY:
        return True
    if volatility <= MAX_NORMALIZED_VOLATILITY:
        return False
    return momentum < HIGH_VOL_MOMENTUM_EXCEPTION or trend_strength < HIGH_VOL_TREND_EXCEPTION


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

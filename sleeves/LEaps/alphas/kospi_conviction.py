from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-kospi-conviction"
VERSION = "0.2.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=10)
MAX_SELECTED = 8
MIN_SCORE = 0.025
KOSPI_BIAS_BONUS = 0.04
MAX_NORMALIZED_VOLATILITY = 0.16
EXTREME_NORMALIZED_VOLATILITY = 0.22
HIGH_VOL_MOMENTUM_EXCEPTION = 0.45
HIGH_VOL_TREND_EXCEPTION = 0.18
SECTOR_STRENGTH_WEIGHT = 0.18
ENTRY_TIMING_WEIGHT = 0.12
MAX_HEALTHY_PULLBACK = 0.14
MIN_HEALTHY_PULLBACK = 0.012
MAX_REBREAK_DISTANCE = 0.04
MIN_REBREAK_MOMENTUM_5 = 0.015
MAX_PLAUSIBLE_DAILY_FEATURE_ABS = 3.0
SECTOR_BY_SYMBOL_KEY = {
    "KRX:005930": "technology",
    "KRX:000660": "technology",
    "KRX:006400": "technology",
    "KRX:005380": "consumer_discretionary",
    "KRX:000270": "consumer_discretionary",
    "KRX:035420": "communication_services",
    "KRX:035720": "communication_services",
    "KRX:068270": "health_care",
    "KRX:207940": "health_care",
    "KRX:051910": "materials",
    "KRX:105560": "financials",
    "KRX:055550": "financials",
    "KRX:086790": "financials",
    "KRX:028260": "industrials",
    "KRX:034020": "industrials",
    "KRX:012450": "industrials",
}


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
        momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "momentum_60_close"))
        rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
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
        intermediate_momentum = momentum_60 if momentum_60 is not None else momentum_20
        if _has_implausible_daily_feature(momentum_20, acceleration, intermediate_momentum, trend_strength):
            continue
        recency_weighted_momentum = (
            (momentum_20 * 0.50)
            + (acceleration * 0.30)
            + (intermediate_momentum * 0.20)
        )
        entry_timing = _entry_timing_score(
            close=close,
            fast_average=fast_average,
            rolling_high=rolling_high,
            momentum_5=acceleration,
        )
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 4_000_000_000_000.0, 0.04)
        raw_candidates.append(
            {
                "symbol_key": symbol_key,
                "sector": SECTOR_BY_SYMBOL_KEY.get(symbol_key, "unknown"),
                "close": close,
                "fast_average": fast_average,
                "slow_average": slow_average,
                "momentum": momentum_20,
                "momentum_5": acceleration,
                "momentum_60": intermediate_momentum,
                "recency_weighted_momentum": recency_weighted_momentum,
                "trend_strength": trend_strength,
                "volatility": volatility,
                "rolling_high": rolling_high or 0.0,
                "entry_timing_score": entry_timing["score"],
                "entry_timing_setup": entry_timing["setup"],
                "pullback_from_high": entry_timing["pullback_from_high"],
                "distance_to_fast": entry_timing["distance_to_fast"],
                "liquidity": liquidity or 0.0,
                "liquidity_bonus": liquidity_bonus,
            }
        )

    market_breadth = trend_count / krx_count if krx_count else 0.0
    average_positive_momentum = positive_momentum_sum / trend_count if trend_count else 0.0
    market_conviction_bonus = min(max((market_breadth - 0.50) * 0.08, 0.0), 0.04)
    market_conviction_bonus += min(max(average_positive_momentum, 0.0) * 0.15, 0.03)

    candidates: list[dict[str, float | str]] = []
    sector_strength = _sector_relative_strength(raw_candidates)
    for item in raw_candidates:
        recency_weighted_momentum = float(item["recency_weighted_momentum"])
        trend_strength = float(item["trend_strength"])
        volatility = float(item["volatility"])
        sector_score = sector_strength.get(str(item["sector"]), 0.0)
        score = (
            KOSPI_BIAS_BONUS
            + market_conviction_bonus
            + (recency_weighted_momentum * 0.65)
            + (trend_strength * 0.22)
            + (sector_score * SECTOR_STRENGTH_WEIGHT)
            + (float(item["entry_timing_score"]) * ENTRY_TIMING_WEIGHT)
            + float(item["liquidity_bonus"])
            - min(volatility, 0.35) * 0.55
        )
        if score < MIN_SCORE:
            continue
        candidate = dict(item)
        candidate["score"] = score
        candidate["sector_relative_strength"] = sector_score
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
                    "sector": item["sector"],
                    "fast_average": item["fast_average"],
                    "slow_average": item["slow_average"],
                    "momentum": momentum,
                    "momentum_5": item["momentum_5"],
                    "momentum_60": item["momentum_60"],
                    "recency_weighted_momentum": item["recency_weighted_momentum"],
                    "trend_strength": item["trend_strength"],
                    "volatility": item["volatility"],
                    "rolling_high": item["rolling_high"],
                    "entry_timing_score": item["entry_timing_score"],
                    "entry_timing_setup": item["entry_timing_setup"],
                    "pullback_from_high": item["pullback_from_high"],
                    "distance_to_fast": item["distance_to_fast"],
                    "liquidity": item["liquidity"],
                    "rank": rank,
                    "selected_count": len(selected),
                    "kospi_bias_bonus": KOSPI_BIAS_BONUS,
                    "sector_relative_strength": item["sector_relative_strength"],
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


def _entry_timing_score(
    *,
    close: float,
    fast_average: float,
    rolling_high: float | None,
    momentum_5: float,
) -> dict[str, float | str]:
    pullback_from_high = 0.0
    if rolling_high is not None and rolling_high > 0:
        pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
    distance_to_fast = 0.0 if close <= 0 else (fast_average / close) - 1.0
    pullback_score = 0.0
    if MIN_HEALTHY_PULLBACK <= pullback_from_high <= MAX_HEALTHY_PULLBACK:
        pullback_score = min(pullback_from_high / MAX_HEALTHY_PULLBACK, 1.0)
    rebreak_score = 0.0
    if pullback_from_high <= MAX_REBREAK_DISTANCE and momentum_5 >= MIN_REBREAK_MOMENTUM_5:
        rebreak_score = min((MAX_REBREAK_DISTANCE - pullback_from_high) / MAX_REBREAK_DISTANCE, 1.0)
    if rebreak_score > pullback_score:
        return {
            "score": rebreak_score,
            "setup": "rebreak",
            "pullback_from_high": pullback_from_high,
            "distance_to_fast": distance_to_fast,
        }
    if pullback_score > 0:
        return {
            "score": pullback_score,
            "setup": "pullback",
            "pullback_from_high": pullback_from_high,
            "distance_to_fast": distance_to_fast,
        }
    return {
        "score": 0.0,
        "setup": "trend",
        "pullback_from_high": pullback_from_high,
        "distance_to_fast": distance_to_fast,
    }


def _sector_relative_strength(candidates: list[dict[str, float | str]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for item in candidates:
        sector = str(item["sector"])
        totals[sector] = totals.get(sector, 0.0) + float(item["recency_weighted_momentum"])
        counts[sector] = counts.get(sector, 0) + 1
    return {
        sector: totals[sector] / counts[sector]
        for sector in totals
        if counts.get(sector, 0) > 0
    }


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _has_implausible_daily_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_DAILY_FEATURE_ABS for value in values)

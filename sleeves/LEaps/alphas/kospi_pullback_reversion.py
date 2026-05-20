from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-kospi-pullback-reversion"
VERSION = "0.3.0"
EVALUATION_CADENCE = "every_15_minutes"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=2)
MAX_SELECTED = 4
MIN_SCORE = 0.07
MIN_TREND_MOMENTUM = 0.08
MAX_PULLBACK_DEPTH = 0.16
MIN_PULLBACK_DEPTH = 0.015
MAX_NORMALIZED_VOLATILITY = 0.17
MAX_REBREAK_DISTANCE = 0.05
MIN_REBREAK_MOMENTUM_5 = 0.015
VOLATILITY_STABILIZATION_START = 0.11
MAX_VOLATILE_PULLBACK = 0.19
VOLATILE_REBREAK_MIN_MOMENTUM_5 = 0.02
VOLATILE_PULLBACK_MAX_DEPTH = 0.09
VOLATILE_PULLBACK_MAX_FAST_DISTANCE = 0.035
MAX_PLAUSIBLE_DAILY_FEATURE_ABS = 3.0


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("KRX:"):
            continue
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_5_close"))
        slow_average = _first_value(context, symbol_key, ("sma_20_close", "sma_5_close"))
        momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
        momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
        rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
        rolling_low = _first_value(context, symbol_key, ("rolling_min_20_close",))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "volume"))
        if (
            close is None
            or fast_average is None
            or slow_average is None
            or momentum_20 is None
            or rolling_high is None
            or close <= 0
            or slow_average <= 0
            or rolling_high <= 0
        ):
            continue

        trend_strength = (close / slow_average) - 1.0
        if _has_implausible_daily_feature(momentum_20, momentum_5 or 0.0, trend_strength):
            continue
        if trend_strength <= 0.0 or fast_average < slow_average or momentum_20 < MIN_TREND_MOMENTUM:
            continue

        pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
        distance_to_fast = (fast_average / close) - 1.0
        short_reversal_pressure = max(-(momentum_5 or 0.0), 0.0)
        timing = _entry_timing(
            close=close,
            fast_average=fast_average,
            momentum_5=momentum_5 or 0.0,
            pullback_from_high=pullback_from_high,
            distance_to_fast=distance_to_fast,
            short_reversal_pressure=short_reversal_pressure,
        )
        if timing is None:
            continue
        volatility = _normalized_volatility(context, symbol_key, close)
        volatility_profile = _volatility_profile(
            volatility=volatility,
            setup=str(timing["setup"]),
            momentum_5=momentum_5 or 0.0,
            pullback_from_high=pullback_from_high,
            distance_to_fast=distance_to_fast,
        )
        if volatility_profile is None:
            continue
        if rolling_low is not None and rolling_low > 0 and close <= rolling_low * 1.01:
            continue

        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 4_000_000_000_000.0, 0.035)
        score = (
            0.035
            + (trend_strength * 0.40)
            + (momentum_20 * 0.22)
            + (timing["score"] * 0.85)
            + liquidity_bonus
            + float(volatility_profile["adjustment"])
            - (volatility * 0.42)
        )
        if score < MIN_SCORE:
            continue
        candidates.append(
            {
                "symbol_key": symbol_key,
                "close": close,
                "fast_average": fast_average,
                "slow_average": slow_average,
                "momentum": momentum_20,
                "momentum_5": momentum_5 or 0.0,
                "trend_strength": trend_strength,
                "volatility": volatility,
                "pullback_depth": timing["score"],
                "entry_setup": timing["setup"],
                "pullback_from_high": pullback_from_high,
                "distance_to_fast": distance_to_fast,
                "short_reversal_pressure": short_reversal_pressure,
                "volatility_regime": volatility_profile["regime"],
                "volatility_regime_adjustment": volatility_profile["adjustment"],
                "rolling_high": rolling_high,
                "rolling_low": rolling_low or 0.0,
                "liquidity": liquidity or 0.0,
                "score": score,
            }
        )

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        score = float(item["score"])
        volatility = float(item["volatility"])
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
                magnitude=float(item["pullback_depth"]),
                confidence=min(0.92, 0.56 + score * 1.7),
                weight=min(0.30, max(0.06, score / max(volatility * 1.8, 0.08))),
                score=score,
                group_id="krw-growth",
                reason="kospi_pullback_reversion_in_uptrend",
                metadata=_with_temporal_features(
                    context,
                    symbol_key,
                    {
                        "role": "krw_pullback_reversion",
                        "close": item["close"],
                        "fast_average": item["fast_average"],
                        "slow_average": item["slow_average"],
                        "momentum": item["momentum"],
                        "momentum_5": item["momentum_5"],
                        "trend_strength": item["trend_strength"],
                        "volatility": item["volatility"],
                        "pullback_depth": item["pullback_depth"],
                        "entry_setup": item["entry_setup"],
                        "pullback_from_high": item["pullback_from_high"],
                        "distance_to_fast": item["distance_to_fast"],
                        "short_reversal_pressure": item["short_reversal_pressure"],
                        "volatility_regime": item["volatility_regime"],
                        "volatility_regime_adjustment": item["volatility_regime_adjustment"],
                        "rolling_high": item["rolling_high"],
                        "rolling_low": item["rolling_low"],
                        "liquidity": item["liquidity"],
                        "rank": rank,
                        "selected_count": len(selected),
                    },
                ),
            )
        )
    return insights


def _with_temporal_features(context: SnapshotContext, symbol_key: str, metadata: dict) -> dict:
    temporal_features = context.metadata_value(symbol_key, "rl_temporal_features")
    if not isinstance(temporal_features, (list, tuple)) or not temporal_features:
        return metadata
    enriched = dict(metadata)
    enriched["rl_temporal_features"] = list(temporal_features)
    return enriched


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


def _entry_timing(
    *,
    close: float,
    fast_average: float,
    momentum_5: float,
    pullback_from_high: float,
    distance_to_fast: float,
    short_reversal_pressure: float,
) -> dict[str, float | str] | None:
    pullback_depth = max(pullback_from_high, distance_to_fast, short_reversal_pressure)
    if (
        pullback_from_high <= MAX_REBREAK_DISTANCE
        and momentum_5 >= MIN_REBREAK_MOMENTUM_5
        and close >= fast_average * 0.995
    ):
        rebreak_score = max(
            MIN_PULLBACK_DEPTH,
            min(MAX_REBREAK_DISTANCE - pullback_from_high + momentum_5, MAX_PULLBACK_DEPTH),
        )
        return {"setup": "rebreak", "score": rebreak_score}
    if MIN_PULLBACK_DEPTH <= pullback_depth <= MAX_PULLBACK_DEPTH:
        return {"setup": "pullback", "score": pullback_depth}
    return None


def _volatility_profile(
    *,
    volatility: float,
    setup: str,
    momentum_5: float,
    pullback_from_high: float,
    distance_to_fast: float,
) -> dict[str, float | str] | None:
    if volatility <= VOLATILITY_STABILIZATION_START:
        return {"regime": "normal", "adjustment": 0.0}
    if volatility > MAX_VOLATILE_PULLBACK:
        return None
    if setup == "rebreak" and momentum_5 >= VOLATILE_REBREAK_MIN_MOMENTUM_5:
        adjustment = -min(max(volatility - VOLATILITY_STABILIZATION_START, 0.0) * 0.12, 0.02)
        return {"regime": "volatile_rebreak", "adjustment": adjustment}
    if (
        momentum_5 < 0.0
        or pullback_from_high > VOLATILE_PULLBACK_MAX_DEPTH
        or distance_to_fast > VOLATILE_PULLBACK_MAX_FAST_DISTANCE
    ):
        return None
    adjustment = -(0.02 + min(max(volatility - VOLATILITY_STABILIZATION_START, 0.0) * 0.22, 0.04))
    return {"regime": "volatile_stabilized_pullback", "adjustment": adjustment}


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _has_implausible_daily_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_DAILY_FEATURE_ABS for value in values)

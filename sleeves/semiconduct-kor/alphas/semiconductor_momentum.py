from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "semiconduct-kor-momentum"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=5)

MAX_SELECTED = 6
MIN_SCORE = 0.035
MIN_MOMENTUM_20 = 0.015
MIN_LIQUIDITY = 1_000_000_000.0
MAX_NORMALIZED_VOLATILITY = 0.22
EXTREME_NORMALIZED_VOLATILITY = 0.32
MAX_HEALTHY_PULLBACK = 0.16
MAX_PLAUSIBLE_DAILY_FEATURE_ABS = 3.0


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("KRX:"):
            continue
        item = _features(context, symbol_key)
        if item is None or not _is_buyable(item):
            continue
        score = _score(item)
        if score < MIN_SCORE:
            continue
        candidates.append({**item, "score": score})

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        score = float(item["score"])
        momentum = float(item["momentum_20"])
        insights.append(
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(str(item["symbol_key"])),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + HORIZON,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=ALPHA_ID,
                alpha_version=VERSION,
                magnitude=momentum,
                confidence=min(0.94, 0.58 + max(score, 0.0) * 1.5),
                weight=min(0.24, max(0.06, score)),
                score=score,
                group_id="krw-semiconductor",
                reason="semiconductor_momentum_trend",
                metadata=_metadata(item, rank=rank, selected_count=len(selected)),
            )
        )
    return insights


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float | str] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_10_close"))
    slow_average = _first_value(context, symbol_key, ("sma_20_close",))
    momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "momentum_60_close"))
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
    liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "volume")) or 0.0
    if (
        close is None
        or fast_average is None
        or slow_average is None
        or momentum_20 is None
        or rolling_high is None
        or close <= 0
        or fast_average <= 0
        or slow_average <= 0
        or rolling_high <= 0
    ):
        return None
    trend_strength = (close / slow_average) - 1.0
    fast_trend = (close / fast_average) - 1.0
    pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
    volatility = _normalized_volatility(context, symbol_key, close)
    if _has_implausible_daily_feature(momentum_5, momentum_20, momentum_60, trend_strength, fast_trend):
        return None
    return {
        "symbol_key": symbol_key,
        "close": close,
        "fast_average": fast_average,
        "slow_average": slow_average,
        "momentum_5": momentum_5 or 0.0,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60 if momentum_60 is not None else momentum_20,
        "trend_strength": trend_strength,
        "fast_trend": fast_trend,
        "rolling_high": rolling_high,
        "pullback_from_high": pullback_from_high,
        "liquidity": liquidity,
        "volatility": volatility,
    }


def _is_buyable(item: dict[str, float | str]) -> bool:
    if float(item["liquidity"]) < MIN_LIQUIDITY:
        return False
    if float(item["momentum_20"]) < MIN_MOMENTUM_20:
        return False
    if float(item["trend_strength"]) <= 0:
        return False
    if float(item["fast_trend"]) < -0.03:
        return False
    if float(item["pullback_from_high"]) > MAX_HEALTHY_PULLBACK:
        return False
    volatility = float(item["volatility"])
    if volatility >= EXTREME_NORMALIZED_VOLATILITY:
        return False
    if volatility > MAX_NORMALIZED_VOLATILITY and float(item["momentum_20"]) < 0.30:
        return False
    return True


def _score(item: dict[str, float | str]) -> float:
    pullback = float(item["pullback_from_high"])
    entry_timing = max(0.0, 1.0 - abs(pullback - 0.055) / 0.11)
    liquidity_bonus = min(float(item["liquidity"]) / 5_000_000_000_000.0, 0.04)
    return (
        float(item["momentum_20"]) * 0.52
        + float(item["momentum_5"]) * 0.22
        + float(item["momentum_60"]) * 0.16
        + max(float(item["trend_strength"]), 0.0) * 0.20
        + max(float(item["fast_trend"]), 0.0) * 0.08
        + entry_timing * 0.04
        + liquidity_bonus
        - min(float(item["volatility"]), 0.40) * 0.32
    )


def _metadata(item: dict[str, float | str], *, rank: int, selected_count: int) -> dict[str, float | str | int]:
    return {
        "role": "krw_semiconductor_momentum",
        "close": float(item["close"]),
        "fast_average": float(item["fast_average"]),
        "slow_average": float(item["slow_average"]),
        "momentum_5": float(item["momentum_5"]),
        "momentum": float(item["momentum_20"]),
        "momentum_60": float(item["momentum_60"]),
        "trend_strength": float(item["trend_strength"]),
        "fast_trend": float(item["fast_trend"]),
        "pullback_from_high": float(item["pullback_from_high"]),
        "rolling_high": float(item["rolling_high"]),
        "liquidity": float(item["liquidity"]),
        "volatility": float(item["volatility"]),
        "rank": rank,
        "selected_count": selected_count,
    }


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


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _has_implausible_daily_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_DAILY_FEATURE_ABS for value in values)

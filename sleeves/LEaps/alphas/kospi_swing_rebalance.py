from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-kospi-swing-rebalance"
VERSION = "0.2.0"
EVALUATION_CADENCE = "every_15_minutes"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=3)

MAX_SELECTED_BUYS = 6
MAX_SELECTED_TRIMS = 8
MIN_MOMENTUM_5 = 0.0
MIN_MOMENTUM_20 = 0.04
MAX_BUY_PULLBACK = 0.085
MIN_BUY_PULLBACK = 0.018
MAX_NEAR_HIGH_DISTANCE = 0.11
MIN_LIQUIDITY = 2_000_000_000.0
VOLATILE_BUY_THRESHOLD = 0.12
MAX_BUY_VOLATILITY = 0.18
VOLATILE_BUY_MIN_MOMENTUM_5 = 0.025
VOLATILE_BUY_MAX_PULLBACK = 0.055
VOLATILITY_SHOCK_THRESHOLD = 0.12
VOLATILITY_SHOCK_PULLBACK = 0.09
VOLATILITY_SHOCK_MOMENTUM_5 = -0.015
TEN_DAY_BREAK_BUFFER = 0.995
TWENTY_DAY_EXIT_BUFFER = 0.995
TAKE_PROFIT_MOMENTUM_5 = 0.065
TAKE_PROFIT_EXTENSION_TO_SMA10 = 0.035
TAKE_PROFIT_NEAR_HIGH = 0.012
MAX_PLAUSIBLE_DAILY_FEATURE_ABS = 3.0


def generate(context: SnapshotContext) -> list[Insight]:
    allow_buys = context.allows_new_entries
    buy_candidates: list[dict[str, float | str]] = []
    trim_candidates: list[dict[str, float | str]] = []
    exit_candidates: list[dict[str, float | str]] = []
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("KRX:"):
            continue
        item = _features(context, symbol_key)
        if item is None:
            continue

        close = float(item["close"])
        sma10 = float(item["sma10"])
        sma20 = float(item["sma20"])
        momentum_5 = float(item["momentum_5"])
        momentum_20 = float(item["momentum_20"])
        pullback = float(item["pullback_from_high"])
        extension_to_sma10 = float(item["extension_to_sma10"])
        liquidity = float(item["liquidity"])

        if close < sma20 * TWENTY_DAY_EXIT_BUFFER:
            exit_candidates.append({**item, "score": _exit_score(close, sma20)})
            continue

        if close < sma10 * TEN_DAY_BREAK_BUFFER and momentum_20 > 0:
            trim_candidates.append(
                {
                    **item,
                    "score": _trim_score(item, reason="ten_day_break"),
                    "trim_multiplier": 0.50,
                    "trim_reason": "ten_day_break",
                }
            )
            continue

        if _is_volatility_shock_trim(item):
            trim_candidates.append(
                {
                    **item,
                    "score": _trim_score(item, reason="volatility_shock"),
                    "trim_multiplier": 0.55,
                    "trim_reason": "volatility_shock",
                }
            )
            continue

        if (
            pullback <= TAKE_PROFIT_NEAR_HIGH
            and momentum_5 >= TAKE_PROFIT_MOMENTUM_5
            and extension_to_sma10 >= TAKE_PROFIT_EXTENSION_TO_SMA10
        ):
            trim_candidates.append(
                {
                    **item,
                    "score": _trim_score(item, reason="overextended"),
                    "trim_multiplier": 0.65,
                    "trim_reason": "overextended",
                }
            )
            continue

        if not allow_buys or not _is_buyable_swing(item):
            continue
        buy_candidates.append({**item, "score": _buy_score(item)})

    insights: list[Insight] = []
    for rank, item in enumerate(
        sorted(exit_candidates, key=lambda row: (float(row["score"]), str(row["symbol_key"])), reverse=True)[
            :MAX_SELECTED_TRIMS
        ],
        start=1,
    ):
        insights.append(_exit_insight(context, item, rank=rank, selected_count=min(len(exit_candidates), MAX_SELECTED_TRIMS)))

    for rank, item in enumerate(
        sorted(trim_candidates, key=lambda row: (float(row["score"]), str(row["symbol_key"])), reverse=True)[
            :MAX_SELECTED_TRIMS
        ],
        start=1,
    ):
        insights.append(_trim_insight(context, item, rank=rank, selected_count=min(len(trim_candidates), MAX_SELECTED_TRIMS)))

    for rank, item in enumerate(
        sorted(buy_candidates, key=lambda row: (float(row["score"]), str(row["symbol_key"])), reverse=True)[
            :MAX_SELECTED_BUYS
        ],
        start=1,
    ):
        insights.append(_buy_insight(context, item, rank=rank, selected_count=min(len(buy_candidates), MAX_SELECTED_BUYS)))

    return insights


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float | str] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    sma10 = _first_value(context, symbol_key, ("sma_10_close", "ema_8_close", "sma_5_close"))
    sma20 = _first_value(context, symbol_key, ("sma_20_close",))
    momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
    liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "volume")) or 0.0
    if (
        close is None
        or sma10 is None
        or sma20 is None
        or momentum_5 is None
        or momentum_20 is None
        or rolling_high is None
        or close <= 0
        or sma10 <= 0
        or sma20 <= 0
        or rolling_high <= 0
    ):
        return None
    trend_strength = (close / sma20) - 1.0
    pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
    extension_to_sma10 = (close / sma10) - 1.0
    volatility = _normalized_volatility(context, symbol_key, close)
    if _has_implausible_daily_feature(momentum_5, momentum_20, trend_strength, extension_to_sma10):
        return None
    return {
        "symbol_key": symbol_key,
        "close": close,
        "sma10": sma10,
        "sma20": sma20,
        "momentum_5": momentum_5,
        "momentum_20": momentum_20,
        "rolling_high": rolling_high,
        "pullback_from_high": pullback_from_high,
        "extension_to_sma10": extension_to_sma10,
        "trend_strength": trend_strength,
        "volatility": volatility,
        "liquidity": liquidity,
    }


def _is_buyable_swing(item: dict[str, float | str]) -> bool:
    if float(item["liquidity"]) < MIN_LIQUIDITY:
        return False
    if float(item["trend_strength"]) <= 0:
        return False
    if float(item["momentum_5"]) <= MIN_MOMENTUM_5 or float(item["momentum_20"]) <= MIN_MOMENTUM_20:
        return False
    pullback = float(item["pullback_from_high"])
    volatility = float(item["volatility"])
    if volatility >= MAX_BUY_VOLATILITY:
        return False
    if volatility >= VOLATILE_BUY_THRESHOLD and (
        float(item["momentum_5"]) < VOLATILE_BUY_MIN_MOMENTUM_5
        or pullback > VOLATILE_BUY_MAX_PULLBACK
        or float(item["extension_to_sma10"]) < -0.005
    ):
        return False
    return MIN_BUY_PULLBACK <= pullback <= MAX_BUY_PULLBACK and pullback <= MAX_NEAR_HIGH_DISTANCE


def _is_volatility_shock_trim(item: dict[str, float | str]) -> bool:
    if float(item["volatility"]) < VOLATILITY_SHOCK_THRESHOLD:
        return False
    if float(item["pullback_from_high"]) >= VOLATILITY_SHOCK_PULLBACK:
        return True
    return float(item["momentum_5"]) <= VOLATILITY_SHOCK_MOMENTUM_5 and float(item["extension_to_sma10"]) < 0.0


def _buy_score(item: dict[str, float | str]) -> float:
    pullback = float(item["pullback_from_high"])
    ideal_pullback = 0.045
    pullback_score = max(0.0, 1.0 - abs(pullback - ideal_pullback) / ideal_pullback)
    liquidity_bonus = min(float(item["liquidity"]) / 4_000_000_000_000.0, 0.04)
    volatility_penalty = float(item["volatility"]) * 0.35
    if float(item["volatility"]) >= VOLATILE_BUY_THRESHOLD:
        volatility_penalty += min((float(item["volatility"]) - VOLATILE_BUY_THRESHOLD) * 0.45, 0.04)
    return (
        0.05
        + pullback_score * 0.18
        + float(item["momentum_5"]) * 0.30
        + float(item["momentum_20"]) * 0.22
        + float(item["trend_strength"]) * 0.18
        + liquidity_bonus
        - volatility_penalty
    )


def _trim_score(item: dict[str, float | str], *, reason: str) -> float:
    base = 0.18 if reason == "ten_day_break" else 0.16 if reason == "volatility_shock" else 0.12
    return (
        base
        + max(float(item["momentum_5"]), 0.0) * 0.25
        + max(float(item["extension_to_sma10"]), 0.0) * 0.30
        + max(float(item["pullback_from_high"]), 0.0) * 0.10
        + max(float(item["volatility"]) - VOLATILITY_SHOCK_THRESHOLD, 0.0) * 0.25
    )


def _exit_score(close: float, sma20: float) -> float:
    return max((sma20 - close) / sma20, 0.0)


def _buy_insight(context: SnapshotContext, item: dict[str, float | str], *, rank: int, selected_count: int) -> Insight:
    score = float(item["score"])
    symbol_key = str(item["symbol_key"])
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(symbol_key),
        direction=InsightDirection.UP,
        generated_at=context.as_of,
        expires_at=context.as_of + HORIZON,
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=float(item["pullback_from_high"]),
        confidence=min(0.93, 0.58 + score * 1.4),
        weight=min(0.26, max(0.06, score)),
        score=score,
        group_id="krw-growth",
        reason="kospi_swing_buy_pullback_in_uptrend",
        metadata=_with_temporal_features(
            context,
            symbol_key,
            _metadata(item, rank=rank, selected_count=selected_count, action="buy_pullback"),
        ),
    )


def _trim_insight(context: SnapshotContext, item: dict[str, float | str], *, rank: int, selected_count: int) -> Insight:
    score = float(item["score"])
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(str(item["symbol_key"])),
        direction=InsightDirection.FLAT,
        generated_at=context.as_of,
        expires_at=context.as_of + timedelta(days=1),
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=-score,
        confidence=min(0.92, 0.62 + score),
        weight=0.0,
        score=score,
        group_id="krw-growth",
        reason=f"kospi_swing_partial_trim_{item['trim_reason']}",
        metadata=_metadata(
            item,
            rank=rank,
            selected_count=selected_count,
            action="partial_trim",
            target_multiplier=float(item["trim_multiplier"]),
        ),
    )


def _exit_insight(context: SnapshotContext, item: dict[str, float | str], *, rank: int, selected_count: int) -> Insight:
    score = float(item["score"])
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(str(item["symbol_key"])),
        direction=InsightDirection.FLAT,
        generated_at=context.as_of,
        expires_at=context.as_of + timedelta(days=2),
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=-score,
        confidence=min(0.95, 0.70 + score * 3.0),
        weight=0.0,
        score=score,
        group_id="krw-growth",
        reason="kospi_swing_exit_20dma_break",
        metadata=_metadata(item, rank=rank, selected_count=selected_count, action="exit"),
    )


def _metadata(
    item: dict[str, float | str],
    *,
    rank: int,
    selected_count: int,
    action: str,
    target_multiplier: float | None = None,
) -> dict[str, float | str | int]:
    payload: dict[str, float | str | int] = {
        "role": "krw_swing_rebalance",
        "portfolio_action": action,
        "close": float(item["close"]),
        "sma10": float(item["sma10"]),
        "sma20": float(item["sma20"]),
        "momentum_5": float(item["momentum_5"]),
        "momentum": float(item["momentum_20"]),
        "trend_strength": float(item["trend_strength"]),
        "pullback_from_high": float(item["pullback_from_high"]),
        "extension_to_sma10": float(item["extension_to_sma10"]),
        "rolling_high": float(item["rolling_high"]),
        "liquidity": float(item["liquidity"]),
        "volatility": float(item["volatility"]),
        "rank": rank,
        "selected_count": selected_count,
    }
    if target_multiplier is not None:
        payload["target_multiplier"] = target_multiplier
    return payload


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


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _has_implausible_daily_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_DAILY_FEATURE_ABS for value in values)

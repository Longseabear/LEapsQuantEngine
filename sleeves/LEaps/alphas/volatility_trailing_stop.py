from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_ID = "leaps-volatility-trailing-stop"
VERSION = "0.1.1"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=1)
ATR_MULTIPLIER = 2.5
STDDEV_MULTIPLIER = 2.0
FALLBACK_TRAIL_PCT = 0.08
STATE_NAMESPACE = "trailing_stop"


def generate(context: SnapshotContext) -> list[Insight]:
    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        mark = _trailing_mark(context, symbol_key)
        if mark is None:
            continue

        if mark["close"] > mark["stop_price"]:
            continue

        severity = 0.0 if mark["stop_price"] == 0 else max((mark["stop_price"] - mark["close"]) / mark["stop_price"], 0.0)
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
                magnitude=-severity,
                confidence=min(0.95, 0.7 + severity * 2.0),
                weight=0.0,
                score=severity,
                reason="volatility_trailing_stop_triggered",
                metadata=mark,
            )
        )
    return insights


def state_patches(context: SnapshotContext, insights: tuple[Insight, ...] = ()) -> tuple[StatePatch, ...]:
    patches: list[StatePatch] = []
    for symbol_key in context.symbol_keys:
        mark = _trailing_mark(context, symbol_key)
        if mark is None:
            continue
        patches.append(
            StatePatch(
                key=context.model_state.key(
                    model_id=ALPHA_ID,
                    namespace=STATE_NAMESPACE,
                    symbol_key=symbol_key,
                ),
                value={
                    "high_watermark_price": mark["high_watermark_price"],
                    "previous_high_watermark_price": mark["previous_high_watermark_price"],
                    "last_price": mark["close"],
                    "stop_price": mark["stop_price"],
                    "rolling_high": mark["rolling_high"],
                    "atr": mark["atr"],
                    "stddev": mark["stddev"],
                },
                reason="trailing_stop_mark",
            )
        )
    return tuple(patches)


def _trailing_mark(context: SnapshotContext, symbol_key: str) -> dict[str, float | None] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    if close is None or close <= 0:
        return None
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close", "max_20_close"))
    atr = _first_value(context, symbol_key, ("atr_14", "average_true_range_14"))
    stddev = _first_value(context, symbol_key, ("stddev_20_close", "std_20_close"))
    previous_high = _state_float(context, symbol_key, "high_watermark_price")
    if previous_high is not None and previous_high > 0:
        high_watermark = max(previous_high, close)
    else:
        high_watermark = max(
            value
            for value in (close, rolling_high)
            if value is not None and value > 0
        )
    stop_distance = _stop_distance(close, atr=atr, stddev=stddev)
    stop_price = max(0.0, high_watermark - stop_distance)
    return {
        "close": close,
        "rolling_high": rolling_high,
        "previous_high_watermark_price": previous_high,
        "high_watermark_price": high_watermark,
        "stop_price": stop_price,
        "stop_distance": stop_distance,
        "atr": atr,
        "stddev": stddev,
    }


def _stop_distance(close: float, *, atr: float | None, stddev: float | None) -> float:
    distances = []
    if atr is not None:
        distances.append(atr * ATR_MULTIPLIER)
    if stddev is not None:
        distances.append(stddev * STDDEV_MULTIPLIER)
    distances.append(close * FALLBACK_TRAIL_PCT)
    return max(distances)


def _state_float(context: SnapshotContext, symbol_key: str, name: str) -> float | None:
    record = context.model_state.get(
        model_id=ALPHA_ID,
        namespace=STATE_NAMESPACE,
        symbol_key=symbol_key,
    )
    if record is None:
        return None
    value = record.value.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

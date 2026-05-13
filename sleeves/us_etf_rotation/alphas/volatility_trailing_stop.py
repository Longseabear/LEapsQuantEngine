from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "us_etf_rotation-volatility-trailing-stop"
VERSION = "0.1.1"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=1)
ATR_MULTIPLIER = 2.5
STDDEV_MULTIPLIER = 2.0
FALLBACK_TRAIL_PCT = 0.08


def generate(context: SnapshotContext) -> list[Insight]:
    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close", "max_20_close"))
        atr = _first_value(context, symbol_key, ("atr_14", "average_true_range_14"))
        stddev = _first_value(context, symbol_key, ("stddev_20_close", "std_20_close"))
        if close is None or rolling_high is None:
            continue

        stop_distance = _stop_distance(close, atr=atr, stddev=stddev)
        stop_price = rolling_high - stop_distance
        if close > stop_price:
            continue

        severity = 0.0 if stop_price == 0 else max((stop_price - close) / stop_price, 0.0)
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
                metadata={
                    "close": close,
                    "rolling_high": rolling_high,
                    "stop_price": stop_price,
                    "stop_distance": stop_distance,
                    "atr": atr,
                    "stddev": stddev,
                },
            )
        )
    return insights


def _stop_distance(close: float, *, atr: float | None, stddev: float | None) -> float:
    distances = []
    if atr is not None:
        distances.append(atr * ATR_MULTIPLIER)
    if stddev is not None:
        distances.append(stddev * STDDEV_MULTIPLIER)
    distances.append(close * FALLBACK_TRAIL_PCT)
    return max(distances)


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

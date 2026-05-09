from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "momentum-strategy-demo"
VERSION = "0.1.0"
HORIZON = timedelta(days=5)
MIN_MOMENTUM = 0.03


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        moving_average = _first_value(context, symbol_key, ("sma_5_close", "sma_20_close", "sma_3_close"))
        momentum = _first_value(context, symbol_key, ("momentum_5_close", "roc_20_close", "momentum_2_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_2", "dollar_volume_1"))
        if close is None or moving_average is None or momentum is None:
            continue
        if close <= moving_average or momentum < MIN_MOMENTUM:
            continue

        confidence = min(0.9, 0.55 + abs(momentum) * 2.0)
        weight = min(0.2, max(0.03, momentum))
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
                score=momentum,
                reason="price_above_average_with_positive_momentum",
                metadata={
                    "close": close,
                    "moving_average": moving_average,
                    "momentum": momentum,
                    "liquidity": liquidity,
                },
            )
        )
    return insights


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


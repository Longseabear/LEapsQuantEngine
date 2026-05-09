from __future__ import annotations

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "price-above-sma-demo"
VERSION = "0.1.0"


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        close = context.value(symbol_key, "close")
        sma = context.value(symbol_key, "sma_3_close")
        momentum = context.value(symbol_key, "momentum_2_close")
        if close is None or sma is None or momentum is None:
            continue
        if close > sma and momentum > 0:
            insights.append(
                Insight(
                    sleeve_id=context.sleeve_id,
                    symbol=context.symbol(symbol_key),
                    direction=InsightDirection.UP,
                    generated_at=context.as_of,
                    source_snapshot_id=context.source_snapshot_id,
                    alpha_id=ALPHA_ID,
                    alpha_version=VERSION,
                    confidence=0.65,
                    score=momentum,
                    reason="close_above_sma_3_and_positive_momentum",
                    metadata={"close": close, "sma": sma, "momentum": momentum},
                )
            )
    return insights

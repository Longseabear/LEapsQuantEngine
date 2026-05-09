from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "live-quote-smoke"
VERSION = "0.1.0"


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        close = context.value(symbol_key, "close", ready_only=False)
        if close is None or close <= 0:
            continue
        vwap = context.value(symbol_key, "vwap_1", ready_only=False)
        reference = vwap if vwap is not None and vwap > 0 else close
        if close < reference:
            continue
        volume = context.value(symbol_key, "volume", ready_only=False)
        insights.append(
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + timedelta(minutes=1),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=ALPHA_ID,
                alpha_version=VERSION,
                confidence=0.55,
                weight=0.05,
                score=close / reference - 1.0 if reference else None,
                reason="live_quote_close_at_or_above_vwap",
                metadata={
                    "close": close,
                    "vwap_1": vwap,
                    "volume": volume,
                },
            )
        )
    return insights

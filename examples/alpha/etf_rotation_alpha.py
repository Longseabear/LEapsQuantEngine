from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "etf-rotation-demo"
VERSION = "0.1.0"
HORIZON = timedelta(days=20)
MAX_SELECTED = 3
MIN_MOMENTUM = 0.0
EMIT_FLAT_FOR_UNSELECTED = True


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, float | str]] = []
    rejected_symbols: list[str] = []
    for symbol_key in context.symbol_keys:
        momentum = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close", "momentum_5_close"))
        volatility = _first_value(context, symbol_key, ("stddev_20_close", "volatility_20_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_1"))
        if momentum is None:
            rejected_symbols.append(symbol_key)
            continue
        if momentum < MIN_MOMENTUM:
            rejected_symbols.append(symbol_key)
            continue
        volatility_penalty = 0.0 if volatility is None else volatility * 0.1
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.05)
        candidates.append(
            {
                "symbol_key": symbol_key,
                "momentum": momentum,
                "volatility": volatility or 0.0,
                "liquidity": liquidity or 0.0,
                "score": momentum - volatility_penalty + liquidity_bonus,
            }
        )

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    selected_keys = {str(item["symbol_key"]) for item in selected}
    selected_weight = 1.0 / len(selected) if selected else 0.0

    insights: list[Insight] = []
    for item in selected:
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
                magnitude=float(item["momentum"]),
                confidence=min(0.9, 0.55 + max(float(item["score"]), 0.0)),
                weight=selected_weight,
                score=float(item["score"]),
                reason="selected_by_etf_rotation_score",
                metadata={
                    "momentum": item["momentum"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "rank_count": len(selected),
                },
            )
        )

    if EMIT_FLAT_FOR_UNSELECTED:
        for symbol_key in context.symbol_keys:
            if symbol_key in selected_keys:
                continue
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
                    confidence=0.6,
                    weight=0.0,
                    score=0.0,
                    reason="not_selected_by_etf_rotation",
                    metadata={"rejected_due_to_missing_or_negative_momentum": symbol_key in rejected_symbols},
                )
            )
    return insights


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


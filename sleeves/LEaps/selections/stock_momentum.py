from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


MAX_NORMALIZED_VOLATILITY = 0.18
EXTREME_NORMALIZED_VOLATILITY = 0.24
HIGH_VOL_MOMENTUM_EXCEPTION = 0.45
TREND_STRENGTH_WEIGHT = 0.08


@dataclass(frozen=True, slots=True)
class StockMomentumSelectionModel:
    max_active_symbols: int = 40
    selection_id: str = "leaps-stock-momentum"

    def select(self, context: UniverseSelectionContext):
        if context.indicator_snapshot is None:
            return build_universe_selection_result(
                context,
                (),
                selection_id=self.selection_id,
                candidates={},
                rejected={symbol.key: ("missing_indicator_snapshot",) for symbol in context.universe.symbols},
            )

        candidates: dict[str, UniverseSelectionCandidate] = {}
        rejected: dict[str, tuple[str, ...]] = {}
        scored = []
        for symbol in context.universe.symbols:
            if not symbol.key.startswith("KRX:"):
                rejected[symbol.key] = ("not_krx_stock_candidate",)
                continue
            if _is_etf(context, symbol.key):
                rejected[symbol.key] = ("not_stock_candidate",)
                continue
            close = _first_value(context, symbol.key, ("identity_close", "close"))
            momentum = _first_value(context, symbol.key, ("momentum_20_close", "roc_20_close", "momentum_5_close", "momentum_2_close"))
            momentum_5 = _first_value(context, symbol.key, ("momentum_5_close", "momentum_2_close"))
            momentum_60 = _first_value(context, symbol.key, ("roc_60_close", "momentum_60_close"))
            slow_average = _first_value(context, symbol.key, ("sma_20_close",))
            liquidity = _first_value(context, symbol.key, ("rolling_dollar_volume_20", "dollar_volume_2", "dollar_volume_1"))
            volatility = _normalized_volatility(context, symbol.key, close)
            if close is None:
                rejected[symbol.key] = ("missing_price",)
                continue
            if _volatility_blocks_candidate(volatility=volatility, momentum=momentum or 0.0):
                rejected[symbol.key] = ("volatility_filter",)
                continue
            momentum_score = momentum if momentum is not None else 0.0
            recent_momentum = momentum_5 if momentum_5 is not None else 0.0
            intermediate_momentum = momentum_60 if momentum_60 is not None else momentum_score
            recency_weighted_momentum = (
                (momentum_score * 0.50)
                + (recent_momentum * 0.30)
                + (intermediate_momentum * 0.20)
            )
            trend_strength = 0.0 if slow_average is None or slow_average <= 0 else (close / slow_average) - 1.0
            liquidity_score = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 1.0)
            scored.append(
                {
                    "symbol": symbol,
                    "close": close,
                    "momentum": momentum_score,
                    "momentum_5": recent_momentum,
                    "momentum_60": intermediate_momentum,
                    "recency_weighted_momentum": recency_weighted_momentum,
                    "trend_strength": trend_strength,
                    "liquidity": liquidity,
                    "liquidity_score": liquidity_score,
                    "volatility": volatility,
                }
            )

        for item in scored:
            item["score"] = (
                float(item["recency_weighted_momentum"])
                + (max(float(item["trend_strength"]), 0.0) * TREND_STRENGTH_WEIGHT)
                + (float(item["liquidity_score"]) * 0.05)
                - min(float(item["volatility"]), 0.35) * 0.20
            )

        selected = tuple(
            item["symbol"]
            for item in sorted(scored, key=lambda item: (float(item["score"]), item["symbol"].key), reverse=True)[: self.max_active_symbols]
        )
        selected_keys = {symbol.key for symbol in selected}
        for item in scored:
            symbol = item["symbol"]
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=float(item["score"]),
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("stock_momentum_candidate",),
                metadata={
                    "close": item["close"],
                    "momentum": item["momentum"],
                    "momentum_5": item["momentum_5"],
                    "momentum_60": item["momentum_60"],
                    "recency_weighted_momentum": item["recency_weighted_momentum"],
                    "trend_strength": item["trend_strength"],
                    "liquidity": item["liquidity"],
                    "volatility": item["volatility"],
                    "volatility_filter": "passed",
                },
            )
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )


def _first_value(context: UniverseSelectionContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    snapshot = context.indicator_snapshot
    if snapshot is None:
        return None
    for name in names:
        value = snapshot.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _normalized_volatility(context: UniverseSelectionContext, symbol_key: str, close: float | None) -> float:
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


def _volatility_blocks_candidate(*, volatility: float, momentum: float) -> bool:
    if volatility >= EXTREME_NORMALIZED_VOLATILITY:
        return True
    if volatility <= MAX_NORMALIZED_VOLATILITY:
        return False
    return momentum < HIGH_VOL_MOMENTUM_EXCEPTION


def _is_etf(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    asset_type = str(properties.get("asset_type") or properties.get("type") or "").strip().lower()
    if asset_type == "etf":
        return True
    value = properties.get("is_etf")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}

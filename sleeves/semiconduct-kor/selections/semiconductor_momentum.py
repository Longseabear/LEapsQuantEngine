from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


SEMICONDUCTOR_TOKENS = frozenset(
    {
        "semiconductor",
        "semiconductors",
        "semi",
        "chip",
        "memory",
        "hbm",
        "foundry",
        "fabless",
        "osat",
        "packaging",
        "substrate",
        "equipment",
        "materials",
        "test",
        "probe",
        "socket",
        "inspection",
    }
)
MAX_NORMALIZED_VOLATILITY = 0.20
EXTREME_NORMALIZED_VOLATILITY = 0.30
HIGH_VOL_MOMENTUM_EXCEPTION = 0.35


@dataclass(frozen=True, slots=True)
class SemiconductorMomentumSelectionModel:
    max_active_symbols: int = 12
    selection_id: str = "semiconduct-kor-momentum"

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
        scored: list[dict[str, Any]] = []
        for symbol in context.universe.symbols:
            if not symbol.key.startswith("KRX:"):
                rejected[symbol.key] = ("not_krx",)
                continue
            if _is_etf(context, symbol.key):
                rejected[symbol.key] = ("not_stock_candidate",)
                continue
            if not _has_semiconductor_profile(context, symbol.key):
                rejected[symbol.key] = ("not_semiconductor_profile",)
                continue

            close = _first_value(context, symbol.key, ("identity_close", "close"))
            momentum = _first_value(context, symbol.key, ("roc_20_close", "momentum_20_close", "momentum_5_close"))
            momentum_5 = _first_value(context, symbol.key, ("momentum_5_close",))
            momentum_60 = _first_value(context, symbol.key, ("roc_60_close", "momentum_60_close"))
            fast_average = _first_value(context, symbol.key, ("ema_8_close", "sma_10_close"))
            slow_average = _first_value(context, symbol.key, ("sma_20_close",))
            liquidity = _first_value(context, symbol.key, ("rolling_dollar_volume_20", "volume"))
            if close is None or close <= 0:
                rejected[symbol.key] = ("missing_price",)
                continue
            if momentum is None:
                rejected[symbol.key] = ("missing_momentum",)
                continue

            trend_strength = 0.0 if slow_average is None or slow_average <= 0 else (close / slow_average) - 1.0
            fast_trend = 0.0 if fast_average is None or fast_average <= 0 else (close / fast_average) - 1.0
            volatility = _normalized_volatility(context, symbol.key, close)
            if _volatility_blocks_candidate(volatility=volatility, momentum=momentum):
                rejected[symbol.key] = ("volatility_filter",)
                continue
            if trend_strength < -0.08 and momentum < 0:
                rejected[symbol.key] = ("weak_trend",)
                continue

            recency_weighted_momentum = (
                (momentum * 0.55)
                + ((momentum_5 or 0.0) * 0.25)
                + ((momentum_60 if momentum_60 is not None else momentum) * 0.20)
            )
            liquidity_score = 0.0 if liquidity is None else min(float(liquidity) / 3_000_000_000.0, 1.0)
            profile_bonus = _profile_bonus(context, symbol.key)
            score = (
                recency_weighted_momentum
                + max(trend_strength, 0.0) * 0.18
                + max(fast_trend, 0.0) * 0.08
                + liquidity_score * 0.04
                + profile_bonus
                - min(volatility, 0.40) * 0.22
            )
            scored.append(
                {
                    "symbol": symbol,
                    "score": score,
                    "close": close,
                    "momentum": momentum,
                    "momentum_5": momentum_5 or 0.0,
                    "momentum_60": momentum_60 if momentum_60 is not None else momentum,
                    "recency_weighted_momentum": recency_weighted_momentum,
                    "trend_strength": trend_strength,
                    "fast_trend": fast_trend,
                    "liquidity": liquidity or 0.0,
                    "volatility": volatility,
                    "profile_bonus": profile_bonus,
                }
            )

        selected = tuple(
            item["symbol"]
            for item in sorted(scored, key=lambda item: (float(item["score"]), item["symbol"].key), reverse=True)[
                : self.max_active_symbols
            ]
        )
        selected_keys = {symbol.key for symbol in selected}
        for item in scored:
            symbol = item["symbol"]
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=float(item["score"]),
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("semiconductor_momentum_candidate",),
                metadata={
                    "close": item["close"],
                    "momentum": item["momentum"],
                    "momentum_5": item["momentum_5"],
                    "momentum_60": item["momentum_60"],
                    "recency_weighted_momentum": item["recency_weighted_momentum"],
                    "trend_strength": item["trend_strength"],
                    "fast_trend": item["fast_trend"],
                    "liquidity": item["liquidity"],
                    "volatility": item["volatility"],
                    "profile_bonus": item["profile_bonus"],
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
            return float(value)
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


def _has_semiconductor_profile(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    for key in ("sector", "industry", "segment", "theme", "sub_industry", "business_line", "tags"):
        if _contains_token(properties.get(key)):
            return True
    return False


def _profile_bonus(context: UniverseSelectionContext, symbol_key: str) -> float:
    properties = context.universe.properties_for(symbol_key)
    tokens = _tokens_for(properties.get("theme")) | _tokens_for(properties.get("industry"))
    bonus = 0.0
    if tokens & {"hbm", "memory"}:
        bonus += 0.025
    if tokens & {"equipment", "packaging", "osat", "test", "probe", "socket"}:
        bonus += 0.015
    if tokens & {"materials", "substrate"}:
        bonus += 0.010
    return bonus


def _contains_token(value: object) -> bool:
    return bool(_tokens_for(value) & SEMICONDUCTOR_TOKENS)


def _tokens_for(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        tokens: set[str] = set()
        for item in value:
            tokens.update(_tokens_for(item))
        return tokens
    return {
        token
        for token in str(value).replace("-", "_").replace("/", "_").split("_")
        if token
    } | {str(value).strip().lower()}

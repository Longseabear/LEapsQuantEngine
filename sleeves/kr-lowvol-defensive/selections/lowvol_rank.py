from dataclasses import dataclass
from typing import Any

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


SELECTION_ID = "kr-lowvol-defensive-core"
MIN_PRICE = 2_000.0
MIN_LIQUIDITY = 600_000_000.0
MAX_NORMALIZED_VOLATILITY = 0.13
HARD_MAX_NORMALIZED_VOLATILITY = 0.170
MAX_DRAWDOWN_60 = 0.26
MIN_MOMENTUM_60 = -0.12
HARD_VOLUME_RATIO = 4.20
HARD_INTRADAY_RANGE = 0.115
HARD_UPSIDE_SPIKE = 0.090


@dataclass(frozen=True, slots=True)
class LowVolDefensiveSelectionModel:
    max_active_symbols: int = 40
    selection_id: str = SELECTION_ID

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
            if _is_preferred(context, symbol.key):
                rejected[symbol.key] = ("preferred_share",)
                continue

            item = _features(context, symbol.key)
            if item is None:
                rejected[symbol.key] = ("missing_required_features",)
                continue
            reason = _reject_reason(item)
            if reason:
                rejected[symbol.key] = (reason,)
                continue
            score = _score(item)
            scored.append({**item, "symbol": symbol, "score": score})

        selected_items = sorted(scored, key=lambda item: (float(item["score"]), item["symbol"].key), reverse=True)[
            : max(0, self.max_active_symbols)
        ]
        selected_keys = {item["symbol"].key for item in selected_items}
        for item in scored:
            symbol = item["symbol"]
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=float(item["score"]),
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("anti_lottery_defensive_candidate",),
                metadata=_candidate_metadata(item),
            )
        return build_universe_selection_result(
            context,
            tuple(item["symbol"] for item in selected_items),
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )


def _features(context: UniverseSelectionContext, symbol_key: str) -> dict[str, float] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_60", "rolling_dollar_volume_20", "volume"))
    volatility_20 = _normalized(context, symbol_key, close, ("stddev_20_close", "atr_14"))
    volatility_60 = _normalized(context, symbol_key, close, ("stddev_60_close", "stddev_20_close"))
    volatility_120 = _normalized(context, symbol_key, close, ("stddev_120_close", "stddev_60_close"))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close",))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "roc_20_close"))
    trend = _trend(context, symbol_key, close)
    drawdown_20 = _first_value(context, symbol_key, ("drawdown_20_close",)) or 0.0
    drawdown_60 = _first_value(context, symbol_key, ("drawdown_60_close", "drawdown_20_close")) or 0.0
    gap = abs(_first_value(context, symbol_key, ("gap_percent",)) or 0.0)
    bar_return = _first_value(context, symbol_key, ("bar_return_close",)) or 0.0
    high_low_range = _first_value(context, symbol_key, ("high_low_range_percent",)) or 0.0
    rolling_range = _normalized(context, symbol_key, close, ("rolling_range_20_close",)) or 0.0
    volume_ratio = _first_value(context, symbol_key, ("volume_ratio_20",)) or 1.0
    volume_momentum = _first_value(context, symbol_key, ("volume_momentum_20",)) or 0.0
    zscore = abs(_first_value(context, symbol_key, ("zscore_20_close",)) or 0.0)
    if close is None or close <= 0 or liquidity is None or volatility_20 is None or momentum_20 is None:
        return None

    base = {
        "close": close,
        "liquidity": liquidity,
        "volatility_20": volatility_20,
        "volatility_60": volatility_60 if volatility_60 is not None else volatility_20,
        "volatility_120": volatility_120 if volatility_120 is not None else volatility_20,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60 if momentum_60 is not None else momentum_20,
        "trend": trend,
        "drawdown_20": drawdown_20,
        "drawdown_60": drawdown_60,
        "gap": gap,
        "bar_return": bar_return,
        "high_low_range": high_low_range,
        "rolling_range_20": rolling_range,
        "volume_ratio_20": volume_ratio,
        "volume_momentum_20": volume_momentum,
        "zscore_20": zscore,
        "quality_score": _quality_score(
            _property_float(context, symbol_key, ("roe", "return_on_equity")),
            _property_float(context, symbol_key, ("debt_ratio", "debt_to_equity")),
        ),
        "value_score": _value_score(
            _property_float(context, symbol_key, ("per", "pe", "trailing_pe")),
            _property_float(context, symbol_key, ("pbr", "pb", "price_to_book")),
        ),
        "dividend_score": _dividend_score(
            _property_float(context, symbol_key, ("dividend_yield", "trailing_dividend_yield"))
        ),
    }
    base["normalized_volatility"] = _combined_volatility(base)
    base["low_vol_score"] = _low_vol_score(base)
    base["stable_trend_score"] = _stable_trend_score(base)
    base["lottery_penalty"] = _lottery_penalty(base)
    base["crowding_penalty"] = _crowding_penalty(base)
    base["turnover_shock_penalty"] = _turnover_shock_penalty(base)
    return base


def _reject_reason(item: dict[str, float]) -> str:
    if item["close"] < MIN_PRICE:
        return "low_price"
    if item["liquidity"] < MIN_LIQUIDITY:
        return "low_liquidity"
    volatility = item["normalized_volatility"]
    if volatility >= HARD_MAX_NORMALIZED_VOLATILITY:
        return "extreme_volatility"
    if volatility > MAX_NORMALIZED_VOLATILITY and item["momentum_60"] < 0.08:
        return "high_vol_without_momentum"
    if item["momentum_60"] < MIN_MOMENTUM_60:
        return "weak_medium_momentum"
    if abs(item["drawdown_60"]) > MAX_DRAWDOWN_60 and item["momentum_20"] < -0.02:
        return "falling_knife"
    if item["gap"] > 0.09:
        return "large_gap"
    if item["high_low_range"] > HARD_INTRADAY_RANGE:
        return "unstable_intraday_range"
    if item["volume_ratio_20"] >= HARD_VOLUME_RATIO:
        return "crowded_turnover_spike"
    if item["bar_return"] > HARD_UPSIDE_SPIKE and item["volume_ratio_20"] > 2.0:
        return "lottery_like_spike"
    if item["lottery_penalty"] >= 0.82:
        return "lottery_like_spike"
    if item["crowding_penalty"] >= 0.88:
        return "crowded_turnover_spike"
    return ""


def _score(item: dict[str, float]) -> float:
    liquidity_score = min(item["liquidity"] / 8_000_000_000.0, 1.0)
    drawdown_penalty = min(abs(min(item["drawdown_60"], 0.0)) / 0.35, 1.0)
    return (
        item["low_vol_score"] * 0.34
        + item["stable_trend_score"] * 0.21
        + item["quality_score"] * 0.11
        + item["value_score"] * 0.07
        + item["dividend_score"] * 0.04
        + liquidity_score * 0.07
        - item["lottery_penalty"] * 0.22
        - item["crowding_penalty"] * 0.18
        - item["turnover_shock_penalty"] * 0.10
        - drawdown_penalty * 0.05
    )


def _candidate_metadata(item: dict[str, Any]) -> dict[str, float | str]:
    return {
        "close": float(item["close"]),
        "liquidity": float(item["liquidity"]),
        "normalized_volatility": float(item["normalized_volatility"]),
        "volatility_20": float(item["volatility_20"]),
        "volatility_60": float(item["volatility_60"]),
        "volatility_120": float(item["volatility_120"]),
        "momentum_20": float(item["momentum_20"]),
        "momentum_60": float(item["momentum_60"]),
        "trend": float(item["trend"]),
        "drawdown_20": float(item["drawdown_20"]),
        "drawdown_60": float(item["drawdown_60"]),
        "gap": float(item["gap"]),
        "bar_return": float(item["bar_return"]),
        "high_low_range": float(item["high_low_range"]),
        "rolling_range_20": float(item["rolling_range_20"]),
        "volume_ratio_20": float(item["volume_ratio_20"]),
        "volume_momentum_20": float(item["volume_momentum_20"]),
        "zscore_20": float(item["zscore_20"]),
        "low_vol_score": float(item["low_vol_score"]),
        "stable_trend_score": float(item["stable_trend_score"]),
        "quality_score": float(item["quality_score"]),
        "value_score": float(item["value_score"]),
        "dividend_score": float(item["dividend_score"]),
        "lottery_penalty": float(item["lottery_penalty"]),
        "crowding_penalty": float(item["crowding_penalty"]),
        "turnover_shock_penalty": float(item["turnover_shock_penalty"]),
        "style": "kr_lowvol_defensive_v2",
    }


def _combined_volatility(item: dict[str, float]) -> float:
    return max(item["volatility_20"], item["volatility_60"] * 0.85, item["volatility_120"] * 0.70)


def _low_vol_score(item: dict[str, float]) -> float:
    return _clamp(1.0 - item["normalized_volatility"] / MAX_NORMALIZED_VOLATILITY)


def _stable_trend_score(item: dict[str, float]) -> float:
    medium_momentum = _clamp(item["momentum_60"], -0.08, 0.16)
    short_momentum = _clamp(item["momentum_20"], -0.08, 0.10)
    smoothness_penalty = min(
        abs(min(item["drawdown_60"], 0.0)) * 0.80
        + item["gap"] * 1.60
        + item["high_low_range"] * 0.90
        + item["rolling_range_20"] * 0.45,
        0.60,
    )
    return _clamp(0.48 + medium_momentum * 1.85 + short_momentum * 0.80 + item["trend"] * 1.15 - smoothness_penalty)


def _lottery_penalty(item: dict[str, float]) -> float:
    upside_spike = max(item["bar_return"], 0.0)
    absolute_spike = abs(item["bar_return"])
    penalty = (
        _clamp((absolute_spike - 0.035) / 0.085) * 0.25
        + _clamp((upside_spike - 0.055) / 0.070) * 0.25
        + _clamp((item["gap"] - 0.030) / 0.070) * 0.16
        + _clamp((item["high_low_range"] - 0.040) / 0.100) * 0.17
        + _clamp((item["zscore_20"] - 1.60) / 2.00) * 0.10
        + _clamp((item["normalized_volatility"] - 0.080) / 0.080) * 0.12
    )
    if item["volume_ratio_20"] > 2.2 and upside_spike > 0.06:
        penalty += 0.18
    return _clamp(penalty)


def _crowding_penalty(item: dict[str, float]) -> float:
    turnover = _turnover_shock_penalty(item)
    return _clamp(
        turnover * 0.62
        + _clamp((abs(item["bar_return"]) - 0.045) / 0.075) * 0.18
        + _clamp((item["volume_momentum_20"] - 0.35) / 1.25) * 0.12
        + (0.12 if item["volume_ratio_20"] > 2.3 and item["bar_return"] > 0.035 else 0.0)
    )


def _turnover_shock_penalty(item: dict[str, float]) -> float:
    return _clamp((item["volume_ratio_20"] - 1.35) / 3.15)


def _quality_score(roe: float | None, debt_ratio: float | None) -> float:
    parts: list[float] = []
    if roe is not None:
        roe_pct = _as_percent(roe)
        parts.append(_clamp((roe_pct - 3.0) / 14.0))
    if debt_ratio is not None:
        debt_pct = _as_percent(debt_ratio) if abs(debt_ratio) <= 5.0 else debt_ratio
        parts.append(_clamp((220.0 - debt_pct) / 170.0))
    return sum(parts) / len(parts) if parts else 0.50


def _value_score(per: float | None, pbr: float | None) -> float:
    parts: list[float] = []
    if per is not None and per > 0:
        parts.append(_clamp((24.0 - per) / 18.0))
    if pbr is not None and pbr > 0:
        parts.append(_clamp((2.40 - pbr) / 2.00))
    return sum(parts) / len(parts) if parts else 0.50


def _dividend_score(dividend_yield: float | None) -> float:
    if dividend_yield is None:
        return 0.35
    return _clamp(_as_percent(dividend_yield) / 4.0)


def _as_percent(value: float) -> float:
    return value * 100.0 if abs(value) <= 1.0 else value


def _trend(context: UniverseSelectionContext, symbol_key: str, close: float | None) -> float:
    if close is None or close <= 0:
        return 0.0
    average = _first_value(context, symbol_key, ("sma_60_close", "sma_20_close"))
    if average is None or average <= 0:
        return 0.0
    return (close / average) - 1.0


def _normalized(
    context: UniverseSelectionContext,
    symbol_key: str,
    close: float | None,
    names: tuple[str, ...],
) -> float | None:
    if close is None or close <= 0:
        return None
    values = [_first_value(context, symbol_key, (name,)) for name in names]
    normalized = [value / close for value in values if value is not None]
    return max(normalized) if normalized else None


def _first_value(context: UniverseSelectionContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    snapshot = context.indicator_snapshot
    if snapshot is None:
        return None
    for name in names:
        value = snapshot.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _property_float(context: UniverseSelectionContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    properties = context.universe.properties_for(symbol_key)
    snapshot = context.indicator_snapshot
    for name in names:
        value = properties.get(name)
        if value is None and snapshot is not None:
            value = snapshot.metadata_value(symbol_key, name)
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(float(value), low), high)


def _is_etf(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    asset_type = str(properties.get("asset_type") or properties.get("type") or "").strip().lower()
    if asset_type == "etf":
        return True
    value = properties.get("is_etf")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}


def _is_preferred(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    value = properties.get("preferred") or properties.get("is_preferred")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}

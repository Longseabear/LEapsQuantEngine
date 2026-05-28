from __future__ import annotations

from datetime import timedelta
from typing import Any

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "kr-lowvol-defensive-alpha"
VERSION = "0.2.3"
EVALUATION_CADENCE = "daily_at 08:50 Asia/Seoul"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=35)

MAX_SELECTED = 15
MIN_SCORE = 0.26
MIN_LIQUIDITY = 600_000_000.0
MAX_NORMALIZED_VOLATILITY = 0.13
HARD_MAX_NORMALIZED_VOLATILITY = 0.170
MIN_PRICE = 2_000.0
HARD_VOLUME_RATIO = 4.20
HARD_INTRADAY_RANGE = 0.115
HARD_UPSIDE_SPIKE = 0.090
LOTTERY_REJECT = 0.74
CROWDING_REJECT = 0.82
SIDEWAYS_MOMENTUM_20_BAND = 0.018
SIDEWAYS_MOMENTUM_60_BAND = 0.025
SIDEWAYS_TREND_BAND = 0.018


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    candidates: list[dict[str, Any]] = []
    for symbol_key in context.symbol_keys:
        if not symbol_key.startswith("KRX:"):
            continue
        item = _features(context, symbol_key)
        if item is None:
            continue
        if _is_defensive_reject(item):
            continue
        score = _score(item)
        if score < MIN_SCORE:
            continue
        candidates.append({**item, "score": score, "symbol_key": symbol_key})

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        volatility = float(item["normalized_volatility"])
        score = float(item["score"])
        heat_penalty = float(item["lottery_penalty"]) * 0.50 + float(item["crowding_penalty"]) * 0.35
        insights.append(
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(str(item["symbol_key"])),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + HORIZON,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=ALPHA_ID,
                alpha_version=VERSION,
                magnitude=float(item["stable_trend_score"]) - heat_penalty,
                confidence=min(0.92, max(0.45, 0.50 + score * 0.38 - heat_penalty * 0.10)),
                weight=min(0.12, max(0.025, 0.105 - volatility * 0.35 - heat_penalty * 0.025)),
                score=score,
                group_id="krw-lowvol-defensive",
                reason="anti_lottery_defensive_rank",
                metadata=_metadata(item, rank=rank, selected_count=len(selected)),
            )
        )
    return insights


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_60", "rolling_dollar_volume_20", "volume"))
    volatility_20 = _normalized(context, symbol_key, close, ("stddev_20_close", "atr_14"))
    volatility_60 = _normalized(context, symbol_key, close, ("stddev_60_close", "stddev_20_close"))
    volatility_120 = _normalized(context, symbol_key, close, ("stddev_120_close", "stddev_60_close"))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close",))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "roc_20_close"))
    momentum_120 = _first_value(context, symbol_key, ("roc_120_close", "roc_60_close", "roc_20_close"))
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
    clv = _first_value(context, symbol_key, ("close_location_value",)) or 0.0
    retail_net_buy_ratio_20 = _ratio_metric(
        context,
        symbol_key,
        (
            "retail_net_buy_ratio_20",
            "individual_net_buy_ratio_20",
            "retail_net_buy_value_to_traded_value_20",
        ),
    )
    retail_flow_z20 = _metric(
        context,
        symbol_key,
        (
            "retail_flow_z20",
            "retail_net_buy_z20",
            "individual_net_buy_z20",
        ),
    )
    retail_buy_concentration = _ratio_metric(
        context,
        symbol_key,
        (
            "retail_buy_concentration",
            "retail_participation",
            "individual_buy_ratio",
        ),
    )
    foreign_institution_net_sell_ratio_20 = _ratio_metric(
        context,
        symbol_key,
        (
            "foreign_institution_net_sell_ratio_20",
            "foreign_inst_net_sell_ratio_20",
            "smart_money_net_sell_ratio_20",
        ),
    )
    if (
        close is None
        or close < MIN_PRICE
        or liquidity is None
        or liquidity < MIN_LIQUIDITY
        or volatility_20 is None
        or momentum_20 is None
    ):
        return None

    base = {
        "close": close,
        "liquidity": liquidity,
        "volatility_20": volatility_20,
        "volatility_60": volatility_60 if volatility_60 is not None else volatility_20,
        "volatility_120": volatility_120 if volatility_120 is not None else volatility_20,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60 if momentum_60 is not None else momentum_20,
        "momentum_120": momentum_120 if momentum_120 is not None else momentum_60 if momentum_60 is not None else momentum_20,
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
        "clv": clv,
        "crowding_data_available": 1.0
        if any(
            value is not None
            for value in (
                retail_net_buy_ratio_20,
                retail_flow_z20,
                retail_buy_concentration,
                foreign_institution_net_sell_ratio_20,
            )
        )
        else 0.0,
        "retail_net_buy_ratio_20": retail_net_buy_ratio_20 or 0.0,
        "retail_flow_z20": retail_flow_z20 or 0.0,
        "retail_buy_concentration": retail_buy_concentration or 0.0,
        "foreign_institution_net_sell_ratio_20": foreign_institution_net_sell_ratio_20 or 0.0,
        "quality_score": _quality_score(
            _metric(context, symbol_key, ("roe", "return_on_equity")),
            _metric(context, symbol_key, ("debt_ratio", "debt_to_equity")),
        ),
        "value_score": _value_score(
            _metric(context, symbol_key, ("per", "pe", "trailing_pe")),
            _metric(context, symbol_key, ("pbr", "pb", "price_to_book")),
        ),
        "dividend_score": _dividend_score(
            _metric(context, symbol_key, ("dividend_yield", "trailing_dividend_yield"))
        ),
    }
    base["normalized_volatility"] = max(
        base["volatility_20"],
        base["volatility_60"] * 0.85,
        base["volatility_120"] * 0.70,
    )
    base["low_vol_score"] = _low_vol_score(base)
    base["stable_trend_score"] = _stable_trend_score(base)
    base["sideways_penalty"] = _sideways_penalty(base)
    base["lottery_penalty"] = _lottery_penalty(base)
    base["real_crowding_penalty"] = _real_crowding_penalty(base)
    base["crowding_penalty"] = _crowding_penalty(base)
    base["turnover_shock_penalty"] = _turnover_shock_penalty(base)
    return base


def _is_defensive_reject(item: dict[str, float]) -> bool:
    volatility = item["normalized_volatility"]
    if volatility >= HARD_MAX_NORMALIZED_VOLATILITY:
        return True
    if volatility > MAX_NORMALIZED_VOLATILITY and item["momentum_60"] < 0.08:
        return True
    if item["momentum_20"] < -0.08 and item["drawdown_20"] < -0.12:
        return True
    if item["momentum_60"] < -0.12:
        return True
    if item["drawdown_60"] < -0.28 and item["trend"] < -0.01:
        return True
    if item["gap"] > 0.09 or item["high_low_range"] > HARD_INTRADAY_RANGE:
        return True
    if item["volume_ratio_20"] >= HARD_VOLUME_RATIO:
        return True
    if item["bar_return"] > HARD_UPSIDE_SPIKE and item["volume_ratio_20"] > 2.0:
        return True
    if item["lottery_penalty"] >= LOTTERY_REJECT or item["crowding_penalty"] >= CROWDING_REJECT:
        return True
    return False


def _score(item: dict[str, float]) -> float:
    liquidity_score = min(item["liquidity"] / 8_000_000_000.0, 1.0)
    drawdown_penalty = min(abs(min(item["drawdown_60"], 0.0)) / 0.35, 1.0)
    close_quality = max(item["clv"], 0.0) * 0.018
    return (
        item["low_vol_score"] * 0.32
        + item["stable_trend_score"] * 0.18
        + item["quality_score"] * 0.14
        + item["value_score"] * 0.10
        + item["dividend_score"] * 0.07
        + liquidity_score * 0.05
        + close_quality
        - item["lottery_penalty"] * 0.22
        - item["crowding_penalty"] * 0.18
        - item["turnover_shock_penalty"] * 0.12
        - item["sideways_penalty"]
        - drawdown_penalty * 0.05
    )


def _metadata(item: dict[str, Any], *, rank: int, selected_count: int) -> dict[str, float | int | str]:
    volatility = float(item["normalized_volatility"])
    if volatility <= 0.045 and item["crowding_penalty"] <= 0.20 and item["lottery_penalty"] <= 0.25:
        risk_bucket = "calm"
    elif volatility <= 0.075 and item["crowding_penalty"] <= 0.35 and item["lottery_penalty"] <= 0.40:
        risk_bucket = "normal"
    else:
        risk_bucket = "defensive"
    return {
        "style": "kr_lowvol_defensive_v2",
        "factor_version": VERSION,
        "rank": rank,
        "selected_count": selected_count,
        "close": float(item["close"]),
        "liquidity": float(item["liquidity"]),
        "normalized_volatility": volatility,
        "volatility_20": float(item["volatility_20"]),
        "volatility_60": float(item["volatility_60"]),
        "volatility_120": float(item["volatility_120"]),
        "momentum_20": float(item["momentum_20"]),
        "momentum_60": float(item["momentum_60"]),
        "momentum_120": float(item["momentum_120"]),
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
        "sideways_penalty": float(item["sideways_penalty"]),
        "quality_score": float(item["quality_score"]),
        "value_score": float(item["value_score"]),
        "dividend_score": float(item["dividend_score"]),
        "lottery_penalty": float(item["lottery_penalty"]),
        "crowding_data_available": float(item["crowding_data_available"]),
        "retail_net_buy_ratio_20": float(item["retail_net_buy_ratio_20"]),
        "retail_flow_z20": float(item["retail_flow_z20"]),
        "retail_buy_concentration": float(item["retail_buy_concentration"]),
        "foreign_institution_net_sell_ratio_20": float(item["foreign_institution_net_sell_ratio_20"]),
        "real_crowding_penalty": float(item["real_crowding_penalty"]),
        "crowding_penalty": float(item["crowding_penalty"]),
        "turnover_shock_penalty": float(item["turnover_shock_penalty"]),
        "risk_bucket": risk_bucket,
    }


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


def _sideways_penalty(item: dict[str, float]) -> float:
    flatness = (
        _clamp((SIDEWAYS_MOMENTUM_20_BAND - abs(item["momentum_20"])) / SIDEWAYS_MOMENTUM_20_BAND) * 0.45
        + _clamp((SIDEWAYS_MOMENTUM_60_BAND - abs(item["momentum_60"])) / SIDEWAYS_MOMENTUM_60_BAND) * 0.30
        + _clamp((SIDEWAYS_TREND_BAND - abs(item["trend"])) / SIDEWAYS_TREND_BAND) * 0.25
    )
    support = (
        _clamp(item["quality_score"]) * 0.42
        + _clamp(item["value_score"]) * 0.34
        + _clamp(item["dividend_score"]) * 0.24
    )
    return _clamp(flatness * (0.030 + (1.0 - support) * 0.050), 0.0, 0.080)


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
    proxy = _clamp(
        turnover * 0.62
        + _clamp((abs(item["bar_return"]) - 0.045) / 0.075) * 0.18
        + _clamp((item["volume_momentum_20"] - 0.35) / 1.25) * 0.12
        + (0.12 if item["volume_ratio_20"] > 2.3 and item["bar_return"] > 0.035 else 0.0)
    )
    if item["crowding_data_available"] <= 0:
        return proxy
    return _clamp(item["real_crowding_penalty"] * 0.72 + proxy * 0.38)


def _real_crowding_penalty(item: dict[str, float]) -> float:
    penalty = (
        _clamp((item["retail_net_buy_ratio_20"] - 0.08) / 0.22) * 0.32
        + _clamp((item["retail_flow_z20"] - 1.00) / 2.50) * 0.24
        + _clamp((item["retail_buy_concentration"] - 0.55) / 0.25) * 0.22
        + _clamp((item["foreign_institution_net_sell_ratio_20"] - 0.04) / 0.18) * 0.22
    )
    if item["retail_net_buy_ratio_20"] > 0.10 and item["foreign_institution_net_sell_ratio_20"] > 0.03:
        penalty += 0.12
    return _clamp(penalty)


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


def _trend(context: SnapshotContext, symbol_key: str, close: float | None) -> float:
    if close is None or close <= 0:
        return 0.0
    average = _first_value(context, symbol_key, ("sma_60_close", "sma_20_close"))
    if average is None or average <= 0:
        return 0.0
    return (close / average) - 1.0


def _normalized(context: SnapshotContext, symbol_key: str, close: float | None, names: tuple[str, ...]) -> float | None:
    if close is None or close <= 0:
        return None
    values = [_first_value(context, symbol_key, (name,)) for name in names]
    normalized = [value / close for value in values if value is not None]
    return max(normalized) if normalized else None


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _metric(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.fundamental(symbol_key, name)
        if value is None:
            value = context.metadata_value(symbol_key, name)
        parsed = _safe_float(value)
        if parsed is not None:
            return parsed
    return None


def _ratio_metric(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    value = _metric(context, symbol_key, names)
    if value is None:
        return None
    return value / 100.0 if abs(value) > 2.0 else value


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return min(max(float(value), low), high)

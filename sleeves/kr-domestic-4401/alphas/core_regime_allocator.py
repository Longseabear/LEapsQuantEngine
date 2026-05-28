from __future__ import annotations

from datetime import timedelta
from typing import Any

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "kr-domestic-4401-core-regime"
VERSION = "0.1.0"
EVALUATION_CADENCE = "daily_at 09:05 Asia/Seoul"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=14)
HEDGE_HORIZON = timedelta(days=1)

MARKET_PROXY = "KRX:069500"
DEFENSIVE_SYMBOLS = ("KRX:488770", "KRX:153130", "KRX:357870")
HEDGE_SYMBOL = "KRX:114800"
MAX_RISK_ASSETS = 10
MIN_LIQUIDITY = 800_000_000.0
MAX_VOLATILITY = 0.115
HARD_SHOCK_RETURN = -0.040
SHOCK_RETURN_SIGMA = -2.50
RISK_OFF_RETURN_SIGMA = -1.60
SHOCK_DRAWDOWN = -0.100
RISK_OFF_DRAWDOWN = -0.075


def generate(context: SnapshotContext) -> list[Insight]:
    if MARKET_PROXY not in context.available_symbol_keys:
        return []
    proxy = _features(context, MARKET_PROXY)
    if proxy is None:
        return []
    regime = _regime(proxy)
    insights: list[Insight] = []

    if regime in {"strong_risk_on", "risk_on", "neutral"} and context.allows_new_entries:
        risky = _rank_risk_assets(context, regime)
        insights.extend(_risk_insights(context, risky, regime))

    insights.extend(_defensive_insights(context, regime, proxy))

    if regime != "shock":
        insights.extend(_flat_hedge(context, regime, proxy))
    if regime in {"risk_off", "shock"}:
        insights.extend(_flat_risky_holdings(context, regime, proxy))
    return insights


def _rank_risk_assets(context: SnapshotContext, regime: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for symbol_key in context.symbol_keys:
        if symbol_key in DEFENSIVE_SYMBOLS or symbol_key == HEDGE_SYMBOL:
            continue
        item = _features(context, symbol_key)
        if item is None:
            continue
        metadata = context.metadata(symbol_key)
        role = str(metadata.get("role", ""))
        if symbol_key != MARKET_PROXY and item["liquidity"] < MIN_LIQUIDITY:
            continue
        if item["volatility"] > MAX_VOLATILITY and role not in {"risk_proxy", "risk_asset"}:
            continue
        score = _risk_score(item, role=role, regime=regime)
        if score <= 0.0:
            continue
        candidates.append({**item, "symbol_key": symbol_key, "role": role, "score": score})
    return sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_RISK_ASSETS
    ]


def _risk_insights(context: SnapshotContext, ranked: list[dict[str, Any]], regime: str) -> list[Insight]:
    if not ranked:
        return []
    budget = {
        "strong_risk_on": 0.82,
        "risk_on": 0.68,
        "neutral": 0.36,
    }.get(regime, 0.0)
    if budget <= 0.0:
        return []
    score_total = sum(max(float(item["score"]), 0.01) for item in ranked)
    insights: list[Insight] = []
    for rank, item in enumerate(ranked, start=1):
        raw_weight = budget * max(float(item["score"]), 0.01) / score_total
        max_weight = 0.30 if item["symbol_key"] == MARKET_PROXY else 0.12
        weight = min(max_weight, max(0.025, raw_weight))
        insights.append(
            _insight(
                context,
                str(item["symbol_key"]),
                InsightDirection.UP,
                weight=weight,
                score=float(item["score"]),
                reason=f"core_regime_{regime}_risk_asset",
                metadata={
                    "regime": regime,
                    "rank": rank,
                    "role": item["role"],
                    "style": "core_regime_allocator",
                    "bucket": "risk",
                    "momentum_20": item["momentum_20"],
                    "momentum_60": item["momentum_60"],
                    "trend_20": item["trend_20"],
                    "trend_60": item["trend_60"],
                    "volatility": item["volatility"],
                    "return_volatility": item["return_volatility"],
                    "drawdown_20": item["drawdown_20"],
                    "liquidity": item["liquidity"],
                },
            )
        )
    return insights


def _defensive_insights(context: SnapshotContext, regime: str, proxy: dict[str, float]) -> list[Insight]:
    defensive_budget = {
        "strong_risk_on": 0.06,
        "risk_on": 0.12,
        "neutral": 0.36,
        "risk_off": 0.82,
        "shock": 0.88,
    }.get(regime, 0.50)
    symbols = [symbol for symbol in DEFENSIVE_SYMBOLS if symbol in context.available_symbol_keys]
    if not symbols:
        return []
    per_symbol = defensive_budget / len(symbols)
    insights: list[Insight] = []
    for symbol_key in symbols:
        insights.append(
            _insight(
                context,
                symbol_key,
                InsightDirection.UP,
                weight=min(0.35, max(0.03, per_symbol)),
                score=1.0,
                reason=f"core_regime_{regime}_defensive_cash_like",
                metadata={
                    "regime": regime,
                    "style": "core_regime_allocator",
                    "bucket": "defensive",
                    "market_proxy": MARKET_PROXY,
                    "proxy_momentum_20": proxy["momentum_20"],
                    "proxy_momentum_60": proxy["momentum_60"],
                    "proxy_return_1": proxy["return_1"],
                    "proxy_return_volatility": proxy["return_volatility"],
                },
            )
        )
    if regime == "shock" and HEDGE_SYMBOL in context.available_symbol_keys:
        insights.append(
            _insight(
                context,
                HEDGE_SYMBOL,
                InsightDirection.UP,
                weight=0.08,
                score=0.50,
                reason="core_regime_shock_small_inverse_hedge",
                horizon=HEDGE_HORIZON,
                metadata={
                    "regime": regime,
                    "style": "core_regime_allocator",
                    "bucket": "hedge",
                    "market_proxy": MARKET_PROXY,
                    "proxy_return_volatility": proxy["return_volatility"],
                },
            )
        )
    return insights


def _flat_hedge(context: SnapshotContext, regime: str, proxy: dict[str, float]) -> list[Insight]:
    if HEDGE_SYMBOL not in context.available_symbol_keys:
        return []
    return [
        _insight(
            context,
            HEDGE_SYMBOL,
            InsightDirection.FLAT,
            weight=0.0,
            score=0.0,
            reason=f"core_regime_{regime}_inverse_hedge_exit",
            metadata={
                "regime": regime,
                "style": "core_regime_allocator",
                "bucket": "hedge_exit",
                "market_proxy": MARKET_PROXY,
                "proxy_momentum_20": proxy["momentum_20"],
                "proxy_momentum_60": proxy["momentum_60"],
                "proxy_return_1": proxy["return_1"],
                "proxy_return_volatility": proxy["return_volatility"],
            },
        )
    ]


def _flat_risky_holdings(context: SnapshotContext, regime: str, proxy: dict[str, float]) -> list[Insight]:
    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        if symbol_key in DEFENSIVE_SYMBOLS or symbol_key == HEDGE_SYMBOL:
            continue
        metadata = context.metadata(symbol_key)
        if str(metadata.get("role", "")) in {"defensive", "hedge"}:
            continue
        insights.append(
            _insight(
                context,
                symbol_key,
                InsightDirection.FLAT,
                weight=0.0,
                score=0.0,
                reason=f"core_regime_{regime}_risk_asset_flat",
                metadata={
                    "regime": regime,
                    "style": "core_regime_allocator",
                    "bucket": "risk_reduction",
                    "market_proxy": MARKET_PROXY,
                    "proxy_momentum_20": proxy["momentum_20"],
                    "proxy_momentum_60": proxy["momentum_60"],
                    "proxy_return_1": proxy["return_1"],
                    "proxy_return_volatility": proxy["return_volatility"],
                },
            )
        )
    return insights


def _regime(proxy: dict[str, float]) -> str:
    return_sigma = _return_sigma(proxy)
    if proxy["return_1"] <= HARD_SHOCK_RETURN:
        return "shock"
    if return_sigma <= SHOCK_RETURN_SIGMA and (proxy["trend_20"] < 0.0 or proxy["drawdown_20"] <= -0.040):
        return "shock"
    if proxy["drawdown_20"] <= SHOCK_DRAWDOWN and proxy["trend_60"] < 0.0:
        return "shock"
    if (
        proxy["trend_60"] < -0.015
        and proxy["momentum_20"] < 0.0
    ) or return_sigma <= RISK_OFF_RETURN_SIGMA or proxy["drawdown_20"] <= RISK_OFF_DRAWDOWN:
        return "risk_off"
    if proxy["trend_20"] > 0.0 and proxy["trend_60"] > 0.0 and proxy["momentum_20"] > 0.025:
        if proxy["momentum_60"] > 0.055 and proxy["volatility"] <= 0.055:
            return "strong_risk_on"
        return "risk_on"
    return "neutral"


def _return_sigma(proxy: dict[str, float]) -> float:
    volatility = max(float(proxy.get("return_volatility") or proxy.get("volatility") or 0.0), 0.001)
    return float(proxy.get("return_1") or 0.0) / volatility


def _risk_score(item: dict[str, float], *, role: str, regime: str) -> float:
    role_bonus = {
        "risk_proxy": 0.18,
        "risk_asset": 0.14,
        "mega_cap": 0.12,
        "core_stock": 0.08,
        "dividend_stock": 0.06,
        "satellite_stock": -0.02,
        "satellite_risk": -0.04,
    }.get(role, 0.0)
    regime_penalty = 0.0 if regime != "neutral" else 0.08
    return (
        0.36
        + item["momentum_20"] * 1.20
        + item["momentum_60"] * 0.75
        + item["trend_20"] * 0.80
        + item["trend_60"] * 0.55
        - item["volatility"] * 1.60
        + item["drawdown_20"] * 0.40
        + min(item["liquidity"] / 12_000_000_000.0, 0.12)
        + role_bonus
        - regime_penalty
    )


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    sma20 = _first_value(context, symbol_key, ("sma_20_close",))
    sma60 = _first_value(context, symbol_key, ("sma_60_close", "sma_20_close"))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close",))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "roc_20_close"))
    return_1 = _first_value(context, symbol_key, ("return_1_close", "bar_return_close"))
    drawdown_20 = _first_value(context, symbol_key, ("drawdown_20_close",)) or 0.0
    liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_60", "rolling_dollar_volume_20", "volume")) or 0.0
    return_volatility = _first_value(context, symbol_key, ("return_stddev_20_close", "return_volatility_20_close"))
    volatility = _normalized_volatility(context, symbol_key, close)
    if (
        close is None
        or close <= 0
        or sma20 is None
        or sma20 <= 0
        or sma60 is None
        or sma60 <= 0
        or momentum_20 is None
        or volatility is None
    ):
        return None
    return {
        "close": close,
        "trend_20": (close / sma20) - 1.0,
        "trend_60": (close / sma60) - 1.0,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60 if momentum_60 is not None else momentum_20,
        "return_1": return_1 if return_1 is not None else 0.0,
        "return_volatility": return_volatility if return_volatility is not None else volatility,
        "drawdown_20": drawdown_20,
        "liquidity": liquidity,
        "volatility": volatility,
    }


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float | None) -> float | None:
    if close is None or close <= 0:
        return None
    values = []
    for name in ("stddev_20_close", "atr_14"):
        value = _first_value(context, symbol_key, (name,))
        if value is not None:
            values.append(value / close)
    return max(values) if values else None


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _insight(
    context: SnapshotContext,
    symbol_key: str,
    direction: InsightDirection,
    *,
    weight: float,
    score: float,
    reason: str,
    metadata: dict[str, Any],
    horizon: timedelta | None = None,
) -> Insight:
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(symbol_key),
        direction=direction,
        generated_at=context.as_of,
        expires_at=context.as_of + (horizon or HORIZON),
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=score,
        confidence=min(0.92, max(0.45, 0.52 + score * 0.20)),
        weight=weight,
        score=score,
        group_id="krw-core-regime",
        reason=reason,
        metadata=metadata,
    )

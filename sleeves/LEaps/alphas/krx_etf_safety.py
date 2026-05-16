from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-krx-etf-safety"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=3)

BENCHMARK_SYMBOL = "KRX:069500"
CASH_LIKE_PRIORITY = ("KRX:488770", "KRX:423160", "KRX:459580")
MARKET_BETA_PRIORITY = ("KRX:069500", "KRX:102110", "KRX:278530")
INVERSE_SYMBOL = "KRX:114800"
DISABLED_LEVERAGED_SYMBOLS = {"KRX:122630", "KRX:252670"}

REGIME_BUDGETS = {
    "shock": {
        "stock_gross_cap": 0.45,
        "cash_like_pct": 0.42,
        "inverse_pct": 0.08,
        "market_beta_pct": 0.0,
    },
    "risk_off": {
        "stock_gross_cap": 0.35,
        "cash_like_pct": 0.50,
        "inverse_pct": 0.05,
        "market_beta_pct": 0.0,
    },
    "neutral": {
        "stock_gross_cap": 0.65,
        "cash_like_pct": 0.25,
        "inverse_pct": 0.0,
        "market_beta_pct": 0.05,
    },
    "risk_on": {
        "stock_gross_cap": 0.78,
        "cash_like_pct": 0.12,
        "inverse_pct": 0.0,
        "market_beta_pct": 0.06,
    },
    "strong_risk_on": {
        "stock_gross_cap": 0.95,
        "cash_like_pct": 0.03,
        "inverse_pct": 0.0,
        "market_beta_pct": 0.05,
    },
}


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    metrics = _benchmark_metrics(context)
    regime = _regime(metrics)
    budget = REGIME_BUDGETS[regime]
    common_metadata = {
        "role": "krw_etf_safety_controller",
        "safety_regime": regime,
        "stock_gross_cap": budget["stock_gross_cap"],
        "cash_like_pct": budget["cash_like_pct"],
        "inverse_pct": budget["inverse_pct"],
        "market_beta_pct": budget["market_beta_pct"],
        "benchmark_symbol": BENCHMARK_SYMBOL,
        **metrics,
    }

    input_symbols = set(context.symbol_keys)
    insights: list[Insight] = []
    cash_symbols = _available_symbols(input_symbols, CASH_LIKE_PRIORITY)
    if cash_symbols and budget["cash_like_pct"] > 0:
        target_pct = budget["cash_like_pct"] / len(cash_symbols)
        for cash_symbol in cash_symbols:
            insights.append(
                _target_insight(
                    context,
                    symbol_key=cash_symbol,
                    role="cash_like",
                    target_pct=target_pct,
                    score=0.70 if regime in {"shock", "risk_off"} else 0.35,
                    reason="krx_etf_safety_cash_buffer",
                    metadata=common_metadata,
                )
            )

    market_beta_symbol = _first_available(input_symbols, MARKET_BETA_PRIORITY)
    if market_beta_symbol is not None and budget["market_beta_pct"] > 0:
        insights.append(
            _target_insight(
                context,
                symbol_key=market_beta_symbol,
                role="market_beta",
                target_pct=budget["market_beta_pct"],
                score=0.55 if regime in {"risk_on", "strong_risk_on"} else 0.20,
                reason="krx_etf_safety_market_beta",
                metadata=common_metadata,
            )
        )

    if INVERSE_SYMBOL in input_symbols and budget["inverse_pct"] > 0:
        insights.append(
            _target_insight(
                context,
                symbol_key=INVERSE_SYMBOL,
                role="inverse",
                target_pct=budget["inverse_pct"],
                score=0.60 if regime == "shock" else 0.40,
                reason="krx_etf_safety_inverse_hedge",
                metadata=common_metadata,
            )
        )

    for symbol_key in sorted(input_symbols & DISABLED_LEVERAGED_SYMBOLS):
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
                confidence=0.7,
                weight=0.0,
                score=0.0,
                group_id="krw-etf-safety",
                reason="leveraged_etf_disabled_for_safety_bucket",
                metadata={**common_metadata, "target_role": "disabled_leveraged"},
            )
        )

    return insights


def _target_insight(
    context: SnapshotContext,
    *,
    symbol_key: str,
    role: str,
    target_pct: float,
    score: float,
    reason: str,
    metadata: dict[str, float | str | None],
) -> Insight:
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(symbol_key),
        direction=InsightDirection.UP,
        generated_at=context.as_of,
        expires_at=context.as_of + HORIZON,
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=target_pct,
        confidence=min(0.92, 0.55 + score * 0.45),
        weight=target_pct,
        score=score,
        group_id="krw-etf-safety",
        reason=reason,
        metadata={
            **metadata,
            "target_role": role,
            "target_bucket_pct": target_pct,
        },
    )


def _benchmark_metrics(context: SnapshotContext) -> dict[str, float | None]:
    close = _first_value(context, BENCHMARK_SYMBOL, ("identity_close", "close"))
    slow_average = _first_value(context, BENCHMARK_SYMBOL, ("sma_20_close", "sma_5_close"))
    momentum_5 = _first_value(context, BENCHMARK_SYMBOL, ("momentum_5_close",))
    momentum_20 = _first_value(context, BENCHMARK_SYMBOL, ("roc_20_close", "momentum_20_close"))
    momentum_60 = _first_value(context, BENCHMARK_SYMBOL, ("roc_60_close", "momentum_60_close"))
    rolling_high = _first_value(context, BENCHMARK_SYMBOL, ("rolling_max_20_close",))
    volatility = _normalized_volatility(context, BENCHMARK_SYMBOL, close)
    trend_strength = None
    if close is not None and slow_average is not None and slow_average > 0:
        trend_strength = (close / slow_average) - 1.0
    pullback_from_high = None
    if close is not None and rolling_high is not None and rolling_high > 0:
        pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
    return {
        "benchmark_close": close,
        "benchmark_slow_average": slow_average,
        "benchmark_momentum_5": momentum_5,
        "benchmark_momentum_20": momentum_20,
        "benchmark_momentum_60": momentum_60,
        "benchmark_trend_strength": trend_strength,
        "benchmark_pullback_from_high": pullback_from_high,
        "benchmark_volatility": volatility,
    }


def _regime(metrics: dict[str, float | None]) -> str:
    trend = metrics["benchmark_trend_strength"]
    momentum_5 = metrics["benchmark_momentum_5"]
    momentum_20 = metrics["benchmark_momentum_20"]
    momentum_60 = metrics["benchmark_momentum_60"]
    pullback = metrics["benchmark_pullback_from_high"]
    volatility = metrics["benchmark_volatility"] or 0.0

    if pullback is not None and pullback >= 0.055:
        return "shock"
    if momentum_5 is not None and momentum_5 <= -0.045:
        return "shock"
    if volatility >= 0.24:
        return "shock"
    if pullback is not None and pullback >= 0.10:
        return "risk_off"
    if trend is not None and momentum_20 is not None and trend < 0.0 and momentum_20 <= 0.0:
        return "risk_off"
    if trend is not None and momentum_20 is not None and trend > 0.0 and momentum_20 > 0.0:
        longer_momentum = momentum_60 if momentum_60 is not None else momentum_20
        if trend >= 0.08 and longer_momentum >= 0.12 and volatility <= 0.16:
            return "strong_risk_on"
        return "risk_on"
    return "neutral"


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float | None) -> float:
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


def _first_available(input_symbols: set[str], ordered_symbols: tuple[str, ...]) -> str | None:
    for symbol_key in ordered_symbols:
        if symbol_key in input_symbols:
            return symbol_key
    return None


def _available_symbols(input_symbols: set[str], ordered_symbols: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(symbol_key for symbol_key in ordered_symbols if symbol_key in input_symbols)


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None

from __future__ import annotations

from datetime import datetime, timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-krx-etf-safety"
VERSION = "0.1.2"
EVALUATION_CADENCE = "every_5_minutes"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=3)

BENCHMARK_SYMBOL = "KRX:069500"
CASH_LIKE_PRIORITY = ("KRX:488770", "KRX:423160", "KRX:459580")
MARKET_BETA_PRIORITY = ("KRX:069500", "KRX:102110", "KRX:278530")
INVERSE_SYMBOL = "KRX:114800"
DISABLED_LEVERAGED_SYMBOLS = {"KRX:122630", "KRX:252670"}
INVERSE_POLICY_NAMESPACE = "inverse_product_risk"
INVERSE_FULL_TARGET_DAYS = 1
INVERSE_MAX_TARGET_DAYS = 2
INVERSE_DECAY_FACTOR = 0.50
DAILY_RESET_PRODUCT_RISK = "daily_reset_compounding_decay"

REGIME_BUDGETS = {
    "shock": {
        "stock_gross_cap": 0.20,
        "cash_like_pct": 0.60,
        "inverse_pct": 0.20,
        "market_beta_pct": 0.0,
    },
    "risk_off": {
        "stock_gross_cap": 0.35,
        "cash_like_pct": 0.55,
        "inverse_pct": 0.10,
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
    raw_budget = REGIME_BUDGETS[regime]
    inverse_policy = _inverse_policy(context, raw_budget["inverse_pct"])
    inverse_pct = inverse_policy["adjusted_inverse_pct"]
    cash_like_pct = raw_budget["cash_like_pct"] + max(raw_budget["inverse_pct"] - inverse_pct, 0.0)
    common_metadata = {
        "role": "krw_etf_safety_controller",
        "safety_regime": regime,
        "stock_gross_cap": raw_budget["stock_gross_cap"],
        "cash_like_pct": cash_like_pct,
        "inverse_pct": inverse_pct,
        "base_inverse_pct": raw_budget["inverse_pct"],
        "market_beta_pct": raw_budget["market_beta_pct"],
        "benchmark_symbol": BENCHMARK_SYMBOL,
        **inverse_policy,
        **metrics,
    }

    input_symbols = set(context.symbol_keys)
    insights: list[Insight] = []
    cash_symbols = _available_symbols(input_symbols, CASH_LIKE_PRIORITY)
    if cash_symbols and cash_like_pct > 0:
        target_pct = cash_like_pct / len(cash_symbols)
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
    if market_beta_symbol is not None and raw_budget["market_beta_pct"] > 0:
        insights.append(
            _target_insight(
                context,
                symbol_key=market_beta_symbol,
                role="market_beta",
                target_pct=raw_budget["market_beta_pct"],
                score=0.55 if regime in {"risk_on", "strong_risk_on"} else 0.20,
                reason="krx_etf_safety_market_beta",
                metadata=common_metadata,
            )
        )

    if INVERSE_SYMBOL in input_symbols and raw_budget["inverse_pct"] > 0:
        if inverse_pct > 0:
            insights.append(
                _target_insight(
                    context,
                    symbol_key=INVERSE_SYMBOL,
                    role="inverse",
                    target_pct=inverse_pct,
                    score=0.60 if regime == "shock" else 0.40,
                    reason="krx_etf_safety_inverse_hedge",
                    metadata=common_metadata,
                )
            )
        else:
            insights.append(
                _flat_insight(
                    context,
                    symbol_key=INVERSE_SYMBOL,
                    role="inverse",
                    reason="krx_etf_safety_inverse_holding_limit",
                    metadata=common_metadata,
                )
            )

    for symbol_key in sorted(input_symbols & DISABLED_LEVERAGED_SYMBOLS):
        insights.append(
            _flat_insight(
                context,
                symbol_key=symbol_key,
                role="disabled_leveraged",
                reason="leveraged_etf_disabled_for_safety_bucket",
                metadata={
                    **common_metadata,
                    "leveraged_etf_policy": "blocked",
                    "product_risk": DAILY_RESET_PRODUCT_RISK,
                    "max_holding_days": 0,
                },
            )
        )

    return insights


def state_patches(context: SnapshotContext, insights: tuple[Insight, ...] = ()) -> tuple:
    inverse_insights = [insight for insight in insights if insight.symbol_key == INVERSE_SYMBOL]
    if not inverse_insights:
        return ()
    latest = max(inverse_insights, key=lambda insight: insight.generated_at)
    metadata = dict(latest.metadata)
    return (
        context.model_state.object_set(
            {
                "target_active": latest.direction is InsightDirection.UP,
                "last_target_date": context.as_of.date().isoformat(),
                "target_day_count": int(metadata.get("inverse_target_day_count") or 0),
                "base_inverse_pct": float(metadata.get("base_inverse_pct") or 0.0),
                "adjusted_inverse_pct": float(metadata.get("inverse_pct") or 0.0),
                "policy_action": str(metadata.get("inverse_policy_action") or ""),
                "product_risk": DAILY_RESET_PRODUCT_RISK,
            },
            model_id=ALPHA_ID,
            namespace=INVERSE_POLICY_NAMESPACE,
            symbol_key=INVERSE_SYMBOL,
            reason="krx_etf_safety_inverse_product_risk",
            generated_at=context.as_of,
        ),
    )


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


def _flat_insight(
    context: SnapshotContext,
    *,
    symbol_key: str,
    role: str,
    reason: str,
    metadata: dict[str, float | str | None],
) -> Insight:
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(symbol_key),
        direction=InsightDirection.FLAT,
        generated_at=context.as_of,
        expires_at=context.as_of + timedelta(days=1),
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        confidence=0.8,
        weight=0.0,
        score=0.0,
        group_id="krw-etf-safety",
        reason=reason,
        metadata={
            **metadata,
            "target_role": role,
            "target_bucket_pct": 0.0,
        },
    )


def _benchmark_metrics(context: SnapshotContext) -> dict[str, float | None]:
    daily_close = _first_value(context, BENCHMARK_SYMBOL, ("identity_close", "close"))
    live_close = _first_value(context, BENCHMARK_SYMBOL, ("live_close",), ready_only=False)
    close = live_close if live_close is not None else daily_close
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
        "benchmark_close": daily_close,
        "benchmark_live_close": live_close,
        "benchmark_regime_close": close,
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


def _inverse_policy(context: SnapshotContext, base_target_pct: float) -> dict[str, float | int | str | bool]:
    target_day_count = _next_inverse_target_day_count(context, base_target_pct)
    adjusted_pct = _adjusted_inverse_pct(base_target_pct, target_day_count)
    if base_target_pct <= 0:
        action = "inactive"
    elif adjusted_pct <= 0:
        action = "blocked_max_target_days"
    elif adjusted_pct < base_target_pct:
        action = "decayed"
    else:
        action = "full"
    return {
        "inverse_target_day_count": target_day_count,
        "inverse_full_target_days": INVERSE_FULL_TARGET_DAYS,
        "inverse_max_target_days": INVERSE_MAX_TARGET_DAYS,
        "inverse_policy_action": action,
        "inverse_policy_enforced": True,
        "inverse_product_risk": DAILY_RESET_PRODUCT_RISK,
        "adjusted_inverse_pct": adjusted_pct,
    }


def _next_inverse_target_day_count(context: SnapshotContext, base_target_pct: float) -> int:
    if base_target_pct <= 0:
        return 0
    state = context.model_state.object_get(
        model_id=ALPHA_ID,
        namespace=INVERSE_POLICY_NAMESPACE,
        symbol_key=INVERSE_SYMBOL,
    )
    today = context.as_of.date()
    last_date = _parse_date(state.get("last_target_date"))
    previous_count = _safe_int(state.get("target_day_count"))
    if last_date == today:
        return max(previous_count, 1)
    if last_date is not None and (today - last_date).days == 1 and bool(state.get("target_active", False)):
        return previous_count + 1
    return 1


def _adjusted_inverse_pct(base_target_pct: float, target_day_count: int) -> float:
    if base_target_pct <= 0 or target_day_count <= 0:
        return 0.0
    if target_day_count <= INVERSE_FULL_TARGET_DAYS:
        return base_target_pct
    if target_day_count <= INVERSE_MAX_TARGET_DAYS:
        return base_target_pct * INVERSE_DECAY_FACTOR
    return 0.0


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


def _parse_date(value) -> object | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _safe_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_available(input_symbols: set[str], ordered_symbols: tuple[str, ...]) -> str | None:
    for symbol_key in ordered_symbols:
        if symbol_key in input_symbols:
            return symbol_key
    return None


def _available_symbols(input_symbols: set[str], ordered_symbols: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(symbol_key for symbol_key in ordered_symbols if symbol_key in input_symbols)


def _first_value(
    context: SnapshotContext,
    symbol_key: str,
    names: tuple[str, ...],
    *,
    ready_only: bool = True,
) -> float | None:
    for name in names:
        value = context.value(symbol_key, name, ready_only=ready_only)
        if value is not None:
            return value
    return None

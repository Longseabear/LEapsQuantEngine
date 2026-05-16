from __future__ import annotations

from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "us_etf_rotation_daa_momentum"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=7)
MAX_SELECTED = 3
OFFENSIVE_TICKERS = {"SPY", "QQQ", "IWM", "DIA", "SMH", "XLK", "XLF", "XLV", "XLE", "XLI"}
DEFENSIVE_TICKERS = {"TLT", "IEF", "GLD", "USMV", "XLP", "XLU"}
CANARY_TICKERS = ("SPY", "QQQ", "IWM")


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    regime = _risk_regime(context)
    candidates: list[dict[str, float | str | bool]] = []
    rejected: dict[str, str] = {}
    for symbol_key in context.symbol_keys:
        ticker = _ticker(symbol_key)
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        trend_average = _first_value(context, symbol_key, ("sma_200_close", "sma_100_close", "sma_20_close"))
        momentum_3m = _first_value(context, symbol_key, ("roc_63_close", "roc_60_close", "roc_20_close"))
        momentum_6m = _first_value(context, symbol_key, ("roc_126_close", "roc_120_close", "roc_63_close"))
        momentum_12m = _first_value(context, symbol_key, ("roc_252_close", "roc_240_close", "roc_126_close"))
        volatility = _first_value(context, symbol_key, ("stddev_63_close", "stddev_20_close", "volatility_20_close"))
        liquidity = _first_value(context, symbol_key, ("rolling_dollar_volume_20", "dollar_volume_1"))
        if close is None or trend_average is None or momentum_3m is None or momentum_6m is None:
            rejected[symbol_key] = "missing_momentum"
            continue

        defensive = ticker in DEFENSIVE_TICKERS
        offensive = ticker in OFFENSIVE_TICKERS
        if not defensive and not offensive:
            rejected[symbol_key] = "outside_daa_universe"
            continue
        if regime["risk_on"] < 0.5 and not defensive:
            rejected[symbol_key] = "canary_risk_off"
            continue

        trend_confirmed = close > trend_average
        composite_momentum = (0.50 * momentum_6m) + (0.30 * momentum_3m) + (0.20 * (momentum_12m or momentum_6m))
        if not trend_confirmed and not defensive:
            rejected[symbol_key] = "below_trend_filter"
            continue
        if composite_momentum <= 0 and not defensive:
            rejected[symbol_key] = "negative_absolute_momentum"
            continue

        normalized_volatility = 0.0 if volatility is None or close <= 0 else volatility / close
        volatility_penalty = min(normalized_volatility, 0.35) * (0.45 if defensive else 0.70)
        liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.04)
        trend_bonus = 0.04 if trend_confirmed else 0.0
        defensive_bonus = 0.08 if defensive and regime["risk_on"] < 0.5 else 0.0
        score = composite_momentum + trend_bonus + liquidity_bonus + defensive_bonus - volatility_penalty
        if score <= 0 and not defensive:
            rejected[symbol_key] = "score_below_zero"
            continue

        candidates.append(
            {
                "symbol_key": symbol_key,
                "score": score,
                "momentum": composite_momentum,
                "momentum_3m": momentum_3m,
                "momentum_6m": momentum_6m,
                "momentum_12m": momentum_12m or 0.0,
                "volatility": normalized_volatility,
                "liquidity": liquidity or 0.0,
                "close": close,
                "moving_average": trend_average,
                "defensive": defensive,
                "risk_on": bool(regime["risk_on"]),
                "canary_score": regime["canary_score"],
            }
        )

    selected = sorted(candidates, key=lambda item: (float(item["score"]), str(item["symbol_key"])), reverse=True)[
        :MAX_SELECTED
    ]
    selected_keys = {str(item["symbol_key"]) for item in selected}
    total_score = sum(max(float(item["score"]), 0.0) for item in selected)

    insights: list[Insight] = []
    for rank, item in enumerate(selected, start=1):
        score = float(item["score"])
        weight = max(score, 0.0) / total_score if total_score > 0 else 1.0 / max(len(selected), 1)
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
                confidence=min(0.92, 0.58 + max(score, 0.0)),
                weight=weight,
                score=score,
                reason="daa_canary_momentum_volatility_score",
                metadata={
                    "rank": rank,
                    "rank_count": len(selected),
                    "momentum": item["momentum"],
                    "momentum_3m": item["momentum_3m"],
                    "momentum_6m": item["momentum_6m"],
                    "momentum_12m": item["momentum_12m"],
                    "volatility": item["volatility"],
                    "liquidity": item["liquidity"],
                    "close": item["close"],
                    "moving_average": item["moving_average"],
                    "defensive": bool(item["defensive"]),
                    "risk_on": bool(item["risk_on"]),
                    "canary_score": item["canary_score"],
                },
            )
        )

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
                confidence=0.65,
                weight=0.0,
                score=0.0,
                reason="not_selected_by_daa_canary_model",
                metadata={
                    "rejection": rejected.get(symbol_key, "not_top_ranked"),
                    "risk_on": bool(regime["risk_on"]),
                    "canary_score": regime["canary_score"],
                },
            )
        )
    return insights


def _risk_regime(context: SnapshotContext) -> dict[str, float]:
    good = 0
    observed = 0
    for ticker in CANARY_TICKERS:
        symbol_key = f"US:{ticker}"
        close = _first_value(context, symbol_key, ("identity_close", "close"))
        trend = _first_value(context, symbol_key, ("sma_200_close", "sma_100_close", "sma_20_close"))
        momentum = _first_value(context, symbol_key, ("roc_126_close", "roc_63_close", "roc_20_close"))
        if close is None or trend is None or momentum is None:
            continue
        observed += 1
        if close > trend and momentum > 0:
            good += 1
    canary_score = good / observed if observed else 0.0
    return {"risk_on": 1.0 if canary_score >= 2.0 / 3.0 else 0.0, "canary_score": canary_score}


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _ticker(symbol_key: str) -> str:
    return symbol_key.split(":", 1)[-1].upper()

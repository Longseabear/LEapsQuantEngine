from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_ID = "semiconduct-kor-samsung-low-frequency-reentry"
VERSION = "0.1.0"
EVALUATION_CADENCE = "daily_at 09:05 Asia/Seoul"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=5)
MEMORY_LEADER_SYMBOL_KEYS = ("KRX:005930", "KRX:000660")
STATE_NAMESPACE = "low_frequency_reentry"

PROBE_TARGET = 0.25
RECLAIM_TARGET = 0.45
REBUILD_TARGET = 0.65
CORE_TARGET = 0.85
PROBE_ADD = 0.25
RECLAIM_ADD = 0.20
REBUILD_ADD = 0.20
CORE_ADD = 0.20
MAX_PLAUSIBLE_FEATURE_ABS = 3.0
HARD_VOLATILITY_BLOCK = 0.13
MIN_DIP_PULLBACK = 0.055
HOT_MOMENTUM_20 = 0.18
HOT_MOMENTUM_PULLBACK_REQUIRED = 0.10
EXTREME_MOMENTUM_20 = 0.28
EXTREME_MOMENTUM_PULLBACK_REQUIRED = 0.14
HIGH_PROXIMITY_PULLBACK = 0.05
OVEREXTENDED_SMA60_GAP = 0.18
OVEREXTENDED_SMA120_GAP = 0.28


def generate(context: SnapshotContext) -> list[Insight]:
    insights: list[Insight] = []
    for symbol_key in MEMORY_LEADER_SYMBOL_KEYS:
        if symbol_key not in context.symbol_keys:
            continue
        features = _features(context, symbol_key)
        if features is None:
            continue
        signal = _signal(features)
        if signal is None:
            continue
        insights.append(_insight(context, symbol_key, features, signal))
    insights.sort(key=lambda insight: float(insight.score or 0.0), reverse=True)
    return insights


def state_patches(context: SnapshotContext, insights: tuple[Insight, ...] = ()) -> tuple[StatePatch, ...]:
    patches: list[StatePatch] = []
    for insight in insights:
        if insight.symbol_key not in MEMORY_LEADER_SYMBOL_KEYS or insight.alpha_id != ALPHA_ID:
            continue
        metadata = dict(insight.metadata)
        patches.append(
            StatePatch(
                key=context.model_state.key(
                    model_id=ALPHA_ID,
                    namespace=STATE_NAMESPACE,
                    symbol_key=insight.symbol_key,
                ),
                value={
                    "last_signal_at": context.as_of.isoformat(),
                    "last_action": metadata.get("action"),
                    "last_reason": insight.reason,
                    "last_target_percent": metadata.get("target_percent"),
                    "last_target_delta_percent": metadata.get("target_delta_percent"),
                    "last_regime": metadata.get("regime"),
                    "last_close": metadata.get("close"),
                },
                reason="samsung_low_frequency_reentry_signal_mark",
            )
        )
    return tuple(patches)


def _insight(
    context: SnapshotContext,
    symbol_key: str,
    features: Mapping[str, float],
    signal: Mapping[str, Any],
) -> Insight:
    target_percent = float(signal["target_percent"])
    target_delta_percent = float(signal["target_delta_percent"])
    score = float(signal["score"])
    metadata = {
        "role": "memory_leader_low_frequency_buy_only_reentry",
        "strategy_mode": "daily_low_frequency_recovery_dca",
        "action": signal["action"],
        "phase": signal["phase"],
        "regime": signal["regime"],
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "max_target_percent": target_percent,
        "dynamic_gate": signal["dynamic_gate"],
        "close": features["close"],
        "fast_average": features["fast_average"],
        "sma20": features["sma20"],
        "sma60": features["sma60"],
        "sma120": features["sma120"],
        "momentum_5": features["momentum_5"],
        "momentum": features["momentum_20"],
        "momentum_60": features["momentum_60"],
        "pullback_from_high": features["pullback_from_high"],
        "drawdown_20": features["drawdown_20"],
        "near_rolling_low_pct": features["near_rolling_low_pct"],
        "zscore_20": features["zscore_20"],
        "bar_return_1": features["bar_return_1"],
        "close_location_value": features["close_location_value"],
        "volatility": features["volatility"],
        "extension_from_sma60": features["extension_from_sma60"],
        "extension_from_sma120": features["extension_from_sma120"],
        "overheat_status": signal["overheat_status"],
    }
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(symbol_key),
        direction=InsightDirection.UP,
        generated_at=context.as_of,
        expires_at=context.as_of + HORIZON,
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=max(features["momentum_20"], 0.0),
        confidence=min(0.94, 0.56 + score * 0.24),
        weight=target_percent,
        score=score,
        group_id="krw-memory-leader-low-frequency-reentry",
        reason=str(signal["reason"]),
        metadata=metadata,
    )


def _signal(features: Mapping[str, float]) -> dict[str, Any] | None:
    close = features["close"]
    fast = features["fast_average"]
    sma20 = features["sma20"]
    sma60 = features["sma60"]
    sma120 = features["sma120"]
    momentum_5 = features["momentum_5"]
    momentum_20 = features["momentum_20"]
    momentum_60 = features["momentum_60"]
    pullback = features["pullback_from_high"]
    drawdown_20 = features["drawdown_20"]
    zscore_20 = features["zscore_20"]
    bar_return_1 = features["bar_return_1"]
    clv = features["close_location_value"]
    volatility = features["volatility"]
    near_low = features["near_rolling_low_pct"]
    regime = _regime(features)
    overheat_status = _overheat_status(features)

    if _falling_knife(
        close=close,
        sma120=sma120,
        momentum_20=momentum_20,
        pullback=pullback,
        volatility=volatility,
    ):
        return None
    if overheat_status != "clear":
        return None

    low_rebound = near_low >= 0.015 and (bar_return_1 > 0.0 or clv >= 0.35 or momentum_5 > 0.0)
    stress_repair = (
        pullback >= MIN_DIP_PULLBACK
        or drawdown_20 <= -0.055
        or volatility >= 0.04
        or close < sma60
        or zscore_20 <= 0.35
    )
    if not (low_rebound or stress_repair):
        return None

    if close >= sma60 * 0.995 and momentum_20 > 0.015 and volatility <= 0.055:
        return _target_signal(
            action="accumulate_lowfreq_rebuild",
            phase="reentry",
            regime=regime,
            target_percent=REBUILD_TARGET,
            target_delta_percent=REBUILD_ADD,
            reason="samsung_lowfreq_rebuild_after_sma60_reclaim",
            dynamic_gate="sma60_reclaim_positive_medium_momentum",
            overheat_status=overheat_status,
            score=0.58 + min(momentum_20, 0.12),
        )
    if close >= sma20 and fast >= sma20 * 0.99 and momentum_5 > 0.0:
        return _target_signal(
            action="accumulate_lowfreq_reclaim",
            phase="reentry",
            regime=regime,
            target_percent=RECLAIM_TARGET,
            target_delta_percent=RECLAIM_ADD,
            reason="samsung_lowfreq_reclaim_after_sma20_repair",
            dynamic_gate="sma20_reclaim_positive_short_momentum",
            overheat_status=overheat_status,
            score=0.48 + min(max(momentum_5, 0.0) * 2.0, 0.12),
        )
    if (zscore_20 <= -2.0 or drawdown_20 <= -0.11) and momentum_20 > -0.055 and low_rebound:
        return _target_signal(
            action="accumulate_lowfreq_deep_dip",
            phase="accumulation",
            regime=regime,
            target_percent=RECLAIM_TARGET,
            target_delta_percent=RECLAIM_ADD,
            reason="samsung_lowfreq_deep_dip_rebound",
            dynamic_gate="deep_dip_with_daily_rebound",
            overheat_status=overheat_status,
            score=0.52 + min(abs(zscore_20) / 6.0, 0.20),
        )
    if (zscore_20 <= -1.2 or near_low <= 0.03 or pullback >= 0.075) and low_rebound:
        return _target_signal(
            action="accumulate_lowfreq_probe",
            phase="accumulation",
            regime=regime,
            target_percent=PROBE_TARGET,
            target_delta_percent=PROBE_ADD,
            reason="samsung_lowfreq_probe_after_stabilized_pullback",
            dynamic_gate="pullback_near_low_rebound",
            overheat_status=overheat_status,
            score=0.36 + min(max(momentum_5, 0.0) * 2.0, 0.10),
        )
    if stress_repair and MIN_DIP_PULLBACK <= pullback <= 0.16 and momentum_20 > 0.05 and close >= sma120 * 0.95:
        return _target_signal(
            action="accumulate_lowfreq_trend_pullback_probe",
            phase="accumulation",
            regime=regime,
            target_percent=PROBE_TARGET,
            target_delta_percent=PROBE_ADD,
            reason="samsung_lowfreq_positive_trend_pullback_probe",
            dynamic_gate="positive_medium_momentum_pullback_without_falling_knife",
            overheat_status=overheat_status,
            score=0.34 + min(momentum_20, 0.18) - min(volatility, HARD_VOLATILITY_BLOCK) * 0.35,
        )
    if pullback >= MIN_DIP_PULLBACK and close >= sma20 * 0.985 and momentum_20 > -0.03 and (clv >= 0.45 or momentum_5 > 0.0):
        return _target_signal(
            action="accumulate_lowfreq_starter",
            phase="starter",
            regime=regime,
            target_percent=PROBE_TARGET,
            target_delta_percent=PROBE_ADD,
            reason="samsung_lowfreq_starter_recovery_gate",
            dynamic_gate="low_noise_starter_recovery",
            overheat_status=overheat_status,
            score=0.30 + min(max(momentum_5, 0.0) * 1.5, 0.10),
        )
    if pullback >= MIN_DIP_PULLBACK and close >= sma60 and close >= sma120 and momentum_20 >= 0.04 and momentum_60 >= 0.02 and volatility <= 0.045:
        return _target_signal(
            action="accumulate_lowfreq_core",
            phase="core",
            regime=regime,
            target_percent=CORE_TARGET,
            target_delta_percent=CORE_ADD,
            reason="samsung_lowfreq_core_rebuild",
            dynamic_gate="confirmed_core_rebuild",
            overheat_status=overheat_status,
            score=0.68 + min(momentum_20, 0.15),
        )
    return None


def _target_signal(
    *,
    action: str,
    phase: str,
    regime: str,
    target_percent: float,
    target_delta_percent: float,
    reason: str,
    dynamic_gate: str,
    overheat_status: str,
    score: float,
) -> dict[str, Any]:
    return {
        "action": action,
        "phase": phase,
        "regime": regime,
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "reason": reason,
        "dynamic_gate": dynamic_gate,
        "overheat_status": overheat_status,
        "score": score,
    }


def _overheat_status(features: Mapping[str, float]) -> str:
    close = features["close"]
    sma60 = features["sma60"]
    sma120 = features["sma120"]
    momentum_20 = features["momentum_20"]
    pullback = features["pullback_from_high"]
    extension_from_sma60 = (close / sma60) - 1.0
    extension_from_sma120 = (close / sma120) - 1.0
    if pullback < HIGH_PROXIMITY_PULLBACK and momentum_20 > 0.08:
        return "near_20d_high_no_chase"
    if momentum_20 >= EXTREME_MOMENTUM_20 and pullback < EXTREME_MOMENTUM_PULLBACK_REQUIRED:
        return "extreme_20d_surge_requires_deeper_pullback"
    if momentum_20 >= HOT_MOMENTUM_20 and pullback < HOT_MOMENTUM_PULLBACK_REQUIRED:
        return "hot_20d_surge_requires_pullback"
    if extension_from_sma60 >= OVEREXTENDED_SMA60_GAP and pullback < HOT_MOMENTUM_PULLBACK_REQUIRED:
        return "overextended_from_sma60"
    if extension_from_sma120 >= OVEREXTENDED_SMA120_GAP and pullback < EXTREME_MOMENTUM_PULLBACK_REQUIRED:
        return "overextended_from_sma120"
    return "clear"


def _regime(features: Mapping[str, float]) -> str:
    close = features["close"]
    sma60 = features["sma60"]
    sma120 = features["sma120"]
    momentum_20 = features["momentum_20"]
    momentum_60 = features["momentum_60"]
    pullback = features["pullback_from_high"]
    volatility = features["volatility"]
    if (close < sma120 * 0.96 and momentum_20 <= -0.03) or pullback >= 0.18 or volatility >= 0.08:
        return "risk_off"
    if (close < sma60 * 0.985 and momentum_20 < 0.0) or momentum_20 <= -0.05 or momentum_60 <= -0.08:
        return "weak"
    if close >= sma60 and close >= sma120 * 0.985 and momentum_20 >= -0.01:
        return "risk_on"
    return "neutral"


def _falling_knife(
    *,
    close: float,
    sma120: float,
    momentum_20: float,
    pullback: float,
    volatility: float,
) -> bool:
    return close < sma120 * 0.90 or momentum_20 <= -0.095 or pullback >= 0.22 or volatility >= HARD_VOLATILITY_BLOCK


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_10_close"))
    sma20 = _first_value(context, symbol_key, ("sma_20_close",))
    sma60 = _first_value(context, symbol_key, ("sma_60_close", "sma_50_close", "sma_20_close"))
    sma120 = _first_value(context, symbol_key, ("sma_120_close", "sma_60_close", "sma_50_close", "sma_20_close"))
    momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close", "momentum_60_close"))
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
    rolling_low = _first_value(context, symbol_key, ("rolling_min_20_close",))
    zscore_20 = _first_value(context, symbol_key, ("zscore_20_close",))
    drawdown_20 = _first_value(context, symbol_key, ("drawdown_20_close",))
    bar_return_1 = _first_value(context, symbol_key, ("return_1_close", "bar_return_1_close"))
    clv = _first_value(context, symbol_key, ("close_location_value", "clv"))
    if (
        close is None
        or fast_average is None
        or sma20 is None
        or sma60 is None
        or sma120 is None
        or momentum_20 is None
        or rolling_high is None
        or rolling_low is None
        or close <= 0
        or fast_average <= 0
        or sma20 <= 0
        or sma60 <= 0
        or sma120 <= 0
        or rolling_high <= 0
        or rolling_low <= 0
    ):
        return None
    momentum_5 = momentum_5 or 0.0
    momentum_60 = momentum_60 if momentum_60 is not None else momentum_20
    pullback = max((rolling_high - close) / rolling_high, 0.0)
    drawdown_20 = drawdown_20 if drawdown_20 is not None else -pullback
    zscore_20 = zscore_20 if zscore_20 is not None else 0.0
    bar_return_1 = bar_return_1 if bar_return_1 is not None else 0.0
    clv = clv if clv is not None else 0.0
    near_low = max((close / rolling_low) - 1.0, 0.0)
    volatility = _normalized_volatility(context, symbol_key, close)
    if _has_implausible_feature(momentum_5, momentum_20, momentum_60, pullback, near_low, volatility):
        return None
    extension_from_sma60 = (close / sma60) - 1.0
    extension_from_sma120 = (close / sma120) - 1.0
    return {
        "close": close,
        "fast_average": fast_average,
        "sma20": sma20,
        "sma60": sma60,
        "sma120": sma120,
        "momentum_5": momentum_5,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60,
        "pullback_from_high": pullback,
        "drawdown_20": drawdown_20,
        "near_rolling_low_pct": near_low,
        "zscore_20": zscore_20,
        "bar_return_1": bar_return_1,
        "close_location_value": clv,
        "volatility": volatility,
        "extension_from_sma60": extension_from_sma60,
        "extension_from_sma120": extension_from_sma120,
    }


def _normalized_volatility(context: SnapshotContext, symbol_key: str, close: float) -> float:
    values = []
    stddev = _first_value(context, symbol_key, ("stddev_20_close",))
    atr = _first_value(context, symbol_key, ("atr_14",))
    if stddev is not None:
        values.append(stddev / close)
    if atr is not None:
        values.append(atr / close)
    return max(values) if values else 0.0


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _has_implausible_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_FEATURE_ABS for value in values)

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_ID = "semiconduct-kor-samsung-steward"
VERSION = "0.4.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=5)
SAMSUNG_SYMBOL_KEY = "KRX:005930"
STATE_NAMESPACE = "steward"

FULL_HOLD_TARGET = 1.0
REBUILD_TARGET = 0.9
LIGHT_DEFENSE_TARGET = 0.85
DEFENSIVE_TRIM_TARGET = 0.65
HEAVY_DEFENSE_TARGET = 0.35
RISK_OFF_TARGET = 0.0

MILD_DIP_ADD = 0.10
STANDARD_DIP_ADD = 0.15
DEEP_DIP_ADD = 0.20
MILD_DIP_MAX_TARGET = 0.85
STANDARD_DIP_MAX_TARGET = 0.9
DEEP_DIP_MAX_TARGET = 0.95
REBUILD_ADD = 0.15
CAPITULATION_ADD = 0.05
CAPITULATION_MAX_TARGET = 0.45
CAPITULATION_TRIGGER_PULLBACK = 0.065
CAPITULATION_STOP_PULLBACK = 0.13
REENTRY_PROBE_ADD = 0.25
REENTRY_RECLAIM_ADD = 0.20
REENTRY_REBUILD_ADD = 0.20
REENTRY_CORE_ADD = 0.15
REENTRY_PROBE_MAX_TARGET = 0.25
REENTRY_RECLAIM_MAX_TARGET = 0.45
REENTRY_REBUILD_MAX_TARGET = 0.65
REENTRY_CORE_MAX_TARGET = 0.85

ACCUMULATION_COOLDOWN_DAYS = 3
MAX_PLAUSIBLE_DAILY_FEATURE_ABS = 3.0


def generate(context: SnapshotContext) -> list[Insight]:
    if SAMSUNG_SYMBOL_KEY not in context.symbol_keys:
        return []
    item = _features(context, SAMSUNG_SYMBOL_KEY)
    if item is None:
        return []

    signal = _signal(context, item)
    return [_insight(context, item, signal)]


def state_patches(context: SnapshotContext, insights: tuple[Insight, ...] = ()) -> tuple[StatePatch, ...]:
    patches: list[StatePatch] = []
    for insight in insights:
        if insight.symbol_key != SAMSUNG_SYMBOL_KEY:
            continue
        metadata = dict(insight.metadata)
        action = str(metadata.get("action") or "")
        value: dict[str, Any] = {
            "last_signal_at": context.as_of.isoformat(),
            "last_action": action,
            "last_reason": insight.reason,
            "last_regime": metadata.get("regime"),
            "last_target_percent": metadata.get("target_percent"),
            "last_target_delta_percent": metadata.get("target_delta_percent"),
            "last_close": metadata.get("close"),
        }
        if action.startswith("accumulate"):
            value["last_accumulation_at"] = context.as_of.isoformat()
        patches.append(
            StatePatch(
                key=context.model_state.key(
                    model_id=ALPHA_ID,
                    namespace=STATE_NAMESPACE,
                    symbol_key=insight.symbol_key,
                ),
                value=value,
                reason="samsung_steward_signal_mark",
            )
        )
    return tuple(patches)


def _insight(
    context: SnapshotContext,
    item: dict[str, float | str],
    signal: dict[str, Any],
) -> Insight:
    target_percent = float(signal["target_percent"])
    target_delta_percent = float(signal.get("target_delta_percent") or 0.0)
    score = float(signal["score"])
    metadata = {
        "role": "samsung_core_steward",
        "strategy_mode": "v3_recovery_dca",
        "action": signal["action"],
        "phase": signal["phase"],
        "regime": signal["regime"],
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "max_target_percent": float(signal.get("max_target_percent") or target_percent),
        "cooldown_active": bool(signal.get("cooldown_active") or False),
        "close": float(item["close"]),
        "fast_average": float(item["fast_average"]),
        "sma20": float(item["sma20"]),
        "sma60": float(item["sma60"]),
        "sma120": float(item["sma120"]),
        "momentum_5": float(item["momentum_5"]),
        "momentum": float(item["momentum_20"]),
        "momentum_60": float(item["momentum_60"]),
        "trend_strength": float(item["trend_strength"]),
        "pullback_from_high": float(item["pullback_from_high"]),
        "drawdown_20": float(item["drawdown_20"]),
        "rolling_low": float(item["rolling_low"]),
        "near_rolling_low_pct": float(item["near_rolling_low_pct"]),
        "zscore_20": float(item["zscore_20"]),
        "bar_return_1": float(item["bar_return_1"]),
        "close_location_value": float(item["close_location_value"]),
        "rolling_high": float(item["rolling_high"]),
        "volatility": float(item["volatility"]),
    }
    for name in (
        "base_target_percent",
        "capitulation_trigger_price",
        "capitulation_stop_price",
        "capitulation_trigger_pullback",
        "capitulation_stop_pullback",
    ):
        if name in signal:
            metadata[name] = signal[name]
    for name in ("reentry_stage", "reentry_setup", "reentry_heal_score"):
        if name in signal:
            metadata[name] = signal[name]
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(SAMSUNG_SYMBOL_KEY),
        direction=signal["direction"],
        generated_at=context.as_of,
        expires_at=context.as_of + HORIZON,
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=float(item["momentum_20"]),
        confidence=min(0.95, 0.58 + min(abs(score), 1.0) * 0.28 + float(signal.get("confidence_bonus") or 0.0)),
        weight=target_percent,
        score=score,
        group_id="krw-semiconductor-core",
        reason=str(signal["reason"]),
        metadata=metadata,
    )


def _signal(context: SnapshotContext, item: dict[str, float | str]) -> dict[str, Any]:
    close = float(item["close"])
    fast_average = float(item["fast_average"])
    sma20 = float(item["sma20"])
    sma60 = float(item["sma60"])
    sma120 = float(item["sma120"])
    momentum_5 = float(item["momentum_5"])
    momentum_20 = float(item["momentum_20"])
    momentum_60 = float(item["momentum_60"])
    pullback = float(item["pullback_from_high"])
    drawdown_20 = float(item["drawdown_20"])
    zscore_20 = float(item["zscore_20"])
    bar_return_1 = float(item["bar_return_1"])
    clv = float(item["close_location_value"])
    volatility = float(item["volatility"])
    near_low = float(item["near_rolling_low_pct"])

    regime = _regime(item)
    cooldown_active = _cooldown_active(context, SAMSUNG_SYMBOL_KEY)

    hard_risk = (
        (close < sma120 * 0.94 and momentum_20 <= -0.07)
        or pullback >= 0.22
        or (volatility >= 0.09 and momentum_20 < 0.0)
    )
    if hard_risk:
        return _target_signal(
            direction=InsightDirection.FLAT,
            action="risk_off_exit",
            phase="risk_off",
            regime=regime,
            target_percent=RISK_OFF_TARGET,
            reason="samsung_steward_risk_off_exit",
            score=-1.0 - pullback,
            confidence_bonus=0.08,
            cooldown_active=cooldown_active,
        )

    capitulation_signal = _capitulation_signal(
        regime=regime,
        close=close,
        sma120=sma120,
        momentum_20=momentum_20,
        pullback=pullback,
        drawdown_20=drawdown_20,
        zscore_20=zscore_20,
        near_low=near_low,
        volatility=volatility,
        cooldown_active=cooldown_active,
    )
    if capitulation_signal is not None:
        return capitulation_signal

    heavy_defense = regime == "risk_off" or drawdown_20 <= -0.16 or close < sma120 * 0.965
    if heavy_defense:
        return _target_signal(
            direction=InsightDirection.FLAT,
            action="heavy_defense_trim",
            phase="defense",
            regime=regime,
            target_percent=HEAVY_DEFENSE_TARGET,
            reason="samsung_steward_heavy_defense_trim",
            score=-0.65 - abs(drawdown_20),
            confidence_bonus=0.05,
            cooldown_active=cooldown_active,
        )

    reserve_defense = (
        regime == "weak"
        or (close < sma60 * 0.985 and momentum_20 < 0.0)
        or momentum_20 <= -0.05
        or (pullback >= 0.13 and momentum_5 < 0.0)
    )
    if reserve_defense:
        return _target_signal(
            direction=InsightDirection.FLAT,
            action="cash_reserve_trim",
            phase="defense",
            regime=regime,
            target_percent=DEFENSIVE_TRIM_TARGET,
            reason="samsung_steward_cash_reserve_trim",
            score=-0.42 - abs(min(momentum_20, 0.0)),
            confidence_bonus=0.04,
            cooldown_active=cooldown_active,
        )

    dip_signal = _dip_signal(
        regime=regime,
        zscore_20=zscore_20,
        drawdown_20=drawdown_20,
        near_low=near_low,
        bar_return_1=bar_return_1,
        close_location_value=clv,
        momentum_20=momentum_20,
        cooldown_active=cooldown_active,
    )
    if dip_signal is not None:
        return dip_signal

    light_defense = close < sma20 and (fast_average < sma20 or momentum_5 < 0.0)
    if light_defense:
        return _target_signal(
            direction=InsightDirection.FLAT,
            action="light_defense_trim",
            phase="defense",
            regime=regime,
            target_percent=LIGHT_DEFENSE_TARGET,
            reason="samsung_steward_light_defense_trim",
            score=-0.22 - abs(min(momentum_5, 0.0)),
            confidence_bonus=0.02,
            cooldown_active=cooldown_active,
        )

    cooldown_signal = _accumulation_cooldown_signal(
        context,
        symbol_key=SAMSUNG_SYMBOL_KEY,
        regime=regime,
        cooldown_active=cooldown_active,
    )
    if cooldown_signal is not None:
        return cooldown_signal

    reentry_signal = _reentry_signal(
        regime=regime,
        close=close,
        fast_average=fast_average,
        sma20=sma20,
        sma60=sma60,
        sma120=sma120,
        momentum_5=momentum_5,
        momentum_20=momentum_20,
        momentum_60=momentum_60,
        pullback=pullback,
        drawdown_20=drawdown_20,
        zscore_20=zscore_20,
        bar_return_1=bar_return_1,
        close_location_value=clv,
        volatility=volatility,
        near_low=near_low,
        cooldown_active=cooldown_active,
    )
    if reentry_signal is not None:
        return reentry_signal

    previous_target = _state_float(context, SAMSUNG_SYMBOL_KEY, "last_target_percent")
    constructive_reclaim = close >= sma20 and fast_average >= sma20 and momentum_5 > 0.0 and momentum_20 > -0.015
    if previous_target is not None and previous_target < REBUILD_TARGET and constructive_reclaim:
        return _target_signal(
            direction=InsightDirection.UP,
            action="rebuild_after_defense",
            phase="rebuild",
            regime=regime,
            target_percent=REBUILD_TARGET,
            target_delta_percent=REBUILD_ADD,
            max_target_percent=REBUILD_TARGET,
            reason="samsung_steward_rebuild_after_defense",
            score=0.35 + max(momentum_5, 0.0),
            confidence_bonus=0.03,
            cooldown_active=cooldown_active,
        )

    return _target_signal(
        direction=InsightDirection.UP,
        action="core_hold",
        phase="core",
        regime=regime,
        target_percent=FULL_HOLD_TARGET,
        reason="samsung_steward_core_hold",
        score=_hold_score(item),
        cooldown_active=cooldown_active,
    )


def _accumulation_cooldown_signal(
    context: SnapshotContext,
    *,
    symbol_key: str,
    regime: str,
    cooldown_active: bool,
) -> dict[str, Any] | None:
    if not cooldown_active:
        return None
    previous_action = str(_state_value(context, symbol_key, "last_action") or "")
    if not previous_action.startswith("accumulate"):
        return None
    previous_target = _state_float(context, symbol_key, "last_target_percent")
    if previous_target is None:
        return None
    return _target_signal(
        direction=InsightDirection.UP,
        action="accumulation_cooldown_hold",
        phase="cooldown",
        regime=regime,
        target_percent=_clamp(previous_target, 0.0, FULL_HOLD_TARGET),
        reason="samsung_steward_accumulation_cooldown_hold",
        score=0.12,
        confidence_bonus=0.01,
        cooldown_active=cooldown_active,
    )


def _reentry_signal(
    *,
    regime: str,
    close: float,
    fast_average: float,
    sma20: float,
    sma60: float,
    sma120: float,
    momentum_5: float,
    momentum_20: float,
    momentum_60: float,
    pullback: float,
    drawdown_20: float,
    zscore_20: float,
    bar_return_1: float,
    close_location_value: float,
    volatility: float,
    near_low: float,
    cooldown_active: bool,
) -> dict[str, Any] | None:
    if cooldown_active or regime not in {"neutral", "risk_on"}:
        return None

    fully_healed = (
        close >= sma60
        and close >= sma120
        and momentum_20 >= 0.04
        and momentum_60 >= 0.02
        and pullback <= 0.06
        and volatility <= 0.045
    )
    if fully_healed:
        return None

    stress_remains = (
        pullback >= 0.06
        or volatility >= 0.045
        or close < sma60
        or momentum_20 < 0.035
        or drawdown_20 <= -0.055
        or zscore_20 < 0.25
    )
    if not stress_remains:
        return None

    stabilization = (
        (bar_return_1 > 0.0 or close_location_value >= 0.35)
        and close >= sma120 * 0.93
        and momentum_20 > -0.055
        and drawdown_20 > -0.17
        and near_low >= 0.018
    )
    if not stabilization:
        return None
    if close < fast_average * 0.985 and momentum_5 < 0.0:
        return None

    heal_score = (
        max(momentum_5, 0.0) * 1.8
        + max(momentum_20, 0.0)
        + max((close / max(sma20, 1.0)) - 1.0, 0.0)
        - min(volatility, 0.15) * 0.35
    )
    if close >= sma60 * 0.995 and momentum_20 > 0.015 and volatility <= 0.055:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_reentry_rebuild",
            phase="reentry",
            regime=regime,
            target_percent=REENTRY_REBUILD_MAX_TARGET,
            target_delta_percent=REENTRY_REBUILD_ADD,
            max_target_percent=REENTRY_REBUILD_MAX_TARGET,
            reason="samsung_steward_reentry_rebuild",
            score=0.50 + heal_score,
            confidence_bonus=0.04,
            cooldown_active=cooldown_active,
            extra_metadata={
                "reentry_stage": "rebuild",
                "reentry_setup": "sma60_reclaim_with_positive_momentum",
                "reentry_heal_score": heal_score,
            },
        )
    if close >= sma20 and fast_average >= sma20 * 0.99 and momentum_5 > 0.0:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_reentry_reclaim",
            phase="reentry",
            regime=regime,
            target_percent=REENTRY_RECLAIM_MAX_TARGET,
            target_delta_percent=REENTRY_RECLAIM_ADD,
            max_target_percent=REENTRY_RECLAIM_MAX_TARGET,
            reason="samsung_steward_reentry_reclaim",
            score=0.42 + heal_score,
            confidence_bonus=0.03,
            cooldown_active=cooldown_active,
            extra_metadata={
                "reentry_stage": "reclaim",
                "reentry_setup": "sma20_reclaim_with_positive_short_momentum",
                "reentry_heal_score": heal_score,
            },
        )
    if close >= sma20 * 0.985 or momentum_5 > 0.0 or close_location_value >= 0.5:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_reentry_probe",
            phase="reentry",
            regime=regime,
            target_percent=REENTRY_PROBE_MAX_TARGET,
            target_delta_percent=REENTRY_PROBE_ADD,
            max_target_percent=REENTRY_PROBE_MAX_TARGET,
            reason="samsung_steward_reentry_probe",
            score=0.30 + heal_score,
            confidence_bonus=0.02,
            cooldown_active=cooldown_active,
            extra_metadata={
                "reentry_stage": "probe",
                "reentry_setup": "post_stress_stabilization",
                "reentry_heal_score": heal_score,
            },
        )
    return None


def _dip_signal(
    *,
    regime: str,
    zscore_20: float,
    drawdown_20: float,
    near_low: float,
    bar_return_1: float,
    close_location_value: float,
    momentum_20: float,
    cooldown_active: bool,
) -> dict[str, Any] | None:
    if cooldown_active or regime not in {"risk_on", "neutral"}:
        return None
    rebound_confirmed = bar_return_1 > 0.0 or close_location_value >= 0.25
    if not rebound_confirmed:
        return None
    if (zscore_20 <= -2.25 or drawdown_20 <= -0.12) and momentum_20 > -0.04:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_deep_dip",
            phase="accumulation",
            regime=regime,
            target_percent=DEEP_DIP_MAX_TARGET,
            target_delta_percent=DEEP_DIP_ADD,
            max_target_percent=DEEP_DIP_MAX_TARGET,
            reason="samsung_steward_deep_dip_accumulate",
            score=0.75 + min(abs(zscore_20) / 4.0, 0.4),
            confidence_bonus=0.06,
            cooldown_active=cooldown_active,
        )
    if zscore_20 <= -1.6 or near_low <= 0.025:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_standard_dip",
            phase="accumulation",
            regime=regime,
            target_percent=STANDARD_DIP_MAX_TARGET,
            target_delta_percent=STANDARD_DIP_ADD,
            max_target_percent=STANDARD_DIP_MAX_TARGET,
            reason="samsung_steward_standard_dip_accumulate",
            score=0.55 + min(abs(zscore_20) / 5.0, 0.3),
            confidence_bonus=0.04,
            cooldown_active=cooldown_active,
        )
    if zscore_20 <= -1.0 and momentum_20 > -0.025:
        return _target_signal(
            direction=InsightDirection.UP,
            action="accumulate_mild_dip",
            phase="accumulation",
            regime=regime,
            target_percent=MILD_DIP_MAX_TARGET,
            target_delta_percent=MILD_DIP_ADD,
            max_target_percent=MILD_DIP_MAX_TARGET,
            reason="samsung_steward_mild_dip_accumulate",
            score=0.38 + min(abs(zscore_20) / 6.0, 0.2),
            confidence_bonus=0.02,
            cooldown_active=cooldown_active,
        )
    return None


def _capitulation_signal(
    *,
    regime: str,
    close: float,
    sma120: float,
    momentum_20: float,
    pullback: float,
    drawdown_20: float,
    zscore_20: float,
    near_low: float,
    volatility: float,
    cooldown_active: bool,
) -> dict[str, Any] | None:
    if cooldown_active or regime != "risk_off":
        return None
    if close < sma120 * 0.90 or momentum_20 <= -0.095 or pullback >= 0.22 or volatility >= 0.10:
        return None
    capitulation_setup = (
        close < sma120 * 0.985
        or drawdown_20 <= -0.10
        or zscore_20 <= -1.2
        or near_low <= 0.035
        or pullback >= 0.12
        or (volatility >= 0.08 and momentum_20 > 0.02)
    )
    if not capitulation_setup:
        return None
    return _target_signal(
        direction=InsightDirection.FLAT,
        action="risk_capitulation_accumulate",
        phase="capitulation",
        regime=regime,
        target_percent=HEAVY_DEFENSE_TARGET,
        target_delta_percent=CAPITULATION_ADD,
        max_target_percent=CAPITULATION_MAX_TARGET,
        reason="samsung_steward_risk_capitulation_accumulate",
        score=0.30 + min(abs(zscore_20) / 8.0, 0.22) + min(pullback, 0.20),
        confidence_bonus=0.02,
        cooldown_active=cooldown_active,
        extra_metadata={
            "base_target_percent": HEAVY_DEFENSE_TARGET,
            "capitulation_trigger_price": close * (1.0 - CAPITULATION_TRIGGER_PULLBACK),
            "capitulation_stop_price": close * (1.0 - CAPITULATION_STOP_PULLBACK),
            "capitulation_trigger_pullback": CAPITULATION_TRIGGER_PULLBACK,
            "capitulation_stop_pullback": CAPITULATION_STOP_PULLBACK,
        },
    )


def _target_signal(
    *,
    direction: InsightDirection,
    action: str,
    phase: str,
    regime: str,
    target_percent: float,
    reason: str,
    score: float,
    target_delta_percent: float = 0.0,
    max_target_percent: float | None = None,
    confidence_bonus: float = 0.0,
    cooldown_active: bool = False,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal = {
        "direction": direction,
        "action": action,
        "phase": phase,
        "regime": regime,
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "max_target_percent": max_target_percent if max_target_percent is not None else target_percent,
        "reason": reason,
        "score": score,
        "confidence_bonus": confidence_bonus,
        "cooldown_active": cooldown_active,
    }
    if extra_metadata:
        signal.update(extra_metadata)
    return signal


def _regime(item: dict[str, float | str]) -> str:
    close = float(item["close"])
    sma60 = float(item["sma60"])
    sma120 = float(item["sma120"])
    momentum_20 = float(item["momentum_20"])
    momentum_60 = float(item["momentum_60"])
    pullback = float(item["pullback_from_high"])
    volatility = float(item["volatility"])

    if (close < sma120 * 0.96 and momentum_20 <= -0.03) or pullback >= 0.18 or volatility >= 0.08:
        return "risk_off"
    if (close < sma60 * 0.985 and momentum_20 < 0.0) or momentum_20 <= -0.05 or momentum_60 <= -0.08:
        return "weak"
    if close >= sma60 and close >= sma120 * 0.985 and momentum_20 >= -0.01:
        return "risk_on"
    return "neutral"


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float | str] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_10_close"))
    sma20 = _first_value(context, symbol_key, ("sma_20_close",))
    sma60 = _first_value(context, symbol_key, ("sma_60_close", "sma_50_close", "sma_20_close"))
    sma120 = _first_value(context, symbol_key, ("sma_120_close", "sma_60_close", "sma_50_close", "sma_20_close"))
    momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close",))
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
    rolling_low = _first_value(context, symbol_key, ("rolling_min_20_close",))
    zscore_20 = _first_value(context, symbol_key, ("zscore_20_close",))
    drawdown_20 = _first_value(context, symbol_key, ("drawdown_20_close",))
    bar_return_1 = _first_value(context, symbol_key, ("return_1_close", "bar_return_1_close"))
    close_location_value = _first_value(context, symbol_key, ("close_location_value", "clv"))
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
    trend_strength = (close / sma20) - 1.0
    pullback_from_high = max((rolling_high - close) / rolling_high, 0.0)
    if drawdown_20 is not None and drawdown_20 < 0:
        pullback_from_high = max(pullback_from_high, abs(drawdown_20))
    near_rolling_low_pct = max((close / rolling_low) - 1.0, 0.0)
    volatility = _normalized_volatility(context, symbol_key, close)
    if _has_implausible_daily_feature(momentum_5, momentum_20, momentum_60, trend_strength, zscore_20):
        return None
    return {
        "symbol_key": symbol_key,
        "close": close,
        "fast_average": fast_average,
        "sma20": sma20,
        "sma60": sma60,
        "sma120": sma120,
        "momentum_5": momentum_5 or 0.0,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60 if momentum_60 is not None else momentum_20,
        "trend_strength": trend_strength,
        "rolling_high": rolling_high,
        "rolling_low": rolling_low,
        "pullback_from_high": pullback_from_high,
        "drawdown_20": drawdown_20 if drawdown_20 is not None else -pullback_from_high,
        "near_rolling_low_pct": near_rolling_low_pct,
        "zscore_20": zscore_20 if zscore_20 is not None else 0.0,
        "bar_return_1": bar_return_1 if bar_return_1 is not None else 0.0,
        "close_location_value": close_location_value if close_location_value is not None else 0.0,
        "volatility": volatility,
    }


def _hold_score(item: dict[str, float | str]) -> float:
    return (
        float(item["momentum_20"]) * 0.55
        + float(item["momentum_5"]) * 0.25
        + max(float(item["trend_strength"]), 0.0) * 0.20
        - min(float(item["volatility"]), 0.35) * 0.25
    )


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


def _cooldown_active(context: SnapshotContext, symbol_key: str) -> bool:
    last_accumulation = _state_datetime(context, symbol_key, "last_accumulation_at")
    if last_accumulation is None:
        return False
    return (context.as_of.date() - last_accumulation.date()).days < ACCUMULATION_COOLDOWN_DAYS


def _state_float(context: SnapshotContext, symbol_key: str, name: str) -> float | None:
    value = _state_value(context, symbol_key, name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _state_datetime(context: SnapshotContext, symbol_key: str, name: str) -> datetime | None:
    value = _state_value(context, symbol_key, name)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _state_value(context: SnapshotContext, symbol_key: str, name: str) -> Any:
    record = context.model_state.get(
        model_id=ALPHA_ID,
        namespace=STATE_NAMESPACE,
        symbol_key=symbol_key,
    )
    if record is None:
        return None
    return record.value.get(name)


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _has_implausible_daily_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_DAILY_FEATURE_ABS for value in values)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

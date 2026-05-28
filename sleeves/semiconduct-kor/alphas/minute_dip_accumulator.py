from __future__ import annotations

from datetime import timedelta
from typing import Any, Mapping

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_ID = "semiconduct-kor-minute-dip-accumulator"
VERSION = "0.1.1"
EVALUATION_CADENCE = "every_5_minutes"
INPUT_RESOLUTION = "minute"
HORIZON = timedelta(minutes=90)
MEMORY_LEADER_SYMBOL_KEYS = ("KRX:005930", "KRX:000660")
STATE_NAMESPACE = "minute_dip_accumulator"

PROBE_TARGET = 0.15
OVERHEAT_PROBE_TARGET = 0.10
RECLAIM_TARGET = 0.25
PROBE_ADD = 0.10
RECLAIM_ADD = 0.10

BASE_PULLBACK = 0.012
HOT_PULLBACK = 0.022
EXTREME_PULLBACK = 0.032
FALLING_KNIFE_15M = -0.038
MAX_INTRADAY_VOLATILITY = 0.065


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []
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
                    "last_target_percent": metadata.get("target_percent"),
                    "last_target_delta_percent": metadata.get("target_delta_percent"),
                    "last_close": metadata.get("minute_close"),
                    "last_pullback_30m": metadata.get("minute_pullback_30m"),
                    "last_daily_heat": metadata.get("daily_heat"),
                },
                reason="minute_dip_accumulator_signal_mark",
            )
        )
    return tuple(patches)


def _insight(
    context: SnapshotContext,
    symbol_key: str,
    features: Mapping[str, float | str],
    signal: Mapping[str, Any],
) -> Insight:
    target_percent = float(signal["target_percent"])
    score = float(signal["score"])
    metadata = {
        "role": "memory_leader_minute_dip_accumulator",
        "strategy_mode": "minute_pullback_buy_only",
        "action": signal["action"],
        "phase": signal["phase"],
        "target_percent": target_percent,
        "target_delta_percent": float(signal["target_delta_percent"]),
        "max_target_percent": target_percent,
        "dynamic_gate": signal["dynamic_gate"],
        "daily_heat": features["daily_heat"],
        "required_pullback": signal["required_pullback"],
        "minute_close": features["minute_close"],
        "minute_sma_5": features["minute_sma_5"],
        "minute_sma_20": features["minute_sma_20"],
        "minute_vwap_20": features["minute_vwap_20"],
        "minute_roc_5": features["minute_roc_5"],
        "minute_roc_15": features["minute_roc_15"],
        "minute_pullback_30m": features["minute_pullback_30m"],
        "minute_rebound_from_low_30m": features["minute_rebound_from_low_30m"],
        "minute_return_1": features["minute_return_1"],
        "minute_close_location_value": features["minute_close_location_value"],
        "daily_pullback_20": features["daily_pullback_20"],
        "daily_momentum_20": features["daily_momentum_20"],
        "daily_extension_from_sma60": features["daily_extension_from_sma60"],
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
        magnitude=max(float(features["minute_rebound_from_low_30m"]), 0.0),
        confidence=min(0.88, 0.52 + score * 0.22),
        weight=target_percent,
        score=score,
        group_id="krw-memory-leader-minute-dip",
        reason=str(signal["reason"]),
        metadata=metadata,
    )


def _signal(features: Mapping[str, float | str]) -> dict[str, Any] | None:
    close = float(features["minute_close"])
    sma5 = float(features["minute_sma_5"])
    sma20 = float(features["minute_sma_20"])
    vwap20 = float(features["minute_vwap_20"])
    roc5 = float(features["minute_roc_5"])
    roc15 = float(features["minute_roc_15"])
    pullback = float(features["minute_pullback_30m"])
    rebound = float(features["minute_rebound_from_low_30m"])
    ret1 = float(features["minute_return_1"])
    clv = float(features["minute_close_location_value"])
    daily_heat = str(features["daily_heat"])
    required_pullback = _required_intraday_pullback(daily_heat)

    if pullback < required_pullback:
        return None
    if roc15 <= FALLING_KNIFE_15M and rebound < 0.006:
        return None
    if pullback >= MAX_INTRADAY_VOLATILITY and roc5 < 0:
        return None

    stabilized = rebound >= 0.004 and (ret1 > 0 or roc5 > -0.004 or clv >= -0.15)
    reclaimed = close >= sma5 and (roc5 > 0 or ret1 > 0) and clv >= -0.05
    vwap_reclaim = close >= vwap20 and roc5 >= 0 and clv >= 0

    if not (stabilized or reclaimed or vwap_reclaim):
        return None

    if vwap_reclaim and pullback >= required_pullback + 0.006:
        return _target_signal(
            action="accumulate_minute_vwap_reclaim",
            phase="reclaim",
            target_percent=RECLAIM_TARGET,
            target_delta_percent=RECLAIM_ADD,
            reason="minute_dip_vwap_reclaim_after_pullback",
            dynamic_gate="intraday_pullback_vwap_reclaim",
            required_pullback=required_pullback,
            score=0.58 + min(rebound * 8.0, 0.16) + min(max(roc5, 0.0) * 4.0, 0.10),
        )
    target = OVERHEAT_PROBE_TARGET if daily_heat != "clear" else PROBE_TARGET
    return _target_signal(
        action="accumulate_minute_dip_probe",
        phase="probe",
        target_percent=target,
        target_delta_percent=PROBE_ADD,
        reason="minute_dip_stabilized_after_pullback",
        dynamic_gate="intraday_pullback_stabilization",
        required_pullback=required_pullback,
        score=0.42 + min(rebound * 7.0, 0.14) + min(max(ret1, 0.0) * 10.0, 0.08),
    )


def _target_signal(
    *,
    action: str,
    phase: str,
    target_percent: float,
    target_delta_percent: float,
    reason: str,
    dynamic_gate: str,
    required_pullback: float,
    score: float,
) -> dict[str, Any]:
    return {
        "action": action,
        "phase": phase,
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "reason": reason,
        "dynamic_gate": dynamic_gate,
        "required_pullback": required_pullback,
        "score": score,
    }


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float | str] | None:
    minute_close = _first_value(context, symbol_key, ("minute_close",))
    minute_sma_5 = _first_value(context, symbol_key, ("minute_sma_5_close",))
    minute_sma_20 = _first_value(context, symbol_key, ("minute_sma_20_close",))
    minute_roc_5 = _first_value(context, symbol_key, ("minute_roc_5_close",))
    minute_roc_15 = _first_value(context, symbol_key, ("minute_roc_15_close",))
    minute_high_30 = _first_value(context, symbol_key, ("minute_rolling_max_30_close",))
    minute_low_30 = _first_value(context, symbol_key, ("minute_rolling_min_30_close",))
    minute_return_1 = _first_value(context, symbol_key, ("minute_return_1_close",))
    minute_clv = _first_value(context, symbol_key, ("minute_close_location_value",))
    minute_vwap_20 = _first_value(context, symbol_key, ("minute_vwap_20", "minute_sma_20_close"))
    daily_close = _first_value(context, symbol_key, ("identity_close", "close"))
    daily_high_20 = _first_value(context, symbol_key, ("rolling_max_20_close",))
    daily_momentum_20 = _first_value(context, symbol_key, ("roc_20_close",))
    daily_sma60 = _first_value(context, symbol_key, ("sma_60_close", "sma_50_close", "sma_20_close"))
    if (
        minute_close is None
        or minute_sma_5 is None
        or minute_sma_20 is None
        or minute_roc_5 is None
        or minute_roc_15 is None
        or minute_high_30 is None
        or minute_low_30 is None
        or minute_vwap_20 is None
        or minute_close <= 0
        or minute_sma_5 <= 0
        or minute_sma_20 <= 0
        or minute_high_30 <= 0
        or minute_low_30 <= 0
        or minute_vwap_20 <= 0
    ):
        return None
    minute_return_1 = minute_return_1 if minute_return_1 is not None else 0.0
    minute_clv = minute_clv if minute_clv is not None else 0.0
    daily_pullback_20 = 0.0
    daily_extension_from_sma60 = 0.0
    if daily_close is not None and daily_high_20 is not None and daily_high_20 > 0:
        daily_pullback_20 = max((daily_high_20 - daily_close) / daily_high_20, 0.0)
    if daily_close is not None and daily_sma60 is not None and daily_sma60 > 0:
        daily_extension_from_sma60 = (daily_close / daily_sma60) - 1.0
    daily_momentum_20 = daily_momentum_20 if daily_momentum_20 is not None else 0.0
    return {
        "minute_close": minute_close,
        "minute_sma_5": minute_sma_5,
        "minute_sma_20": minute_sma_20,
        "minute_vwap_20": minute_vwap_20,
        "minute_roc_5": minute_roc_5,
        "minute_roc_15": minute_roc_15,
        "minute_pullback_30m": max((minute_high_30 - minute_close) / minute_high_30, 0.0),
        "minute_rebound_from_low_30m": max((minute_close / minute_low_30) - 1.0, 0.0),
        "minute_return_1": minute_return_1,
        "minute_close_location_value": minute_clv,
        "daily_pullback_20": daily_pullback_20,
        "daily_momentum_20": daily_momentum_20,
        "daily_extension_from_sma60": daily_extension_from_sma60,
        "daily_heat": _daily_heat(
            daily_pullback_20=daily_pullback_20,
            daily_momentum_20=daily_momentum_20,
            daily_extension_from_sma60=daily_extension_from_sma60,
        ),
    }


def _daily_heat(*, daily_pullback_20: float, daily_momentum_20: float, daily_extension_from_sma60: float) -> str:
    if daily_momentum_20 >= 0.28 and daily_pullback_20 < 0.08:
        return "extreme"
    if daily_momentum_20 >= 0.18 and daily_pullback_20 < 0.06:
        return "hot"
    if daily_extension_from_sma60 >= 0.18 and daily_pullback_20 < 0.06:
        return "extended"
    return "clear"


def _required_intraday_pullback(daily_heat: str) -> float:
    if daily_heat == "extreme":
        return EXTREME_PULLBACK
    if daily_heat in {"hot", "extended"}:
        return HOT_PULLBACK
    return BASE_PULLBACK


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None

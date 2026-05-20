from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_ID = "semiconduct-kor-samsung-strike-reentry"
VERSION = "0.1.0"
EVALUATION_CADENCE = "every_cycle"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=2)
SAMSUNG_SYMBOL_KEY = "KRX:005930"
STATE_NAMESPACE = "strike_reentry"
STRIKE_STATE_NAMESPACE = "strike_risk"
STRIKE_MONITOR_MODEL_ID = "semiconduct-kor-samsung-strike-monitor"

PROBE_TARGET = 0.25
RECLAIM_TARGET = 0.45
REBUILD_TARGET = 0.65
CORE_TARGET = 0.85
PROBE_ADD = 0.25
RECLAIM_ADD = 0.20
REBUILD_ADD = 0.20
CORE_ADD = 0.15
COOLDOWN_DAYS = 3
MAX_PLAUSIBLE_FEATURE_ABS = 3.0


def generate(context: SnapshotContext) -> list[Insight]:
    if SAMSUNG_SYMBOL_KEY not in context.symbol_keys:
        return []
    features = _features(context, SAMSUNG_SYMBOL_KEY)
    if features is None:
        return []

    strike = _strike_risk(context, SAMSUNG_SYMBOL_KEY)
    status = strike["status"]
    if status not in {"off_candidate", "off_confirmed"}:
        return []
    if _falling_knife(features):
        return []

    cooldown = _cooldown_signal(context, SAMSUNG_SYMBOL_KEY, status=status, strike=strike, features=features)
    if cooldown is not None:
        return [_insight(context, features, cooldown, strike)]

    signal = _reentry_signal(status=status, features=features)
    if signal is None:
        return []
    return [_insight(context, features, signal, strike)]


def state_patches(context: SnapshotContext, insights: tuple[Insight, ...] = ()) -> tuple[StatePatch, ...]:
    patches: list[StatePatch] = []
    for insight in insights:
        if insight.symbol_key != SAMSUNG_SYMBOL_KEY or insight.alpha_id != ALPHA_ID:
            continue
        metadata = dict(insight.metadata)
        action = str(metadata.get("action") or "")
        value: dict[str, Any] = {
            "last_signal_at": context.as_of.isoformat(),
            "last_action": action,
            "last_reason": insight.reason,
            "last_target_percent": metadata.get("target_percent"),
            "last_strike_risk_status": metadata.get("strike_risk_status"),
            "last_dynamic_gate": metadata.get("dynamic_gate"),
            "last_close": metadata.get("close"),
            "last_near_rolling_low_pct": metadata.get("near_rolling_low_pct"),
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
                reason="samsung_strike_reentry_signal_mark",
            )
        )
    return tuple(patches)


def _insight(
    context: SnapshotContext,
    features: Mapping[str, float],
    signal: Mapping[str, Any],
    strike: Mapping[str, Any],
) -> Insight:
    target_percent = float(signal["target_percent"])
    target_delta_percent = float(signal.get("target_delta_percent") or 0.0)
    score = float(signal["score"])
    metadata = {
        "role": "samsung_strike_risk_buy_only_reentry",
        "strategy_mode": "buy_only_strike_risk_off_dynamic_reentry",
        "action": signal["action"],
        "phase": signal["phase"],
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "max_target_percent": target_percent,
        "dynamic_gate": signal["dynamic_gate"],
        "strike_risk_status": strike["status"],
        "strike_risk_confidence": strike["confidence"],
        "strike_risk_source_count": strike["source_count"],
        "strike_risk_as_of": strike["as_of"],
        "strike_risk_reason": strike["reason"],
        "close": features["close"],
        "fast_average": features["fast_average"],
        "sma20": features["sma20"],
        "sma60": features["sma60"],
        "sma120": features["sma120"],
        "momentum_5": features["momentum_5"],
        "momentum_20": features["momentum_20"],
        "momentum_60": features["momentum_60"],
        "rolling_high": features["rolling_high"],
        "rolling_low": features["rolling_low"],
        "pullback_from_high": features["pullback_from_high"],
        "drawdown_20": features["drawdown_20"],
        "near_rolling_low_pct": features["near_rolling_low_pct"],
        "bar_return_1": features["bar_return_1"],
        "close_location_value": features["close_location_value"],
        "volatility": features["volatility"],
    }
    return Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol(SAMSUNG_SYMBOL_KEY),
        direction=InsightDirection.UP,
        generated_at=context.as_of,
        expires_at=context.as_of + HORIZON,
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        magnitude=max(features["momentum_20"], 0.0),
        confidence=min(0.95, 0.55 + score * 0.22 + min(float(strike["confidence"]), 1.0) * 0.15),
        weight=target_percent,
        score=score,
        group_id="krw-semiconductor-strike-risk-reentry",
        reason=str(signal["reason"]),
        metadata=metadata,
    )


def _reentry_signal(*, status: str, features: Mapping[str, float]) -> dict[str, Any] | None:
    if not _bottom_confirmed(features):
        return None

    close = features["close"]
    fast = features["fast_average"]
    sma20 = features["sma20"]
    sma60 = features["sma60"]
    sma120 = features["sma120"]
    momentum_5 = features["momentum_5"]
    momentum_20 = features["momentum_20"]
    momentum_60 = features["momentum_60"]
    pullback = features["pullback_from_high"]
    volatility = features["volatility"]
    clv = features["close_location_value"]

    if (
        status == "off_confirmed"
        and close >= sma60
        and close >= sma120
        and momentum_20 >= 0.04
        and momentum_60 >= 0.02
        and pullback <= 0.08
        and volatility <= 0.045
    ):
        return _target_signal(
            action="accumulate_strike_core",
            phase="strike_risk_off_confirmed",
            target_percent=CORE_TARGET,
            target_delta_percent=CORE_ADD,
            reason="samsung_strike_risk_off_core_rebuild",
            dynamic_gate="sma60_sma120_reclaim_with_compressed_volatility",
            score=0.76 + min(momentum_20, 0.15),
        )
    if (
        status == "off_confirmed"
        and close >= sma60 * 0.995
        and momentum_20 > 0.015
        and volatility <= 0.06
    ):
        return _target_signal(
            action="accumulate_strike_rebuild",
            phase="strike_risk_off_confirmed",
            target_percent=REBUILD_TARGET,
            target_delta_percent=REBUILD_ADD,
            reason="samsung_strike_risk_off_rebuild",
            dynamic_gate="sma60_reclaim_with_positive_medium_momentum",
            score=0.62 + min(momentum_20, 0.12),
        )
    if (
        status == "off_confirmed"
        and close >= sma20
        and fast >= sma20 * 0.99
        and momentum_5 > 0.0
    ):
        return _target_signal(
            action="accumulate_strike_reclaim",
            phase="strike_risk_off_confirmed",
            target_percent=RECLAIM_TARGET,
            target_delta_percent=RECLAIM_ADD,
            reason="samsung_strike_risk_off_reclaim",
            dynamic_gate="sma20_reclaim_with_positive_short_momentum",
            score=0.52 + min(momentum_5 * 2.0, 0.12),
        )
    if status == "off_candidate" or clv >= 0.45 or momentum_5 > 0.0:
        return _target_signal(
            action="accumulate_strike_probe",
            phase="strike_risk_off_candidate",
            target_percent=PROBE_TARGET,
            target_delta_percent=PROBE_ADD,
            reason="samsung_strike_risk_off_probe",
            dynamic_gate="recent_low_rebound_without_fixed_price_anchor",
            score=0.38 + min(features["near_rolling_low_pct"], 0.08),
        )
    return None


def _target_signal(
    *,
    action: str,
    phase: str,
    target_percent: float,
    target_delta_percent: float,
    reason: str,
    dynamic_gate: str,
    score: float,
) -> dict[str, Any]:
    return {
        "action": action,
        "phase": phase,
        "target_percent": target_percent,
        "target_delta_percent": target_delta_percent,
        "reason": reason,
        "dynamic_gate": dynamic_gate,
        "score": score,
    }


def _falling_knife(features: Mapping[str, float]) -> bool:
    close = features["close"]
    sma20 = features["sma20"]
    momentum_5 = features["momentum_5"]
    momentum_20 = features["momentum_20"]
    near_low = features["near_rolling_low_pct"]
    drawdown = features["drawdown_20"]
    bar_return = features["bar_return_1"]
    clv = features["close_location_value"]
    volatility = features["volatility"]
    no_reversal = bar_return <= 0.0 and clv < 0.35
    return (
        (near_low <= 0.01 and no_reversal)
        or (close < sma20 * 0.94 and momentum_5 < 0.0 and momentum_20 < 0.0)
        or (drawdown <= -0.18 and no_reversal)
        or (volatility >= 0.09 and no_reversal)
    )


def _bottom_confirmed(features: Mapping[str, float]) -> bool:
    close = features["close"]
    fast = features["fast_average"]
    sma20 = features["sma20"]
    near_low = features["near_rolling_low_pct"]
    bar_return = features["bar_return_1"]
    clv = features["close_location_value"]
    momentum_5 = features["momentum_5"]
    momentum_20 = features["momentum_20"]
    rebounded_from_low = near_low >= 0.02
    daily_reversal = bar_return > 0.0 and clv >= 0.35
    short_momentum_repair = momentum_5 > 0.0 and momentum_20 > -0.06
    average_reclaim = close >= sma20 * 0.985 and fast >= sma20 * 0.985
    return (rebounded_from_low and (daily_reversal or short_momentum_repair)) or average_reclaim


def _cooldown_signal(
    context: SnapshotContext,
    symbol_key: str,
    *,
    status: str,
    strike: Mapping[str, Any],
    features: Mapping[str, float],
) -> dict[str, Any] | None:
    last_accumulation = _state_datetime(context, symbol_key, "last_accumulation_at")
    if last_accumulation is None:
        return None
    if (context.as_of.date() - last_accumulation.date()).days >= COOLDOWN_DAYS:
        return None
    previous_target = _state_float(context, symbol_key, "last_target_percent")
    if previous_target is None or previous_target <= 0:
        return None
    if status == "off_candidate" and strike["confidence"] < 0.65:
        return None
    return _target_signal(
        action="strike_reentry_cooldown_hold",
        phase="cooldown",
        target_percent=_clamp(previous_target, 0.0, CORE_TARGET),
        target_delta_percent=0.0,
        reason="samsung_strike_reentry_cooldown_hold",
        dynamic_gate="cooldown_preserves_prior_buy_only_target",
        score=0.18 + min(max(features["momentum_5"], 0.0), 0.08),
    )


def _strike_risk(context: SnapshotContext, symbol_key: str) -> dict[str, Any]:
    metadata = context.metadata(symbol_key)
    state = _strike_risk_from_mapping(metadata)
    if state is None:
        record = context.model_state.get(model_id=ALPHA_ID, namespace=STRIKE_STATE_NAMESPACE, symbol_key=symbol_key)
        if record is not None:
            state = _strike_risk_from_mapping(record.value)
    if state is None:
        record = context.model_state.get(
            model_id=STRIKE_MONITOR_MODEL_ID,
            namespace=STRIKE_STATE_NAMESPACE,
            symbol_key=symbol_key,
        )
        if record is not None:
            state = _strike_risk_from_mapping(record.value)
    if state is None:
        state = {"status": "on", "confidence": 0.0, "source_count": 0, "as_of": "", "reason": "missing_strike_risk_state"}
    return state


def _strike_risk_from_mapping(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_status = (
        payload.get("strike_risk_status")
        or payload.get("strike_risk_state")
        or payload.get("status")
        or payload.get("state")
    )
    status = _normalize_status(raw_status)
    if status is None:
        return None
    return {
        "status": status,
        "confidence": _float(payload.get("strike_risk_confidence") or payload.get("confidence"), default=0.0),
        "source_count": int(_float(payload.get("source_count") or payload.get("strike_risk_source_count"), default=0.0)),
        "as_of": str(payload.get("as_of") or payload.get("strike_risk_as_of") or ""),
        "reason": str(payload.get("reason") or payload.get("strike_risk_reason") or ""),
    }


def _normalize_status(value: Any) -> str | None:
    text = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "risk_on": "on",
        "strike_on": "on",
        "on": "on",
        "freeze": "on",
        "easing": "easing",
        "risk_easing": "easing",
        "off_candidate": "off_candidate",
        "candidate": "off_candidate",
        "risk_off_candidate": "off_candidate",
        "off_confirmed": "off_confirmed",
        "confirmed": "off_confirmed",
        "risk_off_confirmed": "off_confirmed",
        "off": "off_confirmed",
    }
    return aliases.get(text)


def _features(context: SnapshotContext, symbol_key: str) -> dict[str, float] | None:
    close = _first_value(context, symbol_key, ("identity_close", "close"))
    fast_average = _first_value(context, symbol_key, ("ema_8_close", "sma_10_close", "sma_5_close"))
    sma20 = _first_value(context, symbol_key, ("sma_20_close",))
    sma60 = _first_value(context, symbol_key, ("sma_60_close", "sma_50_close", "sma_20_close"))
    sma120 = _first_value(context, symbol_key, ("sma_120_close", "sma_60_close", "sma_50_close", "sma_20_close"))
    momentum_5 = _first_value(context, symbol_key, ("momentum_5_close",))
    momentum_20 = _first_value(context, symbol_key, ("roc_20_close", "momentum_20_close"))
    momentum_60 = _first_value(context, symbol_key, ("roc_60_close",))
    rolling_high = _first_value(context, symbol_key, ("rolling_max_20_close",))
    rolling_low = _first_value(context, symbol_key, ("rolling_min_20_close",))
    drawdown_20 = _first_value(context, symbol_key, ("drawdown_20_close",))
    bar_return_1 = _first_value(context, symbol_key, ("return_1_close", "bar_return_1_close"))
    close_location_value = _first_value(context, symbol_key, ("close_location_value", "clv"))
    if (
        close is None
        or fast_average is None
        or sma20 is None
        or sma60 is None
        or sma120 is None
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
    momentum_20 = momentum_20 or 0.0
    momentum_60 = momentum_60 if momentum_60 is not None else momentum_20
    drawdown_20 = drawdown_20 if drawdown_20 is not None else -max((rolling_high - close) / rolling_high, 0.0)
    bar_return_1 = bar_return_1 if bar_return_1 is not None else 0.0
    close_location_value = close_location_value if close_location_value is not None else 0.0
    pullback = max((rolling_high - close) / rolling_high, abs(min(drawdown_20, 0.0)), 0.0)
    near_low = max((close / rolling_low) - 1.0, 0.0)
    volatility = _normalized_volatility(context, symbol_key, close)
    if _has_implausible_feature(momentum_5, momentum_20, momentum_60, pullback, near_low, volatility):
        return None
    return {
        "close": close,
        "fast_average": fast_average,
        "sma20": sma20,
        "sma60": sma60,
        "sma120": sma120,
        "momentum_5": momentum_5,
        "momentum_20": momentum_20,
        "momentum_60": momentum_60,
        "rolling_high": rolling_high,
        "rolling_low": rolling_low,
        "pullback_from_high": pullback,
        "drawdown_20": drawdown_20,
        "near_rolling_low_pct": near_low,
        "bar_return_1": bar_return_1,
        "close_location_value": close_location_value,
        "volatility": volatility,
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


def _state_float(context: SnapshotContext, symbol_key: str, name: str) -> float | None:
    record = context.model_state.get(model_id=ALPHA_ID, namespace=STATE_NAMESPACE, symbol_key=symbol_key)
    if record is None:
        return None
    return _float(record.value.get(name), default=None)


def _state_datetime(context: SnapshotContext, symbol_key: str, name: str) -> datetime | None:
    record = context.model_state.get(model_id=ALPHA_ID, namespace=STATE_NAMESPACE, symbol_key=symbol_key)
    if record is None:
        return None
    value = record.value.get(name)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _first_value(context: SnapshotContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    for name in names:
        value = context.value(symbol_key, name)
        if value is not None:
            return float(value)
    return None


def _float(value: Any, *, default: float | None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _has_implausible_feature(*values: float | None) -> bool:
    return any(value is not None and abs(value) > MAX_PLAUSIBLE_FEATURE_ABS for value in values)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

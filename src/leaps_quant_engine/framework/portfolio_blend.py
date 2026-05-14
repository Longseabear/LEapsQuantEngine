from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4

from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
    PortfolioTargetBatch,
)
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.runtime_state import (
    ModelStateKey,
    RuntimeModelStateView,
    StatePatch,
    StatePatchOperation,
)


DEFAULT_PORTFOLIO_BLEND_MODEL_ID = "engine-portfolio-blend"
LAST_TARGET_NAMESPACE = "last_target"
ACTIVE_TRANSITION_NAMESPACE = "active_transition"
DEFAULT_BYPASS_TAG_TOKENS = (
    ":flat",
    ":down",
    "stop",
    "urgent",
    "manual",
    "operator",
    "force",
    "risk",
)


@dataclass(frozen=True, slots=True)
class PortfolioBlendPolicy:
    enabled: bool = False
    duration_minutes: float = 0.0
    target_drift_threshold_pct: float = 0.0
    clock: str = "orderable_session"
    missing_target_behavior: str = "drop"
    bypass_target_tag_tokens: tuple[str, ...] = DEFAULT_BYPASS_TAG_TOKENS
    model_id: str = DEFAULT_PORTFOLIO_BLEND_MODEL_ID

    def __post_init__(self) -> None:
        if self.duration_minutes < 0:
            raise ValueError("portfolio blend duration_minutes cannot be negative.")
        if self.target_drift_threshold_pct < 0:
            raise ValueError("portfolio blend target_drift_threshold_pct cannot be negative.")
        clock = str(self.clock or "orderable_session").strip().lower()
        if clock not in {"wall_time", "orderable_session", "regular_session"}:
            raise ValueError(f"Unsupported portfolio blend clock: {self.clock}")
        missing = str(self.missing_target_behavior or "drop").strip().lower()
        if missing not in {"drop", "zero"}:
            raise ValueError("portfolio blend missing_target_behavior must be 'drop' or 'zero'.")
        model_id = str(self.model_id or DEFAULT_PORTFOLIO_BLEND_MODEL_ID).strip()
        if not model_id:
            raise ValueError("portfolio blend model_id cannot be empty.")
        tokens = tuple(
            str(token).strip().lower()
            for token in self.bypass_target_tag_tokens
            if str(token).strip()
        )
        object.__setattr__(self, "clock", clock)
        object.__setattr__(self, "missing_target_behavior", missing)
        object.__setattr__(self, "bypass_target_tag_tokens", tokens)
        object.__setattr__(self, "model_id", model_id)

    @property
    def active(self) -> bool:
        return self.enabled and self.duration_minutes > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "duration_minutes": self.duration_minutes,
            "target_drift_threshold_pct": self.target_drift_threshold_pct,
            "clock": self.clock,
            "missing_target_behavior": self.missing_target_behavior,
            "bypass_target_tag_tokens": list(self.bypass_target_tag_tokens),
            "model_id": self.model_id,
        }


@dataclass(frozen=True, slots=True)
class PortfolioBlendTransition:
    transition_id: str
    sleeve_id: str
    from_weights: Mapping[str, float]
    to_weights: Mapping[str, float]
    to_tags: Mapping[str, str] = field(default_factory=dict)
    started_at: datetime = field(default_factory=datetime.now)
    duration_minutes: float = 0.0
    elapsed_minutes: float = 0.0
    last_progress_at: datetime | None = None
    source_batch_id: str = ""
    reason: str = "target_drift"
    clock: str = "orderable_session"
    target_drift: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_weights", MappingProxyType(_normalized_weights(self.from_weights)))
        object.__setattr__(self, "to_weights", MappingProxyType(_normalized_weights(self.to_weights)))
        object.__setattr__(self, "to_tags", MappingProxyType({str(k): str(v) for k, v in dict(self.to_tags).items()}))

    @property
    def progress(self) -> float:
        if self.duration_minutes <= 0:
            return 1.0
        return max(0.0, min(self.elapsed_minutes / self.duration_minutes, 1.0))

    @property
    def is_complete(self) -> bool:
        return self.progress >= 1.0

    def advance(self, as_of: datetime, *, market_session: MarketSession | None = None) -> "PortfolioBlendTransition":
        last = self.last_progress_at or self.started_at
        delta = _elapsed_minutes(last, as_of) if _should_advance(self.clock, market_session) else 0.0
        return replace(
            self,
            elapsed_minutes=min(self.duration_minutes, max(0.0, self.elapsed_minutes + delta)),
            last_progress_at=as_of,
        )

    def weights_at_progress(self, progress: float | None = None) -> dict[str, float]:
        ratio = self.progress if progress is None else max(0.0, min(float(progress), 1.0))
        keys = set(self.from_weights) | set(self.to_weights)
        return {
            symbol_key: _clamp_weight(
                self.from_weights.get(symbol_key, 0.0)
                + (self.to_weights.get(symbol_key, 0.0) - self.from_weights.get(symbol_key, 0.0)) * ratio
            )
            for symbol_key in keys
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "sleeve_id": self.sleeve_id,
            "from_weights": dict(self.from_weights),
            "to_weights": dict(self.to_weights),
            "to_tags": dict(self.to_tags),
            "started_at": self.started_at.isoformat(),
            "duration_minutes": self.duration_minutes,
            "elapsed_minutes": self.elapsed_minutes,
            "last_progress_at": self.last_progress_at.isoformat() if self.last_progress_at else None,
            "source_batch_id": self.source_batch_id,
            "reason": self.reason,
            "clock": self.clock,
            "target_drift": self.target_drift,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PortfolioBlendTransition":
        return cls(
            transition_id=str(payload.get("transition_id") or ""),
            sleeve_id=str(payload.get("sleeve_id") or ""),
            from_weights=dict(payload.get("from_weights") or {}),
            to_weights=dict(payload.get("to_weights") or {}),
            to_tags=dict(payload.get("to_tags") or {}),
            started_at=_parse_datetime(payload.get("started_at")),
            duration_minutes=float(payload.get("duration_minutes") or 0.0),
            elapsed_minutes=float(payload.get("elapsed_minutes") or 0.0),
            last_progress_at=_optional_datetime(payload.get("last_progress_at")),
            source_batch_id=str(payload.get("source_batch_id") or ""),
            reason=str(payload.get("reason") or "target_drift"),
            clock=str(payload.get("clock") or "orderable_session"),
            target_drift=float(payload.get("target_drift") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class PortfolioBlendDecision:
    targets: tuple[PortfolioAllocationTarget, ...]
    state_patches: tuple[StatePatch, ...] = ()
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class PortfolioBlendEngine:
    policy: PortfolioBlendPolicy = field(default_factory=PortfolioBlendPolicy)

    def apply(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None = None,
        market_session: MarketSession | None = None,
    ) -> PortfolioBlendDecision:
        if not self.policy.active:
            return self._disabled(raw_batch)

        target_set = _TargetSet.from_batches(raw_batch, previous_batch)
        raw_weights, raw_tags = _weights_and_tags(raw_batch.targets)
        previous_weights, previous_tags = self._previous_weights(context.model_state, previous_batch)
        desired_weights, desired_tags = self._desired_weights(previous_weights, previous_tags, raw_weights, raw_tags)
        bypass_symbols = self._bypass_symbols(raw_batch.targets)

        active = self._active_transition(context.model_state, previous_batch)
        if active is not None:
            active = active.advance(context.data.time, market_session=market_session)
            retarget_drift = _target_drift(active.to_weights, desired_weights)
            if retarget_drift >= self.policy.target_drift_threshold_pct:
                active = self._new_transition(
                    context,
                    from_weights=active.weights_at_progress(),
                    to_weights=desired_weights,
                    to_tags=desired_tags,
                    source_batch_id=raw_batch.batch_id,
                    target_drift=retarget_drift,
                    reason="retarget_during_active_blend",
                )
                return self._active_decision(
                    raw_batch,
                    active,
                    target_set=target_set,
                    raw_weights=raw_weights,
                    raw_tags=raw_tags,
                    bypass_symbols=bypass_symbols,
                    status="retargeted",
                )
            return self._decision_from_transition(
                raw_batch,
                active,
                target_set=target_set,
                raw_weights=raw_weights,
                raw_tags=raw_tags,
                bypass_symbols=bypass_symbols,
            )

        if not previous_weights:
            return self._seed_decision(
                context,
                raw_batch,
                desired_weights,
                desired_tags,
                target_set=target_set,
            )

        drift = _target_drift(previous_weights, desired_weights)
        if drift < self.policy.target_drift_threshold_pct:
            patches = self._last_target_patches(context, raw_batch, desired_weights, desired_tags, reason="target_drift_below_threshold")
            return PortfolioBlendDecision(
                targets=raw_batch.targets,
                state_patches=patches,
                reason=raw_batch.reason,
                metadata=self._metadata(
                    raw_batch,
                    status="idle",
                    target_drift=drift,
                    state_store_attached=context.model_state.store is not None,
                    target_count=len(raw_batch.targets),
                    bypass_symbols=bypass_symbols,
                ),
            )

        transition = self._new_transition(
            context,
            from_weights=previous_weights,
            to_weights=desired_weights,
            to_tags=desired_tags,
            source_batch_id=raw_batch.batch_id,
            target_drift=drift,
            reason="target_drift",
        )
        return self._active_decision(
            raw_batch,
            transition,
            target_set=target_set,
            raw_weights=raw_weights,
            raw_tags=raw_tags,
            bypass_symbols=bypass_symbols,
            status="started",
        )

    def advance(
        self,
        context: PortfolioConstructionContext,
        source_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None = None,
        market_session: MarketSession | None = None,
    ) -> PortfolioBlendDecision:
        if not self.policy.active:
            return self._disabled(source_batch)
        active = self._active_transition(context.model_state, previous_batch or source_batch)
        if active is None:
            return PortfolioBlendDecision(
                targets=source_batch.targets,
                reason=source_batch.reason,
                metadata=self._metadata(
                    source_batch,
                    status="inactive",
                    state_store_attached=context.model_state.store is not None,
                    target_count=len(source_batch.targets),
                ),
            )
        active = active.advance(context.data.time, market_session=market_session)
        raw_targets = _targets_from_weights(
            active.to_weights,
            active.to_tags,
            target_set=_TargetSet.from_batches(source_batch, previous_batch),
            progress=1.0,
            blended_symbols=set(),
            bypass_symbols=set(),
        )
        synthetic_raw = replace(source_batch, targets=raw_targets)
        return self._decision_from_transition(
            synthetic_raw,
            active,
            target_set=_TargetSet.from_batches(synthetic_raw, previous_batch or source_batch),
            raw_weights=dict(active.to_weights),
            raw_tags=dict(active.to_tags),
            bypass_symbols=set(),
            status_when_active="advancing",
        )

    def _decision_from_transition(
        self,
        raw_batch: PortfolioTargetBatch,
        transition: PortfolioBlendTransition,
        *,
        target_set: "_TargetSet",
        raw_weights: Mapping[str, float],
        raw_tags: Mapping[str, str],
        bypass_symbols: set[str],
        status_when_active: str = "active",
    ) -> PortfolioBlendDecision:
        if transition.is_complete:
            patches = (
                *self._last_target_patches_from_values(
                    raw_batch.sleeve_id,
                    raw_batch.generated_at,
                    raw_batch.batch_id,
                    transition.to_weights,
                    transition.to_tags,
                    reason="portfolio_blend_complete",
                ),
                self._delete_active_patch(raw_batch.sleeve_id, raw_batch.generated_at, reason="portfolio_blend_complete"),
            )
            return PortfolioBlendDecision(
                targets=_targets_from_weights(
                    transition.to_weights,
                    transition.to_tags,
                    target_set=target_set,
                    progress=1.0,
                    blended_symbols=set(),
                    bypass_symbols=bypass_symbols,
                    raw_weights=raw_weights,
                    raw_tags=raw_tags,
                ),
                state_patches=patches,
                reason=f"{raw_batch.reason}:portfolio_blend_complete",
                metadata=self._metadata(
                    raw_batch,
                    status="completed",
                    transition=transition,
                    target_drift=transition.target_drift,
                    target_count=len(transition.to_weights),
                    bypass_symbols=bypass_symbols,
                ),
            )
        return self._active_decision(
            raw_batch,
            transition,
            target_set=target_set,
            raw_weights=raw_weights,
            raw_tags=raw_tags,
            bypass_symbols=bypass_symbols,
            status=status_when_active,
        )

    def _active_decision(
        self,
        raw_batch: PortfolioTargetBatch,
        transition: PortfolioBlendTransition,
        *,
        target_set: "_TargetSet",
        raw_weights: Mapping[str, float],
        raw_tags: Mapping[str, str],
        bypass_symbols: set[str],
        status: str,
    ) -> PortfolioBlendDecision:
        blended_weights = transition.weights_at_progress()
        blended_symbols = {
            symbol_key
            for symbol_key, weight in blended_weights.items()
            if abs(weight - transition.to_weights.get(symbol_key, 0.0)) > 1e-9
        }
        targets = _targets_from_weights(
            blended_weights,
            transition.to_tags,
            target_set=target_set,
            progress=transition.progress,
            blended_symbols=blended_symbols,
            bypass_symbols=bypass_symbols,
            raw_weights=raw_weights,
            raw_tags=raw_tags,
        )
        return PortfolioBlendDecision(
            targets=targets,
            state_patches=(self._active_transition_patch(transition, raw_batch.generated_at, reason=f"portfolio_blend_{status}"),),
            reason=f"{raw_batch.reason}:portfolio_blend_{status}",
            metadata=self._metadata(
                raw_batch,
                status=status,
                transition=transition,
                target_drift=transition.target_drift,
                target_count=len(targets),
                bypass_symbols=bypass_symbols,
            ),
        )

    def _seed_decision(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        weights: Mapping[str, float],
        tags: Mapping[str, str],
        *,
        target_set: "_TargetSet",
    ) -> PortfolioBlendDecision:
        patches = self._last_target_patches_from_values(
            raw_batch.sleeve_id,
            raw_batch.generated_at,
            raw_batch.batch_id,
            weights,
            tags,
            reason="portfolio_blend_seed_last_target",
        )
        return PortfolioBlendDecision(
            targets=raw_batch.targets,
            state_patches=patches,
            reason=raw_batch.reason,
            metadata=self._metadata(
                raw_batch,
                status="seeded",
                target_count=len(raw_batch.targets),
                target_drift=0.0,
                state_store_attached=context.model_state.store is not None,
            ),
        )

    def _disabled(self, batch: PortfolioTargetBatch) -> PortfolioBlendDecision:
        return PortfolioBlendDecision(
            targets=batch.targets,
            reason=batch.reason,
            metadata={
                "portfolio_blend": {
                    **self.policy.to_dict(),
                    "status": "disabled",
                    "raw_batch_id": batch.batch_id,
                    "target_count": len(batch.targets),
                }
            },
        )

    def _new_transition(
        self,
        context: PortfolioConstructionContext,
        *,
        from_weights: Mapping[str, float],
        to_weights: Mapping[str, float],
        to_tags: Mapping[str, str],
        source_batch_id: str,
        target_drift: float,
        reason: str,
    ) -> PortfolioBlendTransition:
        return PortfolioBlendTransition(
            transition_id=f"portfolio-blend-{uuid4()}",
            sleeve_id=context.sleeve_id,
            from_weights=from_weights,
            to_weights=to_weights,
            to_tags=to_tags,
            started_at=context.data.time,
            duration_minutes=self.policy.duration_minutes,
            elapsed_minutes=0.0,
            last_progress_at=context.data.time,
            source_batch_id=source_batch_id,
            reason=reason,
            clock=self.policy.clock,
            target_drift=target_drift,
        )

    def _previous_weights(
        self,
        state: RuntimeModelStateView,
        previous_batch: PortfolioTargetBatch | None,
    ) -> tuple[dict[str, float], dict[str, str]]:
        record = state.get(model_id=self.policy.model_id, namespace=LAST_TARGET_NAMESPACE)
        if record is not None:
            return _payload_weights_and_tags(record.value)
        if previous_batch is not None:
            return _weights_and_tags(previous_batch.targets)
        return {}, {}

    def _active_transition(
        self,
        state: RuntimeModelStateView,
        previous_batch: PortfolioTargetBatch | None,
    ) -> PortfolioBlendTransition | None:
        record = state.get(model_id=self.policy.model_id, namespace=ACTIVE_TRANSITION_NAMESPACE)
        if record is not None:
            return PortfolioBlendTransition.from_dict(record.value)
        payload = _portfolio_blend_metadata(previous_batch).get("active_transition") if previous_batch is not None else None
        if isinstance(payload, Mapping):
            return PortfolioBlendTransition.from_dict(payload)
        return None

    def _desired_weights(
        self,
        previous_weights: Mapping[str, float],
        previous_tags: Mapping[str, str],
        raw_weights: Mapping[str, float],
        raw_tags: Mapping[str, str],
    ) -> tuple[dict[str, float], dict[str, str]]:
        desired_weights = dict(raw_weights)
        desired_tags = dict(raw_tags)
        if self.policy.missing_target_behavior == "zero":
            for symbol_key, weight in previous_weights.items():
                if symbol_key not in desired_weights and abs(weight) > 1e-12:
                    desired_weights[symbol_key] = 0.0
                    desired_tags[symbol_key] = previous_tags.get(symbol_key, "portfolio_blend:missing_target_zero")
        return desired_weights, desired_tags

    def _bypass_symbols(self, targets: tuple[PortfolioAllocationTarget, ...]) -> set[str]:
        tokens = self.policy.bypass_target_tag_tokens
        if not tokens:
            return set()
        result = set()
        for target in targets:
            tag = str(target.tag or "").lower()
            if any(token in tag for token in tokens):
                result.add(target.symbol.key)
        return result

    def _last_target_patches(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        weights: Mapping[str, float],
        tags: Mapping[str, str],
        *,
        reason: str,
    ) -> tuple[StatePatch, ...]:
        return self._last_target_patches_from_values(
            context.sleeve_id,
            context.data.time,
            raw_batch.batch_id,
            weights,
            tags,
            reason=reason,
        )

    def _last_target_patches_from_values(
        self,
        sleeve_id: str,
        generated_at: datetime,
        source_batch_id: str,
        weights: Mapping[str, float],
        tags: Mapping[str, str],
        *,
        reason: str,
    ) -> tuple[StatePatch, ...]:
        return (
            StatePatch(
                key=self._state_key(sleeve_id, LAST_TARGET_NAMESPACE),
                value={
                    "weights": dict(_normalized_weights(weights)),
                    "tags": {str(key): str(value) for key, value in dict(tags).items()},
                    "source_batch_id": source_batch_id,
                    "updated_at": generated_at.isoformat(),
                },
                operation=StatePatchOperation.SET,
                reason=reason,
                generated_at=generated_at,
            ),
        )

    def _active_transition_patch(
        self,
        transition: PortfolioBlendTransition,
        generated_at: datetime,
        *,
        reason: str,
    ) -> StatePatch:
        return StatePatch(
            key=self._state_key(transition.sleeve_id, ACTIVE_TRANSITION_NAMESPACE),
            value=transition.to_dict(),
            operation=StatePatchOperation.SET,
            reason=reason,
            generated_at=generated_at,
        )

    def _delete_active_patch(self, sleeve_id: str, generated_at: datetime, *, reason: str) -> StatePatch:
        return StatePatch(
            key=self._state_key(sleeve_id, ACTIVE_TRANSITION_NAMESPACE),
            operation=StatePatchOperation.DELETE,
            reason=reason,
            generated_at=generated_at,
        )

    def _state_key(self, sleeve_id: str, namespace: str) -> ModelStateKey:
        return ModelStateKey(sleeve_id=sleeve_id, model_id=self.policy.model_id, namespace=namespace)

    def _metadata(
        self,
        batch: PortfolioTargetBatch,
        *,
        status: str,
        target_count: int,
        target_drift: float | None = None,
        transition: PortfolioBlendTransition | None = None,
        state_store_attached: bool | None = None,
        bypass_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            **self.policy.to_dict(),
            "status": status,
            "raw_batch_id": batch.batch_id,
            "target_count": target_count,
            "target_drift": target_drift,
            "bypassed_symbols": sorted(bypass_symbols or ()),
        }
        if transition is not None:
            payload.update(
                {
                    "transition_id": transition.transition_id,
                    "progress": transition.progress,
                    "elapsed_minutes": transition.elapsed_minutes,
                    "duration_minutes": transition.duration_minutes,
                    "started_at": transition.started_at.isoformat(),
                    "last_progress_at": transition.last_progress_at.isoformat()
                    if transition.last_progress_at
                    else None,
                    "active_transition": transition.to_dict(),
                }
            )
        if state_store_attached is not None:
            payload["state_store_attached"] = state_store_attached
        return {"portfolio_blend": payload}


@dataclass(frozen=True, slots=True)
class _TargetSet:
    symbols: Mapping[str, Symbol]
    tags: Mapping[str, str]
    order: tuple[str, ...]

    @classmethod
    def from_batches(
        cls,
        raw_batch: PortfolioTargetBatch,
        previous_batch: PortfolioTargetBatch | None,
    ) -> "_TargetSet":
        symbols: dict[str, Symbol] = {}
        tags: dict[str, str] = {}
        order: list[str] = []
        for batch in (raw_batch, previous_batch):
            if batch is None:
                continue
            for target in batch.targets:
                if target.symbol.key not in symbols:
                    symbols[target.symbol.key] = target.symbol
                    order.append(target.symbol.key)
                if target.tag and target.symbol.key not in tags:
                    tags[target.symbol.key] = target.tag
        return cls(
            symbols=MappingProxyType(symbols),
            tags=MappingProxyType(tags),
            order=tuple(order),
        )


def _targets_from_weights(
    weights: Mapping[str, float],
    tags: Mapping[str, str],
    *,
    target_set: _TargetSet,
    progress: float,
    blended_symbols: set[str],
    bypass_symbols: set[str],
    raw_weights: Mapping[str, float] | None = None,
    raw_tags: Mapping[str, str] | None = None,
) -> tuple[PortfolioAllocationTarget, ...]:
    raw_weights = raw_weights or {}
    raw_tags = raw_tags or {}
    ordered_keys = list(target_set.order)
    ordered_keys.extend(symbol_key for symbol_key in weights if symbol_key not in target_set.symbols)
    result: list[PortfolioAllocationTarget] = []
    seen: set[str] = set()
    for symbol_key in ordered_keys:
        if symbol_key in seen:
            continue
        seen.add(symbol_key)
        weight = raw_weights.get(symbol_key, weights.get(symbol_key, 0.0)) if symbol_key in bypass_symbols else weights.get(symbol_key, 0.0)
        if abs(weight) <= 1e-12 and symbol_key not in raw_weights and symbol_key not in weights:
            continue
        symbol = target_set.symbols.get(symbol_key) or _symbol_from_key(symbol_key)
        tag = raw_tags.get(symbol_key) or tags.get(symbol_key) or target_set.tags.get(symbol_key, "")
        if symbol_key in blended_symbols and symbol_key not in bypass_symbols:
            tag = f"{tag}:blend={progress:.3f}" if tag else f"portfolio_blend:{progress:.3f}"
        result.append(
            PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=_clamp_weight(weight),
                tag=tag,
            )
        )
    return tuple(result)


def _weights_and_tags(targets: tuple[PortfolioAllocationTarget, ...]) -> tuple[dict[str, float], dict[str, str]]:
    weights: dict[str, float] = {}
    tags: dict[str, str] = {}
    for target in targets:
        weights[target.symbol.key] = _clamp_weight(target.target_percent)
        tags[target.symbol.key] = str(target.tag or "")
    return weights, tags


def _payload_weights_and_tags(payload: Mapping[str, Any]) -> tuple[dict[str, float], dict[str, str]]:
    return _normalized_weights(dict(payload.get("weights") or {})), {
        str(key): str(value)
        for key, value in dict(payload.get("tags") or {}).items()
    }


def _portfolio_blend_metadata(batch: PortfolioTargetBatch | None) -> dict[str, Any]:
    if batch is None:
        return {}
    value = dict(batch.metadata).get("portfolio_blend")
    return dict(value) if isinstance(value, Mapping) else {}


def _target_drift(first: Mapping[str, float], second: Mapping[str, float]) -> float:
    keys = set(first) | set(second)
    return sum(abs(float(second.get(key, 0.0)) - float(first.get(key, 0.0))) for key in keys)


def _normalized_weights(value: Mapping[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, weight in dict(value).items():
        symbol_key = str(key).strip()
        if not symbol_key:
            continue
        result[symbol_key] = _clamp_weight(float(weight or 0.0))
    return result


def _clamp_weight(value: float) -> float:
    return max(-1.0, min(float(value), 1.0))


def _elapsed_minutes(start: datetime, end: datetime) -> float:
    start_time, end_time = _same_datetime_kind(start, end)
    return max((end_time - start_time).total_seconds() / 60.0, 0.0)


def _same_datetime_kind(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None and end.tzinfo is not None:
        return start.replace(tzinfo=end.tzinfo), end
    if start.tzinfo is not None and end.tzinfo is None:
        return start.replace(tzinfo=None), end
    return start, end


def _should_advance(clock: str, market_session: MarketSession | None) -> bool:
    if clock == "wall_time":
        return True
    if market_session is None:
        return True
    if clock == "regular_session":
        return market_session.is_regular_market_open
    return market_session.is_orderable


def _parse_datetime(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    return parsed or datetime.now()


def _optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    return datetime.fromisoformat(text)


def _symbol_from_key(symbol_key: str) -> Symbol:
    market, _, ticker = symbol_key.partition(":")
    if ticker:
        return Symbol(ticker=ticker, market=market)
    return Symbol(ticker=symbol_key, market="")

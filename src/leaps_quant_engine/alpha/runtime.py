from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import inspect
import logging
from threading import Lock
from typing import Any, Iterable, Mapping

from leaps_quant_engine.alpha.domain import AlphaModel, Insight, InsightBatch, SnapshotContext
from leaps_quant_engine.cadence import cadence_due, normalize_cadence
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.alpha.store import InsightStore
from leaps_quant_engine.runtime_state import StatePatch


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AlphaRuntime:
    active_models: tuple[AlphaModel, ...] = ()
    store: InsightStore = field(default_factory=InsightStore)
    _pending_models: tuple[AlphaModel, ...] | None = None
    _last_run_by_alpha_id: dict[str, datetime] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def active_alpha_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(_model_id(model) for model in self.active_models)

    def replace_active(self, models: list[AlphaModel] | tuple[AlphaModel, ...]) -> None:
        prepared = _prepare_models(models)
        with self._lock:
            self.active_models = prepared
            self._pending_models = None
            self._last_run_by_alpha_id.clear()
        logger.info("alpha_runtime.active.replace", extra={"alpha_ids": [_model_id(model) for model in prepared]})

    def last_run_state(self) -> dict[str, datetime]:
        with self._lock:
            return dict(self._last_run_by_alpha_id)

    def restore_last_run_state(self, state: Mapping[str, datetime]) -> None:
        with self._lock:
            active_ids = {_model_id(model) for model in self.active_models}
            self._last_run_by_alpha_id = {
                str(alpha_id): ran_at
                for alpha_id, ran_at in state.items()
                if alpha_id in active_ids
            }

    def stage(
        self,
        models: list[AlphaModel] | tuple[AlphaModel, ...],
        *,
        validation_context: SnapshotContext | None = None,
    ) -> None:
        prepared = _prepare_models(models)
        if validation_context is not None:
            for model in prepared:
                list(model.generate(validation_context))
        with self._lock:
            self._pending_models = prepared
        logger.info("alpha_runtime.pending.stage", extra={"alpha_ids": [_model_id(model) for model in prepared]})

    def activate_pending(self) -> bool:
        with self._lock:
            if self._pending_models is None:
                return False
            self.active_models = self._pending_models
            self._pending_models = None
            self._last_run_by_alpha_id.clear()
            active_ids = [_model_id(model) for model in self.active_models]
        logger.info("alpha_runtime.pending.activate", extra={"alpha_ids": active_ids})
        return True

    def run(
        self,
        context: SnapshotContext,
        *,
        activate_pending: bool = True,
        publish_active: bool = True,
        symbols_by_alpha: Mapping[str, Iterable[Symbol | str]] | None = None,
        default_symbols: Iterable[Symbol | str] | None = None,
    ) -> InsightBatch:
        if activate_pending:
            self.activate_pending()
        with self._lock:
            models = self.active_models
            last_run_by_alpha_id = dict(self._last_run_by_alpha_id)
        generated_at = context.as_of
        insights: list[Insight] = []
        state_patches: list[StatePatch] = []
        ran_alpha_ids: list[str] = []
        skipped_alpha_ids: list[str] = []
        cadences_by_alpha: dict[str, str] = {}
        for model in models:
            alpha_id = _model_id(model)
            cadence = normalize_cadence(getattr(model, "evaluation_cadence", "every_cycle"))
            cadences_by_alpha[alpha_id] = cadence
            if not cadence_due(cadence, context.as_of, last_run_by_alpha_id.get(alpha_id)):
                skipped_alpha_ids.append(alpha_id)
                logger.info(
                    "alpha_runtime.model.skip",
                    extra={
                        "alpha_id": alpha_id,
                        "alpha_version": getattr(model, "version", ""),
                        "sleeve_id": context.sleeve_id,
                        "cadence": cadence,
                        "last_run_at": last_run_by_alpha_id[alpha_id].isoformat(),
                    },
                )
                continue
            model_context = _context_for_alpha(
                context,
                alpha_id=alpha_id,
                symbols_by_alpha=symbols_by_alpha,
                default_symbols=default_symbols,
            )
            model_insights = list(model.generate(model_context))
            insights.extend(model_insights)
            state_patches.extend(_state_patches_for_model(model, model_context, model_insights))
            ran_alpha_ids.append(alpha_id)
            logger.info(
                "alpha_runtime.model.complete",
                extra={
                    "alpha_id": alpha_id,
                    "alpha_version": getattr(model, "version", ""),
                    "sleeve_id": context.sleeve_id,
                    "source_snapshot_id": context.source_snapshot_id,
                    "input_symbol_count": len(model_context.symbol_keys),
                    "insight_count": len(model_insights),
                    "cadence": cadence,
                },
            )
        if ran_alpha_ids:
            with self._lock:
                for alpha_id in ran_alpha_ids:
                    self._last_run_by_alpha_id[alpha_id] = context.as_of
        batch = InsightBatch(
            sleeve_id=context.sleeve_id,
            universe_id=context.universe_id,
            source_snapshot_id=context.source_snapshot_id,
            generated_at=generated_at,
            alpha_ids=tuple(_model_id(model) for model in models),
            insights=tuple(insights),
            state_patches=tuple(state_patches),
            metadata={
                "ran_alpha_ids": ran_alpha_ids,
                "skipped_alpha_ids": skipped_alpha_ids,
                "cadence_by_alpha": cadences_by_alpha,
                "state_patch_count": len(state_patches),
            },
        )
        if publish_active:
            self.store.publish_active(batch)
        logger.info(
            "alpha_runtime.batch.publish",
            extra={
                "batch_id": batch.batch_id,
                "sleeve_id": batch.sleeve_id,
                "universe_id": batch.universe_id,
                "source_snapshot_id": batch.source_snapshot_id,
                "alpha_ids": list(batch.alpha_ids),
                "insight_count": len(batch.insights),
            },
        )
        return batch


def _prepare_models(models: list[AlphaModel] | tuple[AlphaModel, ...]) -> tuple[AlphaModel, ...]:
    prepared = tuple(models)
    for model in prepared:
        if not callable(getattr(model, "generate", None)):
            raise TypeError("Alpha model must provide generate(context).")
        if not getattr(model, "alpha_id", None):
            raise ValueError("Alpha model must provide alpha_id.")
        if not getattr(model, "version", None):
            raise ValueError("Alpha model must provide version.")
    return prepared


def _model_id(model: AlphaModel) -> str:
    return str(getattr(model, "alpha_id"))


def _context_for_alpha(
    context: SnapshotContext,
    *,
    alpha_id: str,
    symbols_by_alpha: Mapping[str, Iterable[Symbol | str]] | None,
    default_symbols: Iterable[Symbol | str] | None,
) -> SnapshotContext:
    if symbols_by_alpha is not None and alpha_id in symbols_by_alpha:
        return context.with_input_symbols(symbols_by_alpha[alpha_id])
    if default_symbols is not None:
        return context.with_input_symbols(default_symbols)
    return context


def _state_patches_for_model(
    model: AlphaModel,
    context: SnapshotContext,
    insights: list[Insight],
) -> tuple[StatePatch, ...]:
    producer = getattr(model, "state_patches", None)
    if not callable(producer):
        return ()
    kwargs: dict[str, Any] = {}
    try:
        parameters = inspect.signature(producer).parameters
    except (TypeError, ValueError):
        parameters = {}
    supports_kwargs = any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    if supports_kwargs or "insights" in parameters:
        kwargs["insights"] = tuple(insights)
    if supports_kwargs or "context" in parameters:
        kwargs["context"] = context
    if kwargs:
        result = producer(**kwargs)
    elif not parameters:
        result = producer()
    else:
        result = producer(context, tuple(insights))
    return _coerce_state_patches(result)


def _coerce_state_patches(value: Any) -> tuple[StatePatch, ...]:
    if value is None:
        return ()
    patches = tuple(value)
    for patch in patches:
        if not isinstance(patch, StatePatch):
            raise TypeError("state_patches(...) must return StatePatch objects.")
    return patches

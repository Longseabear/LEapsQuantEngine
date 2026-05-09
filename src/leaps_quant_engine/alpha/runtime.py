from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import logging
from threading import Lock

from leaps_quant_engine.alpha.domain import AlphaModel, Insight, InsightBatch, SnapshotContext
from leaps_quant_engine.alpha.store import InsightStore


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AlphaRuntime:
    active_models: tuple[AlphaModel, ...] = ()
    store: InsightStore = field(default_factory=InsightStore)
    _pending_models: tuple[AlphaModel, ...] | None = None
    _lock: Lock = field(default_factory=Lock)

    def active_alpha_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(_model_id(model) for model in self.active_models)

    def replace_active(self, models: list[AlphaModel] | tuple[AlphaModel, ...]) -> None:
        prepared = _prepare_models(models)
        with self._lock:
            self.active_models = prepared
            self._pending_models = None
        logger.info("alpha_runtime.active.replace", extra={"alpha_ids": [_model_id(model) for model in prepared]})

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
            active_ids = [_model_id(model) for model in self.active_models]
        logger.info("alpha_runtime.pending.activate", extra={"alpha_ids": active_ids})
        return True

    def run(
        self,
        context: SnapshotContext,
        *,
        activate_pending: bool = True,
        publish_active: bool = True,
    ) -> InsightBatch:
        if activate_pending:
            self.activate_pending()
        with self._lock:
            models = self.active_models
        generated_at = context.as_of
        insights: list[Insight] = []
        for model in models:
            model_insights = list(model.generate(context))
            insights.extend(model_insights)
            logger.info(
                "alpha_runtime.model.complete",
                extra={
                    "alpha_id": _model_id(model),
                    "alpha_version": getattr(model, "version", ""),
                    "sleeve_id": context.sleeve_id,
                    "source_snapshot_id": context.source_snapshot_id,
                    "insight_count": len(model_insights),
                },
            )
        batch = InsightBatch(
            sleeve_id=context.sleeve_id,
            universe_id=context.universe_id,
            source_snapshot_id=context.source_snapshot_id,
            generated_at=generated_at,
            alpha_ids=tuple(_model_id(model) for model in models),
            insights=tuple(insights),
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

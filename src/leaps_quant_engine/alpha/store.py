from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock

from leaps_quant_engine.alpha.domain import InsightBatch


@dataclass(slots=True)
class InsightStore:
    _active: InsightBatch | None = None
    _lock: Lock = field(default_factory=Lock)

    def publish_active(self, batch: InsightBatch) -> InsightBatch:
        with self._lock:
            self._active = batch
            return batch

    def active(self) -> InsightBatch | None:
        with self._lock:
            return self._active

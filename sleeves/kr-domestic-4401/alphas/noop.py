from __future__ import annotations

from leaps_quant_engine.alpha import Insight, SnapshotContext


ALPHA_ID = "kr-domestic-4401-noop"
VERSION = "0.1.0"
EVALUATION_CADENCE = "daily_at 09:05 Asia/Seoul"
INPUT_RESOLUTION = "daily"


def generate(context: SnapshotContext) -> list[Insight]:
    return []

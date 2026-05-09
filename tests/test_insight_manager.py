from datetime import datetime, timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, InsightManager, InsightState
from leaps_quant_engine.alpha.domain import InsightBatch, InsightType
from leaps_quant_engine.models import Symbol


def _insight(
    *,
    symbol: Symbol = Symbol("NVDA", "US"),
    direction: InsightDirection = InsightDirection.UP,
    generated_at: datetime = datetime(2026, 5, 9, 9, 30),
    expires_at: datetime | None = datetime(2026, 5, 9, 9, 35),
    alpha_id: str = "alpha-a",
) -> Insight:
    return Insight(
        sleeve_id="us-live",
        symbol=symbol,
        insight_type=InsightType.PRICE,
        direction=direction,
        generated_at=generated_at,
        expires_at=expires_at,
        source_snapshot_id="snapshot-1",
        alpha_id=alpha_id,
        alpha_version="1.0",
        confidence=0.8,
        magnitude=0.03,
        weight=0.25,
        reason="test",
    )


def _batch(*insights: Insight) -> InsightBatch:
    return InsightBatch(
        sleeve_id="us-live",
        universe_id="us-active",
        source_snapshot_id="snapshot-1",
        generated_at=datetime(2026, 5, 9, 9, 30),
        alpha_ids=("alpha-a",),
        insights=insights,
    )


def test_insight_manager_ingests_and_returns_active_insights():
    manager = InsightManager()
    insight = _insight()

    update = manager.ingest(_batch(insight), as_of=datetime(2026, 5, 9, 9, 30))

    assert update.added_count == 1
    assert manager.active(datetime(2026, 5, 9, 9, 31)) == (insight,)
    assert manager.state_for(insight.insight_id) is InsightState.ACTIVE
    assert manager.tracked_symbols("us-live") == (Symbol("NVDA", "US"),)


def test_insight_manager_expires_old_insights_by_time():
    manager = InsightManager()
    insight = _insight(expires_at=datetime(2026, 5, 9, 9, 31))
    manager.ingest(_batch(insight), as_of=datetime(2026, 5, 9, 9, 30))

    update = manager.expire(datetime(2026, 5, 9, 9, 32))

    assert update.expired_count == 1
    assert manager.active(datetime(2026, 5, 9, 9, 32)) == ()
    assert manager.state_for(insight.insight_id) is InsightState.EXPIRED


def test_insight_manager_supersedes_same_alpha_symbol_signal():
    manager = InsightManager()
    first = _insight(generated_at=datetime(2026, 5, 9, 9, 30))
    second = _insight(generated_at=datetime(2026, 5, 9, 9, 31), expires_at=datetime(2026, 5, 9, 9, 40))

    manager.ingest(_batch(first), as_of=datetime(2026, 5, 9, 9, 30))
    update = manager.ingest(_batch(second), as_of=datetime(2026, 5, 9, 9, 31))

    assert update.superseded_count == 1
    assert manager.state_for(first.insight_id) is InsightState.SUPERSEDED
    assert manager.active(datetime(2026, 5, 9, 9, 32)) == (second,)


def test_insight_manager_can_cancel_symbol_insights():
    manager = InsightManager()
    insight = _insight(expires_at=datetime(2026, 5, 9, 10, 0))
    manager.ingest(_batch(insight), as_of=datetime(2026, 5, 9, 9, 30))

    update = manager.cancel_symbol("us-live", Symbol("NVDA", "US"), as_of=datetime(2026, 5, 9, 9, 31))

    assert update.cancelled_count == 1
    assert manager.active(datetime(2026, 5, 9, 9, 32)) == ()
    assert manager.state_for(insight.insight_id) is InsightState.CANCELLED


from __future__ import annotations

from datetime import datetime

from leaps_quant_engine.model_state_seed import seed_trailing_stop_state_from_positions
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, ModelStateKey, StatePatch
from leaps_quant_engine.virtual_account import PositionState


def test_seed_trailing_stop_state_from_virtual_position_states():
    store = InMemoryRuntimeStateStore()
    position = PositionState(
        sleeve_id="LEaps",
        symbol=Symbol("005930", "KRX"),
        quantity=12,
        average_entry_price=277_293,
        entry_time=datetime(2026, 5, 12, 9, 2),
        high_watermark_price=291_000,
        high_watermark_at=datetime(2026, 5, 14, 12, 6),
        last_price=296_000,
        last_updated_at=datetime(2026, 5, 14, 16, 20),
    )

    report = seed_trailing_stop_state_from_positions((position,), store, sleeve_id="LEaps")

    key = ModelStateKey(
        sleeve_id="LEaps",
        model_id="leaps-volatility-trailing-stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
    )
    record = store.get(key)
    assert report.status == "seeded"
    assert report.seeded_count == 1
    assert record is not None
    assert record.value["high_watermark_price"] == 296_000
    assert record.value["seeded_from"] == "virtual_account_position_state"


def test_seed_trailing_stop_state_never_lowers_existing_high_watermark():
    store = InMemoryRuntimeStateStore()
    key = ModelStateKey(
        sleeve_id="LEaps",
        model_id="leaps-volatility-trailing-stop",
        namespace="trailing_stop",
        symbol_key="KRX:006400",
    )
    store.apply_patches((StatePatch(key=key, value={"high_watermark_price": 712_000}),))
    position = PositionState(
        sleeve_id="LEaps",
        symbol=Symbol("006400", "KRX"),
        quantity=1,
        average_entry_price=633_000,
        entry_time=datetime(2026, 5, 14, 10, 21),
        high_watermark_price=633_000,
        high_watermark_at=datetime(2026, 5, 14, 10, 21),
        last_price=636_000,
        last_updated_at=datetime(2026, 5, 14, 16, 20),
    )

    seed_trailing_stop_state_from_positions((position,), store, sleeve_id="LEaps")

    record = store.get(key)
    assert record is not None
    assert record.value["high_watermark_price"] == 712_000

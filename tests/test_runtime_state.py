from __future__ import annotations

from datetime import datetime

import pytest

from leaps_quant_engine.runtime_state import (
    InMemoryRuntimeStateStore,
    ModelStateKey,
    RuntimeModelStateView,
    SQLiteRuntimeStateStore,
    StatePatch,
    StatePatchOperation,
    fork_sqlite_runtime_state,
)


def test_in_memory_runtime_state_store_merges_sets_and_deletes_state():
    store = InMemoryRuntimeStateStore()
    key = ModelStateKey(
        sleeve_id="LEaps",
        model_id="volatility_trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
        position_id="pos-1",
    )
    first_at = datetime(2026, 5, 13, 9, 0)

    events = store.apply_patches(
        (
            StatePatch(
                key=key,
                value={"high_watermark_price": 284000, "last_price": 280000},
                reason="mark_position",
            ),
        ),
        applied_at=first_at,
    )

    record = store.get(key)
    assert record is not None
    assert record.value["high_watermark_price"] == 284000
    assert record.value["last_price"] == 280000
    assert record.version == 1
    assert record.created_at == first_at
    assert events[0].prior_version is None
    assert events[0].new_version == 1

    second_at = datetime(2026, 5, 13, 9, 1)
    store.apply_patches(
        (StatePatch(key=key, value={"last_price": 281000}, reason="refresh_price"),),
        applied_at=second_at,
    )

    merged = store.get(key)
    assert merged is not None
    assert merged.value == {"high_watermark_price": 284000, "last_price": 281000}
    assert merged.version == 2
    assert merged.created_at == first_at
    assert merged.updated_at == second_at

    store.apply_patches(
        (
            StatePatch(
                key=key,
                value={"cooldown_until": "2026-05-13T09:05:00"},
                operation=StatePatchOperation.SET,
            ),
        ),
        applied_at=datetime(2026, 5, 13, 9, 2),
    )
    replaced = store.get(key)
    assert replaced is not None
    assert replaced.value == {"cooldown_until": "2026-05-13T09:05:00"}
    assert replaced.version == 3

    delete_events = store.apply_patches(
        (StatePatch(key=key, operation=StatePatchOperation.DELETE, reason="position_closed"),),
        applied_at=datetime(2026, 5, 13, 9, 3),
    )
    assert store.get(key) is None
    assert delete_events[0].prior_version == 3
    assert delete_events[0].new_version is None
    assert len(store.events()) == 4


def test_sqlite_runtime_state_store_persists_and_filters_model_state(tmp_path):
    path = tmp_path / "runtime-state.sqlite"
    store = SQLiteRuntimeStateStore(path)
    leaps_key = ModelStateKey(
        sleeve_id="LEaps",
        model_id="volatility_trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
        position_id="pos-1",
    )
    etf_key = ModelStateKey(
        sleeve_id="ETF",
        model_id="target_smoothing",
        namespace="execution",
        symbol_key="NAS:QQQ",
    )

    store.apply_patches(
        (
            StatePatch(key=leaps_key, value={"high_watermark_price": 284000}),
            StatePatch(key=etf_key, value={"previous_target_weight": 0.5}),
        ),
        applied_at=datetime(2026, 5, 13, 10, 0),
    )

    reloaded = SQLiteRuntimeStateStore(path)
    leaps_record = reloaded.get(leaps_key)
    assert leaps_record is not None
    assert leaps_record.value["high_watermark_price"] == 284000
    assert leaps_record.version == 1

    assert [record.key for record in reloaded.entries(sleeve_id="LEaps")] == [leaps_key]
    assert [record.key for record in reloaded.entries(model_id="target_smoothing")] == [etf_key]
    assert [record.key for record in reloaded.entries(symbol_key="KRX:005930")] == [leaps_key]

    events = reloaded.events(sleeve_id="LEaps")
    assert len(events) == 1
    assert events[0].operation is StatePatchOperation.MERGE
    assert events[0].new_version == 1


def test_sqlite_runtime_state_store_namespaces_same_symbol_by_position(tmp_path):
    store = SQLiteRuntimeStateStore(tmp_path / "runtime-state.sqlite")
    first = ModelStateKey(
        sleeve_id="LEaps",
        model_id="trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
        position_id="pos-1",
    )
    second = ModelStateKey(
        sleeve_id="LEaps",
        model_id="trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
        position_id="pos-2",
    )

    store.apply_patches(
        (
            StatePatch(key=first, value={"high_watermark_price": 280000}),
            StatePatch(key=second, value={"high_watermark_price": 290000}),
        )
    )

    assert store.get(first).value["high_watermark_price"] == 280000
    assert store.get(second).value["high_watermark_price"] == 290000
    assert len(store.entries(symbol_key="KRX:005930")) == 2


def test_fork_sqlite_runtime_state_uses_consistent_backup_snapshot(tmp_path):
    source_path = tmp_path / "live.sqlite"
    target_path = tmp_path / "sandbox" / "fork.sqlite"
    source = SQLiteRuntimeStateStore(source_path)
    key = ModelStateKey(
        sleeve_id="LEaps",
        model_id="trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
    )
    source.apply_patches((StatePatch(key=key, value={"high_watermark_price": 85000}),))

    report = fork_sqlite_runtime_state(source_path, target_path)

    assert report.status == "forked"
    assert report.record_count == 1
    assert report.event_count == 1
    assert report.source == source_path.resolve()
    assert report.target == target_path.resolve()
    forked = SQLiteRuntimeStateStore(target_path)
    assert forked.get(key).value["high_watermark_price"] == 85000


def test_fork_sqlite_runtime_state_refuses_to_overwrite_by_default(tmp_path):
    source_path = tmp_path / "live.sqlite"
    target_path = tmp_path / "sandbox.sqlite"
    source = SQLiteRuntimeStateStore(source_path)
    key = ModelStateKey(sleeve_id="LEaps", model_id="trailing_stop")
    source.apply_patches((StatePatch(key=key, value={"last_price": 100}),))
    fork_sqlite_runtime_state(source_path, target_path)

    with pytest.raises(FileExistsError):
        fork_sqlite_runtime_state(source_path, target_path)

    report = fork_sqlite_runtime_state(source_path, target_path, overwrite=True)
    assert report.target == target_path.resolve()


def test_state_patch_requires_values_for_set_and_merge():
    key = ModelStateKey(sleeve_id="LEaps", model_id="model")

    with pytest.raises(ValueError):
        StatePatch(key=key)

    delete_patch = StatePatch(key=key, operation=StatePatchOperation.DELETE)
    assert delete_patch.operation is StatePatchOperation.DELETE


def test_runtime_model_state_view_is_read_only_and_uses_defaults():
    store = InMemoryRuntimeStateStore()
    view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps", default_model_id="trailing-stop")
    key = view.key(namespace="stop", symbol_key="KRX:005930")

    store.apply_patches((StatePatch(key=key, value={"high": 84000}),))

    record = view.get(namespace="stop", symbol_key="KRX:005930")
    assert record is not None
    assert record.value["high"] == 84000
    assert view.entries(namespace="stop") == (record,)

    empty_view = RuntimeModelStateView()
    assert empty_view.get(key) is None
    assert empty_view.entries() == ()


def test_runtime_model_state_view_object_helpers_build_state_patches():
    store = InMemoryRuntimeStateStore()
    view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps", default_model_id="trailing-stop")

    set_patch = view.object_set(
        {"high": 84000},
        namespace="stop",
        symbol_key="KRX:005930",
        reason="seed",
        generated_at=datetime(2026, 5, 8, 9, 30),
    )
    merge_patch = view.object_merge(
        {"last": 83500},
        namespace="stop",
        symbol_key="KRX:005930",
        reason="refresh",
    )
    store.apply_patches((set_patch, merge_patch))

    assert view.object_get(namespace="stop", symbol_key="KRX:005930") == {"high": 84000, "last": 83500}
    assert view.object_entries(namespace="stop") == ({"high": 84000, "last": 83500},)

    store.apply_patches((view.object_delete(namespace="stop", symbol_key="KRX:005930", reason="closed"),))

    assert view.object_get(namespace="stop", symbol_key="KRX:005930", default={"missing": True}) == {"missing": True}


def test_runtime_model_state_scope_binds_model_namespace_symbol_and_position():
    store = InMemoryRuntimeStateStore()
    view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps")
    scope = view.scope(
        model_id="volatility_trailing_stop",
        namespace="trailing_stop",
    ).for_symbol("KRX:005930").for_position("pos-1")

    store.apply_patches(
        (
            scope.object_set(
                {"high_watermark_price": 84000},
                reason="seed_trailing_stop",
                generated_at=datetime(2026, 5, 13, 9, 0),
            ),
            scope.object_merge(
                {"last_price": 83500},
                reason="refresh_trailing_stop",
                generated_at=datetime(2026, 5, 13, 9, 1),
            ),
        )
    )

    assert scope.object_get() == {"high_watermark_price": 84000, "last_price": 83500}
    assert scope.get().key == ModelStateKey(
        sleeve_id="LEaps",
        model_id="volatility_trailing_stop",
        namespace="trailing_stop",
        symbol_key="KRX:005930",
        position_id="pos-1",
    )
    assert scope.object_entries() == ({"high_watermark_price": 84000, "last_price": 83500},)

    other_symbol = scope.for_symbol("KRX:000660")
    assert other_symbol.object_get(default={"missing": True}) == {"missing": True}

from datetime import datetime

import pytest

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection, PythonAlphaLoader, SnapshotContext
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue, SnapshotFreshnessPolicy


class FixedAlpha:
    def __init__(self, alpha_id: str, reason: str):
        self.alpha_id = alpha_id
        self.version = "1.0"
        self.reason = reason

    def generate(self, context):
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol("KRX:005930"),
                direction=InsightDirection.UP,
                generated_at=datetime(2026, 5, 8, 9, 0),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason=self.reason,
            )
        ]


class SymbolEchoAlpha:
    version = "1.0"

    def __init__(self, alpha_id):
        self.alpha_id = alpha_id
        self.seen_symbol_keys = ()

    def generate(self, context):
        self.seen_symbol_keys = context.symbol_keys
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                reason="echo_input_symbol",
            )
            for symbol_key in context.symbol_keys
        ]


def _snapshot():
    quality = SnapshotFreshnessPolicy().evaluate(
        requested_symbol_count=1,
        collected_symbol_count=1,
        failed_symbol_count=0,
        completed_at=datetime(2026, 5, 8, 9, 0),
        elapsed_ms=10.0,
        now=datetime(2026, 5, 8, 9, 0),
    )
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="swing-kor",
        universe_id="test-universe",
        as_of=datetime(2026, 5, 8, 9, 0),
        created_at=datetime(2026, 5, 8, 9, 0),
        symbols=("KRX:005930",),
        source_snapshot_id="market-test",
        quality_report=quality,
        values={
            "KRX:005930": {
                "close": IndicatorValue("close", 110.0, True, 1, datetime(2026, 5, 8, 9, 0)),
                "sma_3_close": IndicatorValue("sma_3_close", 100.0, True, 3, datetime(2026, 5, 8, 9, 0)),
                "momentum_2_close": IndicatorValue(
                    "momentum_2_close",
                    0.03,
                    True,
                    3,
                    datetime(2026, 5, 8, 9, 0),
                ),
            }
        },
    )


def test_snapshot_context_reads_indicator_snapshot_values():
    context = SnapshotContext.from_indicator_snapshot(_snapshot())

    assert context.sleeve_id == "swing-kor"
    assert context.source_snapshot_id == "market-test"
    assert context.value("KRX:005930", "close") == 110.0
    assert context.symbol("KRX:005930") == Symbol("005930", "KRX")
    assert context.allows_new_entries is True


def test_snapshot_context_can_scope_alpha_input_symbols():
    context = SnapshotContext.from_indicator_snapshot(_snapshot())
    filtered = context.with_input_symbols(["KRX:005930", Symbol("005930", "KRX")])

    assert filtered.symbol_keys == ("KRX:005930",)
    assert filtered.available_symbol_keys == ("KRX:005930",)
    assert context.symbol_keys == ("KRX:005930",)


def test_alpha_runtime_swaps_pending_models_at_snapshot_boundary():
    context = SnapshotContext.from_indicator_snapshot(_snapshot())
    runtime = AlphaRuntime(active_models=(FixedAlpha("old", "old_reason"),))

    first_batch = runtime.run(context)
    runtime.stage([FixedAlpha("new", "new_reason")], validation_context=context)
    second_batch = runtime.run(context)

    assert first_batch.generated_at == context.as_of
    assert first_batch.alpha_ids == ("old",)
    assert first_batch.insights[0].reason == "old_reason"
    assert second_batch.alpha_ids == ("new",)
    assert second_batch.insights[0].reason == "new_reason"
    assert runtime.store.active() is second_batch


def test_alpha_runtime_passes_symbol_inputs_by_alpha_id():
    now = datetime(2026, 5, 8, 9, 0)
    snapshot = IndicatorSnapshot(
        snapshot_id="indicator-inputs",
        sleeve_id="swing-kor",
        universe_id="test-universe",
        as_of=now,
        created_at=now,
        symbols=("KRX:000001", "KRX:000002", "KRX:000003"),
        source_snapshot_id="market-test",
        values={
            "KRX:000001": {"close": IndicatorValue("close", 100.0, True, 1, now)},
            "KRX:000002": {"close": IndicatorValue("close", 110.0, True, 1, now)},
            "KRX:000003": {"close": IndicatorValue("close", 120.0, True, 1, now)},
        },
    )
    momentum = SymbolEchoAlpha("momentum")
    etf = SymbolEchoAlpha("etf-rotation")

    batch = AlphaRuntime(active_models=(momentum, etf)).run(
        SnapshotContext.from_indicator_snapshot(snapshot),
        symbols_by_alpha={
            "momentum": ("KRX:000001", "KRX:000002"),
            "etf-rotation": (Symbol("000003", "KRX"),),
        },
    )

    assert momentum.seen_symbol_keys == ("KRX:000001", "KRX:000002")
    assert etf.seen_symbol_keys == ("KRX:000003",)
    assert [insight.symbol.key for insight in batch.insights] == ["KRX:000001", "KRX:000002", "KRX:000003"]


def test_alpha_runtime_skips_once_per_day_model_after_first_run_same_day():
    daily = SymbolEchoAlpha("daily-alpha")
    daily.evaluation_cadence = "once_per_day"
    context = SnapshotContext.from_indicator_snapshot(_snapshot())
    runtime = AlphaRuntime(active_models=(daily,))

    first = runtime.run(context)
    second = runtime.run(context)

    assert first.insight_count == 1
    assert second.insight_count == 0
    assert second.metadata["ran_alpha_ids"] == []
    assert second.metadata["skipped_alpha_ids"] == ["daily-alpha"]
    assert daily.seen_symbol_keys == ("KRX:005930",)


def test_python_alpha_loader_loads_generate_function(tmp_path):
    alpha_file = tmp_path / "my_alpha.py"
    alpha_file.write_text(
        """
from datetime import datetime
from leaps_quant_engine.alpha import Insight, InsightDirection

ALPHA_ID = "tmp-alpha"
VERSION = "2026.05.08"

def generate(context):
    return [Insight(
        sleeve_id=context.sleeve_id,
        symbol=context.symbol("KRX:005930"),
        direction=InsightDirection.UP,
        generated_at=datetime(2026, 5, 8, 9, 0),
        source_snapshot_id=context.source_snapshot_id,
        alpha_id=ALPHA_ID,
        alpha_version=VERSION,
        confidence=0.7,
        reason="loaded_from_python",
    )]
""",
        encoding="utf-8",
    )

    loaded = PythonAlphaLoader().load(alpha_file)
    batch = AlphaRuntime(active_models=(loaded.model,)).run(SnapshotContext.from_indicator_snapshot(_snapshot()))

    assert loaded.alpha_id == "tmp-alpha"
    assert loaded.version == "2026.05.08"
    assert len(loaded.content_hash) == 64
    assert batch.insights[0].reason == "loaded_from_python"


def test_alpha_runtime_rejects_model_without_metadata():
    class BadAlpha:
        def generate(self, context):
            return []

    with pytest.raises(ValueError):
        AlphaRuntime().replace_active([BadAlpha()])

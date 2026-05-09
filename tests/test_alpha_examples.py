from datetime import datetime
from pathlib import Path

from leaps_quant_engine.alpha import AlphaRuntime, InsightDirection, PythonAlphaLoader, SnapshotContext
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue


def _snapshot(values_by_symbol):
    as_of = datetime(2026, 5, 9, 9, 30)
    return IndicatorSnapshot(
        snapshot_id="alpha-examples",
        sleeve_id="example-sleeve",
        universe_id="example-universe",
        as_of=as_of,
        created_at=as_of,
        symbols=tuple(values_by_symbol),
        source_snapshot_id="market-example",
        values={
            symbol_key: {
                name: IndicatorValue(name, value, True, 20, as_of)
                for name, value in values.items()
            }
            for symbol_key, values in values_by_symbol.items()
        },
    )


def _run_alpha(path: str, snapshot: IndicatorSnapshot):
    loaded = PythonAlphaLoader().load(Path(path))
    return AlphaRuntime(active_models=(loaded.model,)).run(SnapshotContext.from_indicator_snapshot(snapshot))


def test_momentum_strategy_alpha_emits_up_insight():
    batch = _run_alpha(
        "examples/alpha/momentum_strategy_alpha.py",
        _snapshot(
            {
                "US:NVDA": {
                    "identity_close": 120.0,
                    "sma_5_close": 110.0,
                    "momentum_5_close": 0.08,
                    "rolling_dollar_volume_20": 1_000_000_000.0,
                }
            }
        ),
    )

    assert batch.alpha_ids == ("momentum-strategy-demo",)
    assert len(batch.insights) == 1
    assert batch.insights[0].direction is InsightDirection.UP
    assert batch.insights[0].expires_at is not None
    assert batch.insights[0].weight == 0.08


def test_etf_rotation_alpha_emits_top_weighted_up_and_flat_for_unselected():
    batch = _run_alpha(
        "examples/alpha/etf_rotation_alpha.py",
        _snapshot(
            {
                "US:SPY": {"roc_20_close": 0.05, "stddev_20_close": 2.0, "rolling_dollar_volume_20": 2_000_000_000.0},
                "US:QQQ": {"roc_20_close": 0.09, "stddev_20_close": 3.0, "rolling_dollar_volume_20": 3_000_000_000.0},
                "US:SMH": {"roc_20_close": 0.12, "stddev_20_close": 4.0, "rolling_dollar_volume_20": 1_000_000_000.0},
                "US:TLT": {"roc_20_close": -0.01, "stddev_20_close": 1.0, "rolling_dollar_volume_20": 500_000_000.0},
            }
        ),
    )

    up = [insight for insight in batch.insights if insight.direction is InsightDirection.UP]
    flat = [insight for insight in batch.insights if insight.direction is InsightDirection.FLAT]

    assert batch.alpha_ids == ("etf-rotation-demo",)
    assert len(up) == 3
    assert len(flat) == 1
    assert {insight.symbol_key for insight in flat} == {"US:TLT"}
    assert all(insight.weight == 1 / 3 for insight in up)


def test_volatility_trailing_stop_alpha_emits_flat_exit_insight():
    batch = _run_alpha(
        "examples/alpha/volatility_trailing_stop_alpha.py",
        _snapshot(
            {
                "US:NVDA": {
                    "identity_close": 90.0,
                    "rolling_max_20_close": 110.0,
                    "atr_14": 4.0,
                    "stddev_20_close": 3.0,
                }
            }
        ),
    )

    assert batch.alpha_ids == ("volatility-trailing-stop-demo",)
    assert len(batch.insights) == 1
    assert batch.insights[0].direction is InsightDirection.FLAT
    assert batch.insights[0].weight == 0.0
    assert batch.insights[0].metadata["stop_price"] == 100.0


def test_live_quote_smoke_alpha_emits_short_lived_up_insight():
    batch = _run_alpha(
        "examples/alpha/live_quote_smoke_alpha.py",
        _snapshot(
            {
                "US:NVDA": {
                    "close": 120.0,
                    "vwap_1": 119.0,
                    "volume": 1000.0,
                }
            }
        ),
    )

    assert batch.alpha_ids == ("live-quote-smoke",)
    assert len(batch.insights) == 1
    assert batch.insights[0].direction is InsightDirection.UP
    assert batch.insights[0].expires_at is not None
    assert batch.insights[0].weight == 0.05

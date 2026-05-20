from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.models import Bar, DataResolution, DataSlice, Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.temporal_features import (
    TemporalFeatureWindowConfig,
    TemporalFeatureWindowProvider,
    enrich_indicator_snapshot_with_temporal_features,
)


def test_temporal_feature_provider_builds_point_in_time_window_without_future_rows():
    symbol = Symbol("005930", "KRX")
    bars = _daily_bars(symbol, count=110, start=datetime(2026, 1, 1), first_close=100.0)
    as_of = bars[90].time
    provider = TemporalFeatureWindowProvider(TemporalFeatureWindowConfig(lookback_window=64))
    provider.update(bars)
    snapshot = _snapshot(symbol, as_of=as_of)

    enriched = enrich_indicator_snapshot_with_temporal_features(snapshot, provider)
    rows = enriched.metadata_value(symbol.key, "rl_temporal_features")

    assert len(rows) == 64
    assert enriched.metadata_value(symbol.key, "rl_temporal_feature_schema") == "v2_temporal"
    assert enriched.metadata_value(symbol.key, "rl_temporal_feature_as_of") == as_of.isoformat()
    expected_return_1 = (bars[90].close / bars[89].close) - 1.0
    assert rows[-1]["return_1"] == pytest.approx(expected_return_1)
    future_return_1 = (bars[-1].close / bars[-2].close) - 1.0
    assert rows[-1]["return_1"] != pytest.approx(future_return_1)


def test_temporal_feature_provider_fails_closed_when_history_is_short():
    symbol = Symbol("005930", "KRX")
    bars = _daily_bars(symbol, count=64, start=datetime(2026, 1, 1), first_close=100.0)
    provider = TemporalFeatureWindowProvider(TemporalFeatureWindowConfig(lookback_window=64))
    provider.update(bars)
    snapshot = _snapshot(symbol, as_of=bars[-1].time)

    enriched = enrich_indicator_snapshot_with_temporal_features(snapshot, provider)

    assert enriched.metadata_value(symbol.key, "rl_temporal_features") is None


def test_temporal_feature_provider_ignores_minute_bars_for_daily_window():
    symbol = Symbol("005930", "KRX")
    bars = _daily_bars(symbol, count=90, start=datetime(2026, 1, 1), first_close=100.0)
    provider = TemporalFeatureWindowProvider(TemporalFeatureWindowConfig(lookback_window=64))
    provider.update(bars)
    minute_time = bars[-1].time + timedelta(hours=1)
    provider.update(
        DataSlice(
            time=minute_time,
            resolution=DataResolution.MINUTE.value,
            bars={
                symbol.key: Bar(
                    symbol,
                    minute_time,
                    999.0,
                    999.0,
                    999.0,
                    999.0,
                    resolution=DataResolution.MINUTE.value,
                )
            },
        )
    )
    snapshot = _snapshot(symbol, as_of=minute_time)

    rows = enrich_indicator_snapshot_with_temporal_features(snapshot, provider).metadata_value(
        symbol.key,
        "rl_temporal_features",
    )

    assert rows[-1]["return_1"] == pytest.approx((bars[-1].close / bars[-2].close) - 1.0)


def test_temporal_feature_provider_builds_residual_windows_for_aligned_symbols():
    first = Symbol("005930", "KRX")
    second = Symbol("000660", "KRX")
    start = datetime(2026, 1, 1)
    provider = TemporalFeatureWindowProvider(
        TemporalFeatureWindowConfig(feature_schema="v2_temporal_residual", lookback_window=84)
    )
    provider.update(_daily_bars(first, count=150, start=start, first_close=100.0))
    provider.update(_daily_bars(second, count=150, start=start, first_close=130.0))
    snapshot = IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="LEaps",
        universe_id="test",
        as_of=start + timedelta(days=149),
        created_at=start + timedelta(days=149),
        symbols=(first.key, second.key),
        values={
            first.key: {"close": IndicatorValue("close", 0.0, True, 1)},
            second.key: {"close": IndicatorValue("close", 0.0, True, 1)},
        },
    )

    enriched = enrich_indicator_snapshot_with_temporal_features(snapshot, provider)
    rows = enriched.metadata_value(first.key, "rl_temporal_features")

    assert len(rows) == 84
    assert "residual_momentum_20" in rows[-1]
    assert "market_beta_60" in rows[-1]


def test_residual_temporal_provider_does_not_block_all_symbols_when_one_history_is_short():
    ready = Symbol("005930", "KRX")
    short = Symbol("123456", "KRX")
    start = datetime(2026, 1, 1)
    provider = TemporalFeatureWindowProvider(
        TemporalFeatureWindowConfig(feature_schema="v2_temporal_residual", lookback_window=84)
    )
    provider.update(_daily_bars(ready, count=150, start=start, first_close=100.0))
    provider.update(_daily_bars(short, count=20, start=start + timedelta(days=130), first_close=80.0))
    snapshot = IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="LEaps",
        universe_id="test",
        as_of=start + timedelta(days=149),
        created_at=start + timedelta(days=149),
        symbols=(ready.key, short.key),
        values={
            ready.key: {"close": IndicatorValue("close", 0.0, True, 1)},
            short.key: {"close": IndicatorValue("close", 0.0, True, 1)},
        },
    )

    enriched = enrich_indicator_snapshot_with_temporal_features(snapshot, provider)

    assert len(enriched.metadata_value(ready.key, "rl_temporal_features")) == 84
    assert enriched.metadata_value(short.key, "rl_temporal_features") is None


def _daily_bars(symbol: Symbol, *, count: int, start: datetime, first_close: float) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            time=start + timedelta(days=index),
            open=first_close + index,
            high=first_close + index + 1,
            low=first_close + index - 1,
            close=first_close + index,
            volume=1_000_000,
            resolution=DataResolution.DAILY.value,
        )
        for index in range(count)
    ]


def _snapshot(symbol: Symbol, *, as_of: datetime) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="LEaps",
        universe_id="test",
        as_of=as_of,
        created_at=as_of,
        symbols=(symbol.key,),
        values={symbol.key: {"close": IndicatorValue("close", 0.0, True, 1)}},
    )

from datetime import datetime

from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.runtime import UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import (
    MomentumUniverseSelectionModel,
    StaticUniverseSelectionModel,
    UniverseSelectionContext,
)


def _universe():
    return parse_universe_definition(
        {
            "id": "coarse-test",
            "market": "KRX",
            "symbols": ["000001", "000002", "000003", "000004"],
            "indicators": [
                {"name": "identity_close", "type": "identity", "period": 1},
                {"name": "sma_5_close", "type": "sma", "period": 5},
                {"name": "momentum_5_close", "type": "momentum", "period": 5},
                {"name": "rolling_dollar_volume_20", "type": "rolling_dollar_volume", "period": 20},
                {"name": "stddev_20_close", "type": "stddev", "period": 20},
            ],
        }
    )


def _snapshot():
    now = datetime(2026, 5, 8, 9, 0)
    values = {
        "KRX:000001": _values(close=100, sma=95, momentum=0.03, liquidity=10_000, volatility=1.0, now=now),
        "KRX:000002": _values(close=100, sma=101, momentum=0.08, liquidity=50_000, volatility=3.0, now=now),
        "KRX:000003": _values(close=100, sma=98, momentum=-0.01, liquidity=80_000, volatility=2.0, now=now),
        "KRX:000004": _values(close=100, sma=99, momentum=0.01, liquidity=5_000, volatility=0.5, now=now),
    }
    return IndicatorSnapshot(
        snapshot_id="indicator-selection",
        sleeve_id="swing-kor",
        universe_id="coarse-test",
        as_of=now,
        created_at=now,
        symbols=tuple(values),
        source_snapshot_id="market-selection",
        values=values,
    )


def _values(*, close, sma, momentum, liquidity, volatility, now):
    return {
        "identity_close": IndicatorValue("identity_close", close, True, 1, now),
        "sma_5_close": IndicatorValue("sma_5_close", sma, True, 5, now),
        "momentum_5_close": IndicatorValue("momentum_5_close", momentum, True, 6, now),
        "rolling_dollar_volume_20": IndicatorValue("rolling_dollar_volume_20", liquidity, True, 20, now),
        "stddev_20_close": IndicatorValue("stddev_20_close", volatility, True, 20, now),
    }


def test_momentum_universe_selection_picks_top_n_and_rejects_negative_momentum():
    universe = _universe()
    context = UniverseSelectionContext(
        sleeve_id="swing-kor",
        universe=universe,
        indicator_snapshot=_snapshot(),
    )

    result = MomentumUniverseSelectionModel(max_active_symbols=2).select(context)

    assert result.selected_symbols == (Symbol("000002", "KRX"), Symbol("000001", "KRX"))
    assert result.live_symbols == result.selected_symbols
    assert result.rejected["KRX:000003"] == ("momentum_not_positive",)
    assert result.candidates["KRX:000002"].selected is True
    assert result.candidates["KRX:000002"].score > result.candidates["KRX:000004"].score


def test_forced_symbols_are_included_even_when_not_selected_or_not_in_coarse_universe():
    universe = _universe()
    held = Symbol("999999", "KRX")
    open_order = Symbol("000004", "KRX")
    previous = (Symbol("000001", "KRX"), Symbol("000003", "KRX"))
    context = UniverseSelectionContext(
        sleeve_id="swing-kor",
        universe=universe,
        indicator_snapshot=_snapshot(),
        previous_live_symbols=previous,
        held_symbols=(held,),
        open_order_symbols=(open_order,),
    )

    result = MomentumUniverseSelectionModel(max_active_symbols=1).select(context)

    assert result.selected_symbols == (Symbol("000002", "KRX"),)
    assert result.forced_symbols == (held, open_order)
    assert result.live_symbols == (Symbol("000002", "KRX"), held, open_order)
    assert held in result.added_symbols
    assert Symbol("000003", "KRX") in result.removed_symbols
    assert result.candidates[held.key].forced is True
    assert result.candidates[open_order.key].forced is True


def test_selection_result_can_build_active_universe_definition():
    universe = _universe()
    context = UniverseSelectionContext(
        sleeve_id="swing-kor",
        universe=universe,
        indicator_snapshot=_snapshot(),
    )

    result = MomentumUniverseSelectionModel(max_active_symbols=2).select(context)
    active_universe = result.to_universe_definition(universe)

    assert active_universe.id == "coarse-test-active"
    assert active_universe.symbols == result.live_symbols
    assert active_universe.indicators == universe.indicators
    assert "active" in active_universe.tags


def test_static_universe_selection_keeps_forced_symbols_after_static_top_n():
    universe = _universe()
    forced = Symbol("000004", "KRX")
    context = UniverseSelectionContext(
        sleeve_id="swing-kor",
        universe=universe,
        held_symbols=(forced,),
    )

    result = StaticUniverseSelectionModel(max_active_symbols=2).select(context)

    assert result.selected_symbols == (Symbol("000001", "KRX"), Symbol("000002", "KRX"))
    assert result.live_symbols == (Symbol("000001", "KRX"), Symbol("000002", "KRX"), forced)


def test_universe_selection_runtime_returns_active_universe_definition_with_forced_symbols():
    universe = _universe()
    runtime = UniverseSelectionRuntime(
        coarse_universe=universe,
        selection_model=StaticUniverseSelectionModel(max_active_symbols=2),
    )

    result = runtime.select_active(
        sleeve_id="swing-kor",
        held_symbols=(Symbol("000004", "KRX"),),
        active_universe_id="active-test",
    )

    assert result.selection.selected_symbols == (Symbol("000001", "KRX"), Symbol("000002", "KRX"))
    assert result.active_universe.id == "active-test"
    assert result.active_universe.symbols == (
        Symbol("000001", "KRX"),
        Symbol("000002", "KRX"),
        Symbol("000004", "KRX"),
    )

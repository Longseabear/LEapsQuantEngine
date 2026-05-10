from datetime import datetime

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.backtesting import VirtualMarketDataProvider, run_framework_backtest
from leaps_quant_engine.framework import FrameworkRunner, PassThroughRiskManagementModel
from leaps_quant_engine.fundamentals import PointInTimeFundamentalStore
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.definition import IndicatorDefinition, UniverseDefinition


class ValueAlpha:
    alpha_id = "value-alpha"
    version = "1.0"

    def generate(self, context):
        insights = []
        for symbol_key in context.symbol_keys:
            per = context.fundamental(symbol_key, "per")
            close = context.value(symbol_key, "close")
            if per is None or close is None or per >= 10:
                continue
            insights.append(
                Insight(
                    sleeve_id=context.sleeve_id,
                    symbol=context.symbol(symbol_key),
                    direction=InsightDirection.UP,
                    generated_at=context.as_of,
                    source_snapshot_id=context.source_snapshot_id,
                    alpha_id=self.alpha_id,
                    alpha_version=self.version,
                    confidence=0.7,
                    reason="low_per",
                    metadata={"per": per},
                )
            )
        return insights


def test_point_in_time_fundamental_store_uses_latest_known_value_only():
    symbol = Symbol("005930", "KRX")
    store = PointInTimeFundamentalStore()
    store.add(symbol, "PER", 12.0, as_of=datetime(2024, 3, 31), source="sample")
    store.add(symbol, "PER", 8.0, as_of=datetime(2025, 3, 31), source="sample")

    assert store.latest(symbol, "per", as_of=datetime(2024, 3, 30)) is None
    assert store.latest(symbol, "per", as_of=datetime(2024, 4, 1)).value == 12.0
    assert store.latest(symbol, "per", as_of=datetime(2025, 4, 1)).value == 8.0


def test_snapshot_context_reads_fundamental_values():
    symbol = Symbol("005930", "KRX")
    as_of = datetime(2026, 5, 8)
    store = PointInTimeFundamentalStore()
    store.add(symbol, "PER", 9.5, as_of=datetime(2026, 3, 31), reported_at=datetime(2026, 3, 31))
    fundamental_snapshot = store.snapshot(
        sleeve_id="LEaps",
        universe_id="value-universe",
        symbols=(symbol,),
        as_of=as_of,
        names=("per",),
        created_at=as_of,
    )
    indicator_snapshot = _indicator_snapshot(symbol, as_of)

    cycle = FrameworkRunner(
        sleeve_id="LEaps",
        alpha_runtime=AlphaRuntime(active_models=(ValueAlpha(),)),
        risk_model=PassThroughRiskManagementModel(),
    ).run_once(
        indicator_snapshot=indicator_snapshot,
        fundamental_snapshot=fundamental_snapshot,
        data=_data_slice(symbol, as_of),
        portfolio=Portfolio(cash=1_000),
    )

    assert cycle.new_insight_batch.insights[0].metadata["per"] == 9.5
    assert cycle.order_intents[0].sleeve_id == "LEaps"


def test_framework_backtest_passes_point_in_time_fundamentals_to_alpha():
    symbol = Symbol("005930", "KRX")
    store = PointInTimeFundamentalStore()
    store.add(symbol, "per", 15.0, as_of=datetime(2024, 1, 1))
    store.add(symbol, "per", 8.0, as_of=datetime(2024, 1, 3))
    universe = UniverseDefinition(
        id="value-universe",
        market="KRX",
        symbols=(symbol,),
        indicators=(IndicatorDefinition(name="close", type="close", period=1),),
    )
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2024, 1, 2), 100.0, 100.0, 100.0, 100.0, 1000),
            Bar(symbol, datetime(2024, 1, 4), 100.0, 100.0, 100.0, 100.0, 1000),
        ]
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="LEaps",
        framework_runner=FrameworkRunner(
            sleeve_id="LEaps",
            alpha_runtime=AlphaRuntime(active_models=(ValueAlpha(),)),
            risk_model=PassThroughRiskManagementModel(),
        ),
        portfolio=Portfolio(cash=1_000),
        fundamental_store=store,
        fundamental_names=("per",),
    )

    assert result.insight_count == 1
    assert result.orders[0].sleeve_id == "LEaps"
    assert result.orders[0].quantity == 10


def _indicator_snapshot(symbol: Symbol, as_of: datetime) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="LEaps",
        universe_id="value-universe",
        as_of=as_of,
        created_at=as_of,
        symbols=(symbol.key,),
        values={
            symbol.key: {
                "close": IndicatorValue("close", 100.0, True, 1, as_of),
            }
        },
    )


def _data_slice(symbol: Symbol, as_of: datetime) -> DataSlice:
    return DataSlice(
        time=as_of,
        bars={symbol.key: Bar(symbol, as_of, 100.0, 100.0, 100.0, 100.0, 1000)},
    )

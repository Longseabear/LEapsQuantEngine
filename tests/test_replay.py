from datetime import datetime

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel, FrameworkRunner
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.replay import MarketReplayStore
from leaps_quant_engine.universe.definition import IndicatorDefinition, UniverseDefinition


class OneShotAlpha:
    alpha_id = "one-shot"
    version = "1.0"

    def generate(self, context):
        symbol = context.symbol(context.symbol_keys[0])
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=symbol,
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                weight=0.5,
            )
        ]


def _slice(time: datetime, symbol: Symbol, close: float) -> DataSlice:
    bar = Bar(symbol, time, close, close, close, close, 100)
    return DataSlice(time=time, bars={symbol.key: bar})


def test_market_replay_store_records_and_replays_data_slices(tmp_path):
    symbol = Symbol("005930", "KRX")
    store = MarketReplayStore(root=tmp_path / "replay")
    first = _slice(datetime(2026, 5, 8), symbol, 100.0)
    second = _slice(datetime(2026, 5, 9), symbol, 110.0)
    universe = UniverseDefinition(
        id="replay-test",
        market="KRX",
        symbols=(symbol,),
        indicators=(IndicatorDefinition(name="close", type="close", period=1),),
    )

    store.write_data_slices("2026-05-09", "LEaps", [second, first])
    loaded = store.load_data_slices("2026-05-09", "LEaps")
    result = store.run_framework_replay(
        "2026-05-09",
        "LEaps",
        universe=universe,
        framework_runner=FrameworkRunner(
            sleeve_id="LEaps",
            alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(),)),
            portfolio_model=EqualWeightPortfolioConstructionModel(),
        ),
        portfolio=Portfolio(cash=1_000),
    )

    assert [data.time for data in loaded] == [first.time, second.time]
    assert result.data_slice_count == 2
    assert result.order_count == 2
    assert result.orders[0].quantity == 5
    assert result.final_quantity == {"KRX:005930": 4}


def test_market_replay_store_records_agent_status(tmp_path):
    store = MarketReplayStore(root=tmp_path / "replay")

    path = store.append_engine_status("2026-05-09", "LEaps", {"event": "engine_status", "orders": 1})

    assert path.read_text(encoding="utf-8").strip() == '{"event":"engine_status","orders":1}'

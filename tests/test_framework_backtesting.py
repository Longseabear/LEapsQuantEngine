from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.backtesting import VirtualMarketDataProvider, run_framework_backtest
from leaps_quant_engine.framework import FrameworkRunner
from leaps_quant_engine.models import Bar, OrderSide, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.universe.loader import parse_universe_definition


class EntryThenFlatAlpha:
    alpha_id = "entry-then-flat"
    version = "1.0"

    def __init__(self, exit_at: datetime):
        self.exit_at = exit_at

    def generate(self, context):
        symbol = context.symbol(context.symbol_keys[0])
        direction = InsightDirection.FLAT if context.as_of >= self.exit_at else InsightDirection.UP
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=symbol,
                direction=direction,
                generated_at=context.as_of,
                expires_at=context.as_of + timedelta(days=1),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                weight=0.5 if direction is InsightDirection.UP else 0.0,
                reason="framework_backtest_test",
            )
        ]


def _bar(symbol: Symbol, day: int, close: float) -> Bar:
    return Bar(
        symbol=symbol,
        time=datetime(2026, 1, 1) + timedelta(days=day),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1000,
    )


def test_framework_backtest_replays_indicators_alpha_and_metrics():
    symbol = Symbol("005930", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-backtest",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    provider = VirtualMarketDataProvider.from_bars(
        [
            _bar(symbol, 0, 100.0),
            _bar(symbol, 1, 100.0),
            _bar(symbol, 2, 108.0),
        ]
    )
    runner = FrameworkRunner(
        sleeve_id="framework-kor",
        alpha_runtime=AlphaRuntime(active_models=(EntryThenFlatAlpha(datetime(2026, 1, 3)),)),
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="framework-kor",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
    )

    assert result.data_slice_count == 3
    assert result.indicator_snapshot_count == 3
    assert [cycle.indicator_snapshot_id.startswith("indicator-") for cycle in result.framework_cycles] == [True] * 3
    assert [cycle.indicator_snapshot_id for cycle in result.framework_cycles]
    assert [cycle.new_insight_batch.insight_count for cycle in result.framework_cycles] == [1, 1, 1]
    assert [(order.side, order.quantity, order.reference_price) for order in result.orders] == [
        (OrderSide.BUY, 5, 100.0),
        (OrderSide.SELL, 5, 108.0),
    ]
    assert result.final_cash == pytest.approx(1_040.0)
    assert result.final_quantity == {}
    assert result.metrics.total_return == pytest.approx(0.04)
    assert result.metrics.trade_count == 1
    assert result.metrics.order_count == 2
    assert result.to_report(include_orders=False)["order_count"] == 2


def test_framework_backtest_preserves_replay_times_on_snapshots():
    symbol = Symbol("005930", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-backtest",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    provider = VirtualMarketDataProvider.from_bars([_bar(symbol, 1, 100.0), _bar(symbol, 0, 99.0)])
    runner = FrameworkRunner(
        sleeve_id="framework-kor",
        alpha_runtime=AlphaRuntime(active_models=(EntryThenFlatAlpha(datetime(2026, 1, 3)),)),
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="framework-kor",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
    )

    assert [cycle.new_insight_batch.insights[0].generated_at for cycle in result.framework_cycles] == [
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
    ]
    assert result.start == datetime(2026, 1, 1)
    assert result.end == datetime(2026, 1, 2)

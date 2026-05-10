from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.backtesting import (
    BacktestMetrics,
    VirtualMarketDataProvider,
    build_minute_replay_feed_from_bars,
    load_minute_replay_feed,
    run_framework_backtest,
    run_framework_replay,
    simulated_fill_model_for_slippage_bps,
    universe_with_default_indicator_resolution,
)
from leaps_quant_engine.framework import FrameworkRunner
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar, OrderSide, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.selection import build_universe_selection_result


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


class SecondSymbolSelectionModel:
    selection_id = "second-only"

    def select(self, context):
        selected = (context.universe.symbols[1],)
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates={},
            rejected={},
        )


class ReadyMomentumAlpha:
    alpha_id = "ready-momentum"
    version = "1.0"

    def generate(self, context):
        symbol_key = context.symbol_keys[0]
        momentum = context.value(symbol_key, "momentum_5_close")
        if momentum is None:
            return []
        symbol = context.symbol(symbol_key)
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=symbol,
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + timedelta(days=1),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                weight=0.5,
                score=momentum,
                reason="ready_momentum_test",
            )
        ]


class CloseValueAlpha:
    alpha_id = "close-value"
    version = "1.0"

    def generate(self, context):
        symbol_key = context.symbol_keys[0]
        close = context.value(symbol_key, "close")
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + timedelta(days=1),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
                weight=1.0,
                reason="close_value",
                metadata={"close": close},
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


def test_backtest_metrics_report_marks_cross_currency_aggregate_without_fx():
    metrics = BacktestMetrics(
        initial_equity=10_000_100,
        final_equity=10_100_105,
        total_return=0.01,
        cagr=0.01,
        sharpe=1.0,
        mdd=0.02,
        turnover=0.3,
        avg_holding_days=5.0,
        avg_exposure=0.4,
        win_rate=0.5,
        trade_count=2,
        order_count=4,
    )

    report = metrics.to_report(currency_mode="multi_currency_native_sum", valid_without_fx=False)

    assert report["valid_without_fx"] is False
    assert report["currency_mode"] == "multi_currency_native_sum"
    assert report["warning"] == "cross_currency_metrics_are_native_currency_sums_without_fx"


def test_backtest_metrics_report_can_mark_empty_currency_scope():
    metrics = BacktestMetrics(
        initial_equity=0.0,
        final_equity=0.0,
        total_return=0.0,
        cagr=0.0,
        sharpe=0.0,
        mdd=0.0,
        turnover=0.0,
        avg_holding_days=0.0,
        avg_exposure=0.0,
        win_rate=0.0,
        trade_count=0,
        order_count=0,
    )

    report = metrics.to_report(currency_mode="no_currency", valid_without_fx=True)

    assert report["currency_mode"] == "no_currency"
    assert report["valid_without_fx"] is True
    assert "warning" not in report


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
        (OrderSide.BUY, 10, 100.0),
        (OrderSide.SELL, 10, 108.0),
    ]
    assert result.final_cash == pytest.approx(1_080.0)
    assert result.final_quantity == {}
    assert result.metrics.total_return == pytest.approx(0.08)
    assert result.metrics.trade_count == 1
    assert result.metrics.order_count == 2
    assert result.to_report(include_orders=False)["order_count"] == 2


def test_minute_replay_feed_loader_groups_csv_rows_and_marks_resolution(tmp_path):
    feed_path = tmp_path / "minute.csv"
    feed_path.write_text(
        "\n".join(
            [
                "symbol,time,open,high,low,close,volume",
                "US:SPY,2026-05-01T09:30:00,100,101,99,100.5,1000",
                "US:QQQ,2026-05-01T09:30:00,200,201,199,200.5,2000",
                "US:SPY,2026-05-01T09:31:00,101,102,100,101.5,1100",
            ]
        ),
        encoding="utf-8",
    )
    universe = parse_universe_definition(
        {
            "id": "minute-feed",
            "market": "US",
            "symbols": ["SPY", "QQQ"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )

    feed = load_minute_replay_feed(feed_path, universe=universe)

    assert len(feed) == 2
    assert feed[0].resolution == "minute"
    assert set(feed[0].bars) == {"US:SPY", "US:QQQ"}
    assert feed[0].bars["US:SPY"].resolution == "minute"
    assert feed[1].bars["US:SPY"].close == 101.5


def test_universe_with_default_indicator_resolution_keeps_explicit_minute():
    universe = parse_universe_definition(
        {
            "id": "minute-resolution",
            "market": "US",
            "symbols": ["SPY"],
            "indicators": [
                {"name": "daily_close", "type": "close", "period": 1},
                {"name": "minute_close", "type": "close", "period": 1, "resolution": "minute"},
            ],
        }
    )

    resolved = universe_with_default_indicator_resolution(universe, default_resolution="daily")

    assert [indicator.resolution for indicator in resolved.indicators] == ["daily", "minute"]


def test_minute_replay_does_not_advance_default_daily_indicator():
    symbol = Symbol("SPY", "US")
    universe = parse_universe_definition(
        {
            "id": "minute-daily-gate",
            "market": "US",
            "symbols": ["SPY"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    universe = universe_with_default_indicator_resolution(universe, default_resolution="daily")
    indicator_engine = IndicatorEngine()
    indicator_engine.register_universe("us", universe)
    indicator_engine.warm_up(
        "us",
        [Bar(symbol, datetime(2026, 4, 30), 100, 100, 100, 100, 1000, resolution="daily")],
    )
    feed = build_minute_replay_feed_from_bars(
        [Bar(symbol, datetime(2026, 5, 1, 9, 30), 200, 200, 200, 200, 1000, resolution="minute")]
    )
    runner = FrameworkRunner(
        sleeve_id="us",
        alpha_runtime=AlphaRuntime(active_models=(CloseValueAlpha(),)),
    )

    result = run_framework_replay(
        feed,
        universe,
        sleeve_id="us",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
        indicator_engine=indicator_engine,
    )

    insight = result.framework_cycles[0].new_insight_batch.insights[0]
    assert insight.metadata["close"] == 100.0
    assert indicator_engine.value("us", symbol, "close") == 100.0


def test_framework_backtest_report_can_include_insight_ledger_without_orders():
    symbol = Symbol("005930", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-backtest-debug",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    provider = VirtualMarketDataProvider.from_bars(
        [
            _bar(symbol, 0, 100.0),
            _bar(symbol, 1, 101.0),
        ]
    )
    runner = FrameworkRunner(
        sleeve_id="framework-kor",
        alpha_runtime=AlphaRuntime(active_models=(EntryThenFlatAlpha(datetime(2026, 1, 10)),)),
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="framework-kor",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
    )

    report = result.to_report(include_orders=False, include_insights=True)

    assert "orders" not in report
    assert report["insights"]["cycle_count"] == 2
    assert report["insights"]["insight_count"] == 2
    assert report["insights"]["cycles"][0]["new_insights"][0]["alpha_id"] == "entry-then-flat"
    assert report["insights"]["cycles"][0]["active_insight_count"] == 1


def test_framework_backtest_warms_indicators_before_evaluation_start():
    symbol = Symbol("005930", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-backtest-warmup",
            "market": "KRX",
            "symbols": ["005930"],
            "indicators": [{"name": "momentum_5_close", "type": "momentum", "period": 5}],
        }
    )
    provider = VirtualMarketDataProvider.from_bars(
        [_bar(symbol, index, 100.0 + index) for index in range(7)]
    )
    runner = FrameworkRunner(
        sleeve_id="framework-kor",
        alpha_runtime=AlphaRuntime(active_models=(ReadyMomentumAlpha(),)),
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="framework-kor",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
        warmup_start=datetime(2026, 1, 1),
        start=datetime(2026, 1, 6),
        end=datetime(2026, 1, 7),
    )

    assert result.warmup_data_slice_count == 5
    assert result.data_slice_count == 2
    assert result.indicator_snapshot_count == 2
    assert result.start == datetime(2026, 1, 6)
    assert result.orders
    assert all(order.symbol == symbol for order in result.orders)


def test_framework_backtest_reports_simulated_slippage_cost():
    symbol = Symbol("005930", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-backtest-slippage",
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
        fill_model=simulated_fill_model_for_slippage_bps(100),
    )

    assert result.final_cash == pytest.approx(1051.28)
    assert result.metrics.slippage_cost == pytest.approx(20.72)
    assert result.metrics.slippage_bps == pytest.approx(100.0)
    report = result.to_report(include_orders=False)
    assert report["metrics"]["slippage_cost"] == pytest.approx(20.72)
    assert report["metrics_by_currency"]["KRW"]["slippage_bps"] == pytest.approx(100.0)


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


def test_framework_backtest_scopes_alpha_inputs_from_selection_model():
    first = Symbol("005930", "KRX")
    second = Symbol("000660", "KRX")
    universe = parse_universe_definition(
        {
            "id": "framework-selection-backtest",
            "market": "KRX",
            "symbols": ["005930", "000660"],
            "indicators": [{"name": "close", "type": "close", "period": 1}],
        }
    )
    provider = VirtualMarketDataProvider.from_bars(
        [
            _bar(first, 0, 100.0),
            _bar(second, 0, 200.0),
            _bar(first, 1, 100.0),
            _bar(second, 1, 200.0),
        ]
    )
    runner = FrameworkRunner(
        sleeve_id="framework-kor",
        alpha_runtime=AlphaRuntime(active_models=(EntryThenFlatAlpha(datetime(2026, 1, 10)),)),
    )

    result = run_framework_backtest(
        universe,
        provider,
        sleeve_id="framework-kor",
        framework_runner=runner,
        portfolio=Portfolio(cash=1_000),
        selection_models=(SecondSymbolSelectionModel(),),
        alpha_input_selections={"entry-then-flat": "second-only"},
    )

    assert result.selection_results
    assert result.selection_results[-1].selected_symbols == (second,)
    assert [cycle.new_insight_batch.insights[0].symbol for cycle in result.framework_cycles] == [second, second]
    assert result.orders[0].symbol == second

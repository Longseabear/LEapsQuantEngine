from datetime import datetime

import pytest

from leaps_quant_engine.algorithm import Algorithm
from leaps_quant_engine.backtesting import VirtualMarketDataProvider, build_replay_feed, run_backtest
from leaps_quant_engine.engine import Engine
from leaps_quant_engine.examples.buy_and_hold import BuyAndHoldAlgorithm
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.portfolio import PortfolioView
from leaps_quant_engine.sleeve import Sleeve, SleevePolicy


class RoundTripAlgorithm(Algorithm):
    def __init__(self, symbol: Symbol, quantity: int, exit_at: datetime) -> None:
        self.symbol = symbol
        self.quantity = quantity
        self.exit_at = exit_at

    def on_data(self, data: DataSlice, portfolio: PortfolioView) -> list[PortfolioTarget]:
        target_quantity = 0 if data.time >= self.exit_at else self.quantity
        return [PortfolioTarget(self.symbol, target_quantity, tag="round-trip")]


class StaticTargetAlgorithm(Algorithm):
    def __init__(self, symbol: Symbol, quantity: int) -> None:
        self.symbol = symbol
        self.quantity = quantity

    def on_data(self, data: DataSlice, portfolio: PortfolioView) -> list[PortfolioTarget]:
        return [PortfolioTarget(self.symbol, self.quantity, tag="static")]


def test_virtual_market_data_provider_replays_bars_chronologically():
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2026, 5, 7), 110, 110, 110, 110, 10),
            Bar(symbol, datetime(2026, 5, 4), 100, 100, 100, 100, 10),
        ]
    )

    feed = build_replay_feed(provider, [symbol])

    assert [slice.time for slice in feed] == [datetime(2026, 5, 4), datetime(2026, 5, 7)]
    assert provider.get_latest_bar(symbol).close == 110


def test_build_replay_feed_adds_opening_gap_proxy_context():
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2026, 5, 4), 100, 105, 95, 100, 10),
            Bar(symbol, datetime(2026, 5, 7), 110, 120, 98, 115, 10),
        ]
    )

    feed = build_replay_feed(provider, [symbol])

    first = feed[0].bars[symbol.key]
    second = feed[1].bars[symbol.key]
    assert first.metadata["opening_context_source"] == "daily_ohlc_proxy"
    assert first.metadata["opening_context_available"] is False
    assert second.metadata["opening_context_available"] is True
    assert second.metadata["previous_close"] == 100.0
    assert second.metadata["opening_gap_pct"] == pytest.approx(0.10)
    assert second.metadata["open_to_close_return_pct"] == pytest.approx(115 / 110 - 1)
    assert second.metadata["open_to_low_drawdown_pct"] == pytest.approx(98 / 110 - 1)
    assert second.metadata["open_to_high_runup_pct"] == pytest.approx(120 / 110 - 1)
    assert second.metadata["gap_filled"] is True


def test_run_backtest_uses_immediate_fill_model_and_updates_portfolio_state():
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2026, 5, 4), 100, 100, 100, 100, 10),
            Bar(symbol, datetime(2026, 5, 7), 110, 110, 110, 110, 10),
        ]
    )
    sleeve = Sleeve(
        id="swing-kor",
        algorithm=BuyAndHoldAlgorithm(symbol=symbol, quantity=3),
        portfolio=Portfolio(cash=1_000),
        policy=SleevePolicy(max_position_pct=1.0),
    )

    result = run_backtest(Engine([sleeve]), provider, [symbol])

    assert len(result.orders) == 1
    assert len(result.order_tickets) == 1
    assert len([event for event in result.order_events if event.is_fill]) == 1
    assert result.final_cash_by_sleeve == {"swing-kor": 700.0}
    assert result.final_quantity_by_sleeve == {"swing-kor": {"KRX:005930": 3}}


def test_run_backtest_reports_core_performance_metrics_for_closed_trades():
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2026, 1, 1), 100, 100, 100, 100, 10),
            Bar(symbol, datetime(2026, 1, 2), 120, 120, 120, 120, 10),
            Bar(symbol, datetime(2026, 1, 3), 110, 110, 110, 110, 10),
        ]
    )
    sleeve = Sleeve(
        id="swing-kor",
        algorithm=RoundTripAlgorithm(symbol=symbol, quantity=2, exit_at=datetime(2026, 1, 3)),
        portfolio=Portfolio(cash=1_000),
        policy=SleevePolicy(max_position_pct=1.0),
    )

    result = run_backtest(Engine([sleeve]), provider, [symbol])

    metrics = result.metrics_by_sleeve["swing-kor"]
    assert metrics.initial_equity == 1_000
    assert metrics.final_equity == 1_020
    assert metrics.total_return == pytest.approx(0.02)
    assert metrics.cagr > 0
    assert metrics.sharpe != 0
    assert metrics.mdd == pytest.approx(20 / 1040)
    assert metrics.turnover == pytest.approx(420 / 1020)
    assert metrics.avg_holding_days == pytest.approx(2.0)
    assert metrics.avg_exposure == pytest.approx((0.2 + (240 / 1040) + 0.0) / 3)
    assert metrics.win_rate == 1.0
    assert metrics.trade_count == 1
    assert metrics.order_count == 2
    assert result.metrics.to_report()["trade_count"] == 1
    assert result.trades_by_sleeve["swing-kor"][0].pnl == pytest.approx(20)


def test_run_backtest_records_cross_sleeve_same_symbol_buy_sell_collision():
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [Bar(symbol, datetime(2026, 5, 4), 100, 100, 100, 100, 10)]
    )
    buy_sleeve = Sleeve(
        id="buyer",
        algorithm=StaticTargetAlgorithm(symbol=symbol, quantity=1),
        portfolio=Portfolio(cash=1_000),
        policy=SleevePolicy(max_position_pct=1.0),
    )
    sell_sleeve = Sleeve(
        id="seller",
        algorithm=StaticTargetAlgorithm(symbol=symbol, quantity=0),
        portfolio=Portfolio(
            cash=0,
            holdings={symbol.key: Holding(symbol, quantity=1, average_price=90.0)},
        ),
        policy=SleevePolicy(max_position_pct=1.0),
    )

    result = run_backtest(Engine([buy_sleeve, sell_sleeve]), provider, [symbol])

    assert [(order.sleeve_id, order.side) for order in result.orders] == [
        ("buyer", OrderSide.BUY),
        ("seller", OrderSide.SELL),
    ]
    assert len(result.order_collisions) == 1
    assert result.order_collisions[0].buy_sleeve_ids == ("buyer",)
    assert result.order_collisions[0].sell_sleeve_ids == ("seller",)
    assert result.final_quantity_by_sleeve == {"buyer": {"KRX:005930": 1}, "seller": {}}

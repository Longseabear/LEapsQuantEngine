from datetime import datetime

from leaps_quant_engine.backtesting import VirtualMarketDataProvider, build_replay_feed, run_backtest
from leaps_quant_engine.engine import Engine
from leaps_quant_engine.examples.buy_and_hold import BuyAndHoldAlgorithm
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.sleeve import Sleeve, SleevePolicy


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
    assert result.final_cash_by_sleeve == {"swing-kor": 700.0}
    assert result.final_quantity_by_sleeve == {"swing-kor": {"KRX:005930": 3}}

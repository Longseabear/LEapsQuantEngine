from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.alpha import SnapshotContext
from leaps_quant_engine.framework.risk import RiskManagementContext
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.selection import UniverseSelectionContext


ROOT = Path(__file__).resolve().parents[1]


def test_kospi_conviction_alpha_emits_krw_growth_only():
    module = _load("sleeves/LEaps/alphas/kospi_conviction.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=268_500, fast=247_000, slow=224_000, momentum=0.27, momentum_5=0.18, vol=0.07),
            "US:SPY": _values(close=700, fast=690, slow=680, momentum=0.04, momentum_5=0.02, vol=0.02),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930", "US:SPY")))

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].alpha_id == "leaps-kospi-conviction"
    assert insights[0].metadata["role"] == "krw_growth_engine"
    assert insights[0].metadata["market_breadth"] == 1.0
    assert insights[0].metadata["market_conviction_bonus"] > 0
    assert insights[0].reason == "kospi_conviction_breadth_trend_momentum"


def test_leaps_live_alphas_run_every_cycle():
    alpha_paths = (
        "sleeves/LEaps/alphas/kospi_conviction.py",
        "sleeves/LEaps/alphas/kospi_pullback_reversion.py",
        "sleeves/LEaps/alphas/volatility_trailing_stop.py",
    )

    cadences = {
        relative_path: getattr(_load(relative_path), "EVALUATION_CADENCE", None)
        for relative_path in alpha_paths
    }

    assert cadences == {
        "sleeves/LEaps/alphas/kospi_conviction.py": "every_cycle",
        "sleeves/LEaps/alphas/kospi_pullback_reversion.py": "every_cycle",
        "sleeves/LEaps/alphas/volatility_trailing_stop.py": "every_cycle",
    }


def test_leaps_rl_constructor_can_be_configured_as_complete_target_portfolio():
    module = _load("sleeves/LEaps/portfolios/rl_ppo_constructor.py")

    model = module.create_portfolio_model({"emit_zero_for_missing_held_targets": True})

    assert model.emit_zero_for_missing_held_targets is True


def test_kospi_conviction_alpha_filters_uncompensated_high_volatility():
    module = _load("sleeves/LEaps/alphas/kospi_conviction.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=100_000, fast=110_000, slow=90_000, momentum=0.25, momentum_5=0.05, vol=0.19),
            "KRX:000660": _values(close=150_000, fast=160_000, slow=120_000, momentum=0.62, momentum_5=0.15, vol=0.19),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930", "KRX:000660"))
    )

    assert [insight.symbol.key for insight in insights] == ["KRX:000660"]
    assert insights[0].metadata["volatility_filter"] == "passed"


def test_kospi_pullback_reversion_alpha_emits_uptrend_pullbacks_only():
    module = _load("sleeves/LEaps/alphas/kospi_pullback_reversion.py")
    now = datetime(2026, 5, 12)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=100_000,
                fast=103_000,
                slow=92_000,
                momentum=0.16,
                momentum_5=-0.025,
                vol=0.06,
                rolling_high=106_000,
                rolling_low=96_000,
            ),
            "KRX:000660": _values(
                close=150_000,
                fast=148_000,
                slow=155_000,
                momentum=-0.03,
                momentum_5=-0.02,
                vol=0.05,
                rolling_high=165_000,
                rolling_low=149_000,
            ),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930", "KRX:000660"))
    )

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].alpha_id == "leaps-kospi-pullback-reversion"
    assert insights[0].reason == "kospi_pullback_reversion_in_uptrend"
    assert insights[0].metadata["role"] == "krw_pullback_reversion"
    assert insights[0].metadata["pullback_depth"] > 0
    assert insights[0].metadata["momentum"] > 0


def test_us_stability_alpha_prefers_defensive_us_etfs():
    module = _load("sleeves/LEaps/alphas/us_stability_hedge.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "US:USMV": _values(close=95, fast=96, slow=94, momentum=0.03, momentum_5=0.01, vol=0.01),
            "US:SMH": _values(close=550, fast=560, slow=540, momentum=0.10, momentum_5=0.03, vol=0.07),
            "KRX:005930": _values(close=268_500, fast=247_000, slow=224_000, momentum=0.27, momentum_5=0.18, vol=0.07),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("US:USMV", "US:SMH", "KRX:005930")))

    assert insights
    assert all(insight.symbol.key.startswith("US:") for insight in insights)
    assert insights[0].symbol.key == "US:USMV"
    assert insights[0].metadata["role"] == "usd_stability_hedge"


def test_stock_momentum_selection_keeps_kospi_alpha_universe_krx_only():
    module = _load("sleeves/LEaps/selections/stock_momentum.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "mixed-test",
            "market": "MIXED",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock"},
                {"ticker": "AAPL", "market": "US", "asset_type": "stock"},
                {"ticker": "SPY", "market": "US", "asset_type": "etf"},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80_000, fast=82_000, slow=75_000, momentum=0.10, momentum_5=0.04, vol=0.03),
            "US:AAPL": _values(close=210, fast=212, slow=200, momentum=0.50, momentum_5=0.08, vol=0.04),
            "US:SPY": _values(close=620, fast=622, slow=600, momentum=0.30, momentum_5=0.06, vol=0.02),
        },
    )

    result = module.StockMomentumSelectionModel(max_active_symbols=5).select(
        UniverseSelectionContext(sleeve_id="LEaps", universe=universe, indicator_snapshot=snapshot)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:005930"]
    assert result.rejected["US:AAPL"] == ("not_krx_stock_candidate",)
    assert result.rejected["US:SPY"] == ("not_krx_stock_candidate",)


def test_stock_momentum_selection_rejects_high_volatility_without_exception():
    module = _load("sleeves/LEaps/selections/stock_momentum.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "kr-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock"},
                {"ticker": "000660", "market": "KRX", "asset_type": "stock"},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80_000, fast=82_000, slow=75_000, momentum=0.20, momentum_5=0.04, vol=0.20),
            "KRX:000660": _values(close=150_000, fast=160_000, slow=120_000, momentum=0.50, momentum_5=0.10, vol=0.20),
        },
    )

    result = module.StockMomentumSelectionModel(max_active_symbols=5).select(
        UniverseSelectionContext(sleeve_id="LEaps", universe=universe, indicator_snapshot=snapshot)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:000660"]
    assert result.rejected["KRX:005930"] == ("volatility_filter",)


def test_kospi_growth_us_hedge_risk_applies_currency_limits():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 8)
    kr = Symbol("005930", "KRX")
    us = Symbol("USMV", "US")
    data = DataSlice(
        time=now,
        bars={
            kr.key: Bar(kr, now, 100_000, 100_000, 100_000, 100_000, 1000),
            us.key: Bar(us, now, 100, 100, 100, 100, 1000),
        },
    )
    portfolio = Portfolio(cash=0, cash_by_currency={"KRW": 1_000_000, "USD": 1_000})
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 0.4, "USD": 0.3},
            "max_total_exposure_pct_by_currency": {"KRW": 0.95, "USD": 0.65},
            "cash_buffer_pct_by_currency": {"KRW": 0.0, "USD": 0.0},
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(kr, 10), PortfolioTarget(us, 100)),
        )
    )

    approved = {target.symbol.key: target.quantity for target in batch.approved_targets}
    assert approved == {"KRX:005930": 4, "US:USMV": 3}


def test_kospi_growth_risk_raises_exposure_cap_in_strong_regime():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 8)
    samsung = Symbol("005930", "KRX")
    hynix = Symbol("000660", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(samsung, now, 100_000, 100_000, 100_000, 100_000, 1000),
            hynix.key: Bar(hynix, now, 100_000, 100_000, 100_000, 100_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 0.50},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"strong_risk_on": 0.80}},
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(samsung, 5), PortfolioTarget(hynix, 5)),
            active_insights=(
                _regime_insight(samsung, now, breadth=0.60, momentum=0.20, volatility=0.10),
                _regime_insight(hynix, now, breadth=0.60, momentum=0.22, volatility=0.09),
            ),
        )
    )

    approved = {target.symbol.key: target.quantity for target in batch.approved_targets}
    assert approved == {"KRX:005930": 5, "KRX:000660": 3}
    assert batch.decisions[0].metadata["market_regime"]["name"] == "strong_risk_on"
    assert batch.decisions[0].metadata["max_total_exposure_pct"] == 0.80


def test_kospi_growth_risk_treats_narrow_leadership_momentum_as_risk_on():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 14)
    samsung = Symbol("005930", "KRX")
    samsung_ct = Symbol("028260", "KRX")
    hyundai = Symbol("005380", "KRX")
    kia = Symbol("000270", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(samsung, now, 298_000, 298_000, 298_000, 298_000, 1000),
            samsung_ct.key: Bar(samsung_ct, now, 443_500, 443_500, 443_500, 443_500, 1000),
            hyundai.key: Bar(hyundai, now, 713_000, 713_000, 713_000, 713_000, 1000),
            kia.key: Bar(kia, now, 179_000, 179_000, 179_000, 179_000, 1000),
        },
    )
    portfolio = Portfolio(
        cash=4_830_406,
        cash_by_currency={"KRW": 4_830_406},
        holdings={
            samsung.key: Holding(samsung, quantity=10, average_price=274_552),
            samsung_ct.key: Holding(samsung_ct, quantity=4, average_price=429_500),
            hyundai.key: Holding(hyundai, quantity=2, average_price=682_519),
            kia.key: Holding(kia, quantity=5, average_price=174_920),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 0.26},
            "max_total_exposure_pct_by_currency": {"KRW": 0.68},
            "cash_buffer_pct_by_currency": {"KRW": 0.10},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {
                "KRW": {
                    "neutral": 0.60,
                    "risk_on": 0.78,
                    "strong_risk_on": 0.95,
                }
            },
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(samsung_ct, 7),),
            active_insights=(
                _regime_insight(samsung, now, breadth=0.3125, momentum=0.46, volatility=0.10),
                _regime_insight(samsung_ct, now, breadth=0.3125, momentum=0.47, volatility=0.11),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.metadata["market_regime"]["name"] == "risk_on"
    assert decision.metadata["market_regime"]["trigger"] == "narrow_leadership_strong_momentum"
    assert decision.metadata["max_total_exposure_pct"] == 0.78
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 6


def test_kospi_growth_risk_explains_when_exposure_cap_has_no_room():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 13)
    samsung = Symbol("005930", "KRX")
    hyundai = Symbol("005380", "KRX")
    existing = Symbol("000660", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(samsung, now, 279_000, 279_000, 279_000, 279_000, 1000),
            hyundai.key: Bar(hyundai, now, 646_000, 646_000, 646_000, 646_000, 1000),
            existing.key: Bar(existing, now, 396_410, 396_410, 396_410, 396_410, 1000),
        },
    )
    portfolio = Portfolio(
        cash=4_561_806,
        cash_by_currency={"KRW": 4_561_806},
        holdings={
            samsung.key: Holding(samsung, quantity=3, average_price=279_000),
            hyundai.key: Holding(hyundai, quantity=3, average_price=646_000),
            existing.key: Holding(existing, quantity=10, average_price=396_410),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 0.26},
            "max_total_exposure_pct_by_currency": {"KRW": 0.68},
            "cash_buffer_pct_by_currency": {"KRW": 0.10},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"neutral": 0.60}},
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(samsung, 11), PortfolioTarget(hyundai, 5)),
        )
    )

    assert [decision.reason for decision in batch.decisions] == [
        "exposure_limit_no_room",
        "exposure_limit_no_room",
    ]
    assert batch.decisions[0].metadata["exposure_limited_quantity"] == 3
    assert batch.decisions[0].metadata["market_regime"]["name"] == "neutral"


def test_leaps_execution_tags_orders_by_currency():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model({"tag_prefix": "leaps"})

    orders = model.create_orders("LEaps", Portfolio(cash=1_000_000), data, [PortfolioTarget(symbol, 3, tag="alpha")])

    assert len(orders) == 1
    assert orders[0].side is OrderSide.BUY
    assert orders[0].tag == "leaps:krw:alpha"
    assert orders[0].metadata["execution_style"] == "leaps_momentum"


def test_leaps_execution_slices_and_prices_momentum_entries():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 101_000, 99_000, 100_000, 1_000_000)})
    model = module.create_execution_model(
        {
            "tag_prefix": "leaps",
            "buy_limit_offset_bps": 10,
            "max_slice_notional": 250_000,
            "max_slices": 3,
        }
    )

    orders = model.create_orders("LEaps", Portfolio(cash=2_000_000), data, [PortfolioTarget(symbol, 7, tag="entry")])

    assert [order.quantity for order in orders] == [2, 2, 2]
    assert all(round(order.limit_price or 0, 6) == 100_100 for order in orders)
    assert orders[0].metadata["slice_count"] == 3
    assert orders[0].metadata["deferred_quantity"] == 1


def test_leaps_execution_chase_guard_reduces_overextended_buy_size():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("000660", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 112_000, 99_000, 111_000, 1_000_000)})
    model = module.create_execution_model(
        {
            "chase_guard_intraday_return_bps": 900,
            "chase_guard_size_multiplier": 0.5,
            "max_slice_notional": 10_000_000,
        }
    )

    orders = model.create_orders("LEaps", Portfolio(cash=2_000_000), data, [PortfolioTarget(symbol, 5, tag="entry")])

    assert [order.quantity for order in orders] == [2]
    assert orders[0].metadata["chase_guard"] == "reduced_size"


def test_leaps_execution_uses_more_aggressive_limit_for_stop_sells():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)})
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=10, average_price=100_000)})
    model = module.create_execution_model({"stop_sell_limit_offset_bps": 50})

    orders = model.create_orders("LEaps", portfolio, data, [PortfolioTarget(symbol, 0, tag="volatility_stop")])

    assert len(orders) == 1
    assert orders[0].side is OrderSide.SELL
    assert orders[0].limit_price == 99_500
    assert orders[0].metadata["limit_offset_bps"] == 50


def test_leaps_execution_rounds_domestic_limit_prices_to_krx_tick():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("006400", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 634_000, 634_000, 634_000, 634_000, 1_000_000)})
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=1, average_price=675_000)})
    model = module.create_execution_model({"sell_limit_offset_bps": 15})

    orders = model.create_orders("LEaps", portfolio, data, [PortfolioTarget(symbol, 0, tag="exit")])

    assert len(orders) == 1
    assert orders[0].limit_price == 633_000


def test_leaps_execution_reduces_entries_in_extended_session():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 8, 35)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)})
    model = module.create_execution_model(
        {
            "extended_session_buy_multiplier": 0.3,
            "max_slice_notional": 10_000_000,
        }
    )

    orders = model.create_orders(
        "LEaps",
        Portfolio(cash=2_000_000),
        data,
        [PortfolioTarget(symbol, 10, tag="entry")],
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="pre_open_after_hours",
            is_orderable=True,
            is_regular_market_open=False,
            source="test",
        ),
    )

    assert [order.quantity for order in orders] == [3]
    assert orders[0].metadata["session_policy"] == "extended_session"
    assert orders[0].metadata["session_quantity_multiplier"] == 0.3
    assert orders[0].metadata["session_quantity_clamp"] == "reduced_size"


def test_leaps_execution_keeps_exit_size_in_after_hours_close():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 15, 45)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)})
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=10, average_price=100_000)})
    model = module.create_execution_model({"max_slice_notional": 10_000_000})

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        [PortfolioTarget(symbol, 0, tag="no_longer_in_target_portfolio")],
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="after_hours_close",
            is_orderable=True,
            is_regular_market_open=False,
            source="test",
        ),
    )

    assert [order.quantity for order in orders] == [10]
    assert orders[0].side is OrderSide.SELL
    assert orders[0].metadata["session_policy"] == "extended_session"
    assert orders[0].metadata["session_quantity_multiplier"] == 1.0


def test_leaps_execution_blocks_after_hours_single_price_by_default():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 16, 5)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)})
    model = module.create_execution_model({})

    orders = model.create_orders(
        "LEaps",
        Portfolio(cash=2_000_000),
        data,
        [PortfolioTarget(symbol, 3, tag="entry")],
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="after_hours_single_price",
            is_orderable=True,
            is_regular_market_open=False,
            source="test",
        ),
    )

    assert orders == []


def _snapshot(now: datetime, values: dict[str, dict[str, float]]) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="LEaps",
        universe_id="test",
        as_of=now,
        created_at=now,
        symbols=tuple(values),
        values={
            symbol: {
                name: IndicatorValue(name=name, value=value, is_ready=True, samples=30, time=now)
                for name, value in indicator_values.items()
            }
            for symbol, indicator_values in values.items()
        },
        source_snapshot_id="test",
    )


def _values(
    *,
    close: float,
    fast: float,
    slow: float,
    momentum: float,
    momentum_5: float,
    vol: float,
    rolling_high: float | None = None,
    rolling_low: float | None = None,
) -> dict[str, float]:
    return {
        "close": close,
        "identity_close": close,
        "ema_8_close": fast,
        "sma_20_close": slow,
        "roc_20_close": momentum,
        "momentum_5_close": momentum_5,
        "stddev_20_close": close * vol,
        "atr_14": close * vol,
        "rolling_max_20_close": rolling_high if rolling_high is not None else close * 1.1,
        "rolling_min_20_close": rolling_low if rolling_low is not None else close * 0.9,
        "rolling_dollar_volume_20": 5_000_000_000,
        "volume": 1000,
    }


def _regime_insight(symbol: Symbol, now: datetime, *, breadth: float, momentum: float, volatility: float) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        source_snapshot_id="test",
        alpha_id="leaps-kospi-conviction",
        alpha_version="0.1.0",
        metadata={
            "market_breadth": breadth,
            "momentum": momentum,
            "volatility": volatility,
        },
    )


def _load(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

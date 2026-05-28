from __future__ import annotations

from datetime import datetime
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys

from leaps_quant_engine.alpha import InsightDirection, SnapshotContext
from leaps_quant_engine.framework.portfolio_construction import PortfolioConstructionContext
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition


ROOT = Path(__file__).resolve().parents[1]


def test_kr_domestic_4401_runtime_uses_core_regime_alpha_and_expanded_universe():
    snapshot = load_runtime_config_snapshot(ROOT / "configs" / "runtime" / "kr_domestic_4401_sleeve.json")
    sleeve = snapshot.config.sleeve("kr-domestic-4401")
    universe_payload = json.loads((ROOT / "configs" / "universes" / "kr_domestic_4401_core.json").read_text(encoding="utf-8"))
    universe = parse_universe_definition(universe_payload)

    assert sleeve.workspace_path == Path("sleeves/kr-domestic-4401")
    assert [module.ref for module in sleeve.alpha.modules] == ["alphas/core_regime_allocator.py"]
    assert sleeve.portfolio.model.ref == "portfolios/regime_inverse_vol.py"
    assert sleeve.portfolio.rebalance.cadence == "daily_at 09:05 Asia/Seoul"
    assert sleeve.portfolio.parameters["whole_share_floor_enabled"] is True
    assert sleeve.portfolio.parameters["whole_share_floor_min_fraction"] == 0.35
    assert sleeve.portfolio.rebalance.min_order_notional == 50000.0
    assert sleeve.portfolio.rebalance.min_order_notional_equity_bps == 50.0
    assert sleeve.portfolio.rebalance.min_quantity_delta == 1
    assert sleeve.portfolio.rebalance.target_churn_guard is True
    assert sleeve.portfolio.rebalance.target_churn_max_quantity_delta == 2
    assert sleeve.portfolio.rebalance.target_churn_lot_fraction == 1.0
    assert sleeve.portfolio.rebalance.target_churn_equity_bps == 50.0
    assert sleeve.portfolio.rebalance.whole_share_entry_floor_min_fraction == 0.35
    assert sleeve.portfolio.rebalance.reused_target_churn_guard is True
    assert sleeve.portfolio.rebalance.reused_target_churn_max_quantity_delta == 2
    assert sleeve.portfolio.rebalance.reused_target_churn_lot_fraction == 1.0
    assert sleeve.portfolio.rebalance.reused_target_churn_equity_bps == 50.0
    assert dict(sleeve.alpha.input_selections) == {
        "kr-domestic-4401-core-regime": "kr-domestic-4401-watchlist",
    }
    assert sleeve.universe.active.max_symbols == 48
    assert len(universe.symbols) >= 40
    assert "KRX:102780" not in {symbol.key for symbol in universe.symbols}
    assert "KRX:395160" not in {symbol.key for symbol in universe.symbols}
    assert {symbol.key for symbol in universe.symbols} >= {
        "KRX:069500",
        "KRX:488770",
        "KRX:357870",
        "KRX:114800",
        "KRX:005930",
        "KRX:105560",
    }


def test_core_regime_alpha_allocates_to_risk_assets_in_strong_regime():
    module = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    now = datetime(2026, 5, 22, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=39000, sma20=37400, sma60=36000, momentum_20=0.055, momentum_60=0.090, vol=0.030),
            "KRX:102110": _values(close=41000, sma20=39500, sma60=38200, momentum_20=0.052, momentum_60=0.084, vol=0.032),
            "KRX:005930": _values(close=82000, sma20=79000, sma60=76000, momentum_20=0.065, momentum_60=0.110, vol=0.036),
            "KRX:105560": _values(close=89000, sma20=86000, sma60=82500, momentum_20=0.050, momentum_60=0.070, vol=0.028),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002),
            "KRX:153130": _values(close=113000, sma20=112900, sma60=112700, momentum_20=0.002, momentum_60=0.005, vol=0.002),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy"},
            "KRX:102110": {"role": "risk_asset"},
            "KRX:005930": {"role": "core_stock"},
            "KRX:105560": {"role": "dividend_stock"},
            "KRX:488770": {"role": "defensive"},
            "KRX:153130": {"role": "defensive"},
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["KRX:069500"].direction is InsightDirection.UP
    assert by_symbol["KRX:005930"].direction is InsightDirection.UP
    assert by_symbol["KRX:488770"].direction is InsightDirection.UP
    assert by_symbol["KRX:069500"].metadata["regime"] == "strong_risk_on"
    assert by_symbol["KRX:069500"].metadata["bucket"] == "risk"
    assert by_symbol["KRX:488770"].metadata["bucket"] == "defensive"
    assert by_symbol["KRX:069500"].weight > by_symbol["KRX:488770"].weight


def test_core_regime_alpha_allows_etf_entries_after_broker_routing_fix():
    module = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    now = datetime(2026, 5, 22, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=39000, sma20=37400, sma60=36000, momentum_20=0.055, momentum_60=0.090, vol=0.030),
            "KRX:102110": _values(close=41000, sma20=39500, sma60=38200, momentum_20=0.052, momentum_60=0.084, vol=0.032),
            "KRX:005930": _values(close=82000, sma20=79000, sma60=76000, momentum_20=0.065, momentum_60=0.110, vol=0.036),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy", "asset_type": "etf"},
            "KRX:102110": {"role": "risk_asset", "asset_type": "etf"},
            "KRX:005930": {"role": "core_stock", "asset_type": "stock"},
            "KRX:488770": {"role": "defensive", "asset_type": "etf"},
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["KRX:005930"].direction is InsightDirection.UP
    assert by_symbol["KRX:069500"].direction is InsightDirection.UP
    assert by_symbol["KRX:102110"].direction is InsightDirection.UP
    assert by_symbol["KRX:488770"].direction is InsightDirection.UP


def test_kr_domestic_4401_execution_allows_etf_buys_after_broker_routing_fix():
    module = _load("sleeves/kr-domestic-4401/executions/immediate.py")
    stock = Symbol("005930", "KRX")
    etf = Symbol("069500", "KRX")
    now = datetime(2026, 5, 22, 14, 30)
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100.0, 100.0, 100.0, 100.0, 1000),
            etf.key: Bar(etf, now, 100.0, 100.0, 100.0, 100.0, 1000),
        },
    )
    model = module.KrDomestic4401ExecutionModel(max_slice_notional=None)

    orders = model.create_orders(
        "kr-domestic-4401",
        Portfolio(cash=1_000_000),
        data,
        [
            PortfolioTarget(etf, quantity=5, tag="risk_proxy"),
            PortfolioTarget(stock, quantity=5, tag="stock"),
        ],
    )

    assert [(order.symbol.key, order.side) for order in orders] == [
        ("KRX:069500", OrderSide.BUY),
        ("KRX:005930", OrderSide.BUY),
    ]


def test_core_regime_alpha_moves_to_defense_and_flats_risk_assets_on_shock():
    module = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    now = datetime(2026, 5, 22, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(
                close=36000,
                sma20=38500,
                sma60=39000,
                momentum_20=-0.070,
                momentum_60=-0.090,
                vol=0.030,
                return_vol=0.030,
                return_1=-0.078,
                drawdown=-0.085,
            ),
            "KRX:005930": _values(close=76000, sma20=81000, sma60=83000, momentum_20=-0.060, momentum_60=-0.080, vol=0.050),
            "KRX:105560": _values(close=83000, sma20=87000, sma60=89000, momentum_20=-0.040, momentum_60=-0.030, vol=0.035),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002),
            "KRX:153130": _values(close=113000, sma20=112900, sma60=112700, momentum_20=0.002, momentum_60=0.005, vol=0.002),
            "KRX:357870": _values(close=56000, sma20=55900, sma60=55700, momentum_20=0.002, momentum_60=0.005, vol=0.002),
            "KRX:114800": _values(close=4500, sma20=4300, sma60=4200, momentum_20=0.040, momentum_60=0.060, vol=0.040),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy"},
            "KRX:005930": {"role": "core_stock"},
            "KRX:105560": {"role": "dividend_stock"},
            "KRX:488770": {"role": "defensive"},
            "KRX:153130": {"role": "defensive"},
            "KRX:357870": {"role": "defensive"},
            "KRX:114800": {"role": "hedge"},
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["KRX:488770"].direction is InsightDirection.UP
    assert by_symbol["KRX:153130"].direction is InsightDirection.UP
    assert by_symbol["KRX:357870"].direction is InsightDirection.UP
    assert by_symbol["KRX:114800"].direction is InsightDirection.UP
    assert by_symbol["KRX:005930"].direction is InsightDirection.FLAT
    assert by_symbol["KRX:105560"].direction is InsightDirection.FLAT
    assert by_symbol["KRX:488770"].metadata["regime"] == "shock"
    assert by_symbol["KRX:005930"].metadata["bucket"] == "risk_reduction"
    assert by_symbol["KRX:114800"].expires_at == now + timedelta(days=1)


def test_core_regime_alpha_does_not_treat_ordinary_high_vol_pullback_as_shock():
    module = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    now = datetime(2026, 5, 25, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(
                close=38300,
                sma20=38600,
                sma60=38100,
                momentum_20=-0.010,
                momentum_60=0.015,
                vol=0.030,
                return_vol=0.030,
                return_1=-0.025,
                drawdown=-0.040,
            ),
            "KRX:005930": _values(close=80000, sma20=79500, sma60=78000, momentum_20=0.010, momentum_60=0.025, vol=0.035),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002),
            "KRX:153130": _values(close=113000, sma20=112900, sma60=112700, momentum_20=0.002, momentum_60=0.005, vol=0.002),
            "KRX:114800": _values(close=4400, sma20=4300, sma60=4200, momentum_20=0.030, momentum_60=0.040, vol=0.040),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy"},
            "KRX:005930": {"role": "core_stock"},
            "KRX:488770": {"role": "defensive"},
            "KRX:153130": {"role": "defensive"},
            "KRX:114800": {"role": "hedge"},
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["KRX:488770"].metadata["regime"] != "shock"
    assert by_symbol["KRX:114800"].direction is InsightDirection.FLAT
    assert by_symbol["KRX:114800"].metadata["bucket"] == "hedge_exit"


def test_core_regime_alpha_exits_inverse_hedge_when_shock_clears():
    module = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    now = datetime(2026, 5, 25, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=39000, sma20=38000, sma60=37200, momentum_20=0.020, momentum_60=0.035, vol=0.030),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002),
            "KRX:153130": _values(close=113000, sma20=112900, sma60=112700, momentum_20=0.002, momentum_60=0.005, vol=0.002),
            "KRX:114800": _values(close=4400, sma20=4300, sma60=4200, momentum_20=0.030, momentum_60=0.040, vol=0.040),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy"},
            "KRX:488770": {"role": "defensive"},
            "KRX:153130": {"role": "defensive"},
            "KRX:114800": {"role": "hedge"},
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot))
    by_symbol = {insight.symbol.key: insight for insight in insights}

    assert by_symbol["KRX:114800"].direction is InsightDirection.FLAT
    assert by_symbol["KRX:114800"].weight == 0.0
    assert by_symbol["KRX:114800"].metadata["bucket"] == "hedge_exit"
    assert by_symbol["KRX:114800"].metadata["regime"] != "shock"


def test_regime_inverse_vol_portfolio_caps_initial_entry_and_exits_hedge():
    alpha = _load("sleeves/kr-domestic-4401/alphas/core_regime_allocator.py")
    portfolio_module = _load("sleeves/kr-domestic-4401/portfolios/regime_inverse_vol.py")
    now = datetime(2026, 5, 25, 9, 5)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=39000, sma20=37400, sma60=36000, momentum_20=0.055, momentum_60=0.090, vol=0.030, return_vol=0.018),
            "KRX:102110": _values(close=41000, sma20=39500, sma60=38200, momentum_20=0.052, momentum_60=0.084, vol=0.032, return_vol=0.020),
            "KRX:005930": _values(close=82000, sma20=79000, sma60=76000, momentum_20=0.065, momentum_60=0.110, vol=0.036, return_vol=0.028),
            "KRX:009150": _values(close=1324000, sma20=1280000, sma60=1220000, momentum_20=0.060, momentum_60=0.120, vol=0.080, return_vol=0.065),
            "KRX:488770": _values(close=102000, sma20=101900, sma60=101700, momentum_20=0.002, momentum_60=0.006, vol=0.002, return_vol=0.003),
            "KRX:153130": _values(close=113000, sma20=112900, sma60=112700, momentum_20=0.002, momentum_60=0.005, vol=0.002, return_vol=0.003),
            "KRX:357870": _values(close=56000, sma20=55900, sma60=55700, momentum_20=0.002, momentum_60=0.005, vol=0.002, return_vol=0.003),
            "KRX:114800": _values(close=4400, sma20=4300, sma60=4200, momentum_20=0.030, momentum_60=0.040, vol=0.040, return_vol=0.040),
        },
        metadata={
            "KRX:069500": {"role": "risk_proxy"},
            "KRX:102110": {"role": "risk_asset"},
            "KRX:005930": {"role": "core_stock"},
            "KRX:009150": {"role": "satellite_stock"},
            "KRX:488770": {"role": "defensive"},
            "KRX:153130": {"role": "defensive"},
            "KRX:357870": {"role": "defensive"},
            "KRX:114800": {"role": "hedge"},
        },
    )
    insights = tuple(alpha.generate(SnapshotContext.from_indicator_snapshot(snapshot)))
    symbols = [Symbol("069500", "KRX"), Symbol("102110", "KRX"), Symbol("005930", "KRX"), Symbol("009150", "KRX"), Symbol("488770", "KRX"), Symbol("153130", "KRX"), Symbol("357870", "KRX"), Symbol("114800", "KRX")]
    data = DataSlice(
        time=now,
        bars={
            symbol.key: Bar(symbol, now, 100.0, 100.0, 100.0, _price(symbol.key), 1000)
            for symbol in symbols
        },
    )
    context = PortfolioConstructionContext(
        sleeve_id="kr-domestic-4401",
        data=data,
        portfolio=Portfolio(cash=18_721_557, cash_by_currency={"KRW": 18_721_557}),
        active_insights=insights,
        managed_symbols=tuple(symbols),
    )

    targets = portfolio_module.RegimeBudgetedInverseVolPortfolioConstructionModel(
        max_gross_increase_pct=0.35,
        max_symbol_increase_pct=0.08,
    ).create_targets(context)
    target_by_symbol = {target.symbol.key: target for target in targets}

    assert round(sum(target.target_percent for target in targets if target.target_percent > 0), 6) <= 0.35
    assert max(target.target_percent for target in targets if target.target_percent > 0) <= 0.08
    assert target_by_symbol["KRX:114800"].target_percent == 0.0
    assert (
        target_by_symbol["KRX:009150"].target_percent
        >= _price("KRX:009150") / context.target_value_for_symbol(Symbol("009150", "KRX"))
    )


def _snapshot(
    now: datetime,
    values: dict[str, dict[str, float]],
    *,
    metadata: dict[str, dict[str, object]] | None = None,
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="kr-domestic-4401",
        universe_id="kr-domestic-4401-test",
        as_of=now,
        created_at=now,
        symbols=tuple(values),
        values={
            symbol: {
                name: IndicatorValue(name=name, value=value, is_ready=True, samples=140, time=now)
                for name, value in indicator_values.items()
            }
            for symbol, indicator_values in values.items()
        },
        source_snapshot_id="test",
        symbol_metadata=metadata or {},
    )


def _values(
    *,
    close: float,
    sma20: float,
    sma60: float,
    momentum_20: float,
    momentum_60: float,
    vol: float,
    return_vol: float | None = None,
    liquidity: float = 5_000_000_000.0,
    return_1: float = 0.004,
    drawdown: float = -0.025,
) -> dict[str, float]:
    return {
        "close": close,
        "identity_close": close,
        "sma_20_close": sma20,
        "sma_60_close": sma60,
        "roc_20_close": momentum_20,
        "roc_60_close": momentum_60,
        "stddev_20_close": close * vol,
        "return_stddev_20_close": return_vol if return_vol is not None else vol,
        "atr_14": close * vol * 0.80,
        "return_1_close": return_1,
        "drawdown_20_close": drawdown,
        "rolling_dollar_volume_20": liquidity,
        "rolling_dollar_volume_60": liquidity,
    }


def _load(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _price(symbol_key: str) -> float:
    return {
        "KRX:069500": 123_595.0,
        "KRX:102110": 123_617.0,
        "KRX:005930": 293_750.0,
        "KRX:009150": 1_324_000.0,
        "KRX:488770": 104_427.0,
        "KRX:153130": 113_250.0,
        "KRX:357870": 57_530.0,
        "KRX:114800": 4_400.0,
    }[symbol_key]

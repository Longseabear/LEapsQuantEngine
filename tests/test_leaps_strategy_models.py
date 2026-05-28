from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.alpha import SnapshotContext
from leaps_quant_engine.execution import ExecutionContext, PendingOrderState
from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext
from leaps_quant_engine.framework.risk import RiskManagementContext
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import Bar, DataSlice, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.orders import OrderTicket, OrderTicketStatus
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, RuntimeModelStateView, StatePatch
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
    assert insights[0].metadata["recency_weighted_momentum"] > 0
    assert "sector_relative_strength" not in insights[0].metadata
    assert insights[0].metadata["entry_timing_setup"] in {"trend", "pullback", "rebreak"}
    assert insights[0].reason == "kospi_conviction_breadth_trend_momentum"


def test_kospi_conviction_alpha_omits_sector_bonus_metadata():
    module = _load("sleeves/LEaps/alphas/kospi_conviction.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "KRX:011070": _values(close=100_000, fast=112_000, slow=90_000, momentum=0.30, momentum_5=0.10, vol=0.05),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:011070",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:011070"]
    assert "sector" not in insights[0].metadata
    assert "sector_relative_strength" not in insights[0].metadata


def test_leaps_live_alphas_use_patient_alpha_fast_risk_cadences():
    alpha_paths = (
        "sleeves/LEaps/alphas/kospi_conviction.py",
        "sleeves/LEaps/alphas/kospi_pullback_reversion.py",
        "sleeves/LEaps/alphas/kospi_swing_rebalance.py",
        "sleeves/LEaps/alphas/krx_etf_safety.py",
        "sleeves/LEaps/alphas/volatility_trailing_stop.py",
    )

    cadences = {
        relative_path: getattr(_load(relative_path), "EVALUATION_CADENCE", None)
        for relative_path in alpha_paths
    }

    assert cadences == {
        "sleeves/LEaps/alphas/kospi_conviction.py": "every_5_minutes",
        "sleeves/LEaps/alphas/kospi_pullback_reversion.py": "every_5_minutes",
        "sleeves/LEaps/alphas/kospi_swing_rebalance.py": "every_5_minutes",
        "sleeves/LEaps/alphas/krx_etf_safety.py": "every_5_minutes",
        "sleeves/LEaps/alphas/volatility_trailing_stop.py": "every_5_minutes",
    }


def test_leaps_live_config_uses_agent_daily_target_portfolio():
    payload = json.loads((ROOT / "configs/runtime/live_multi_sleeve.json").read_text(encoding="utf-8"))
    sleeve = next(item for item in payload["sleeves"] if item["sleeve_id"] == "LEaps")
    portfolio = sleeve["portfolio"]
    startup_script = (ROOT / "tools/leaps_start_live_stack.ps1").read_text(encoding="utf-8")

    assert sleeve["universe"]["active"]["cadence"] == "daily_at 08:45 Asia/Seoul"
    assert sleeve["universe"]["active"]["selection_models"] == [
        "selections/agent_daily_target.py:AgentDailyTargetSelectionModel",
        "selections/krx_etf_safety.py:KrxEtfSafetySelectionModel",
        "selections/operational_symbols.py:OperationalSymbolsSelectionModel",
    ]
    assert sleeve["alpha"]["modules"] == []
    assert sleeve["alpha"]["input_selections"] == {}
    assert portfolio["model"] == "portfolios/agent_daily_target.py"
    assert portfolio["rebalance"]["cadence"] == "daily_at 08:50 Asia/Seoul"
    assert portfolio["rebalance"]["min_order_notional"] == 50_000
    assert portfolio["rebalance"]["whole_share_entry_floor_min_fraction"] == 0.75
    assert portfolio["rebalance"]["reused_target_churn_lot_fraction"] == 0.25
    assert portfolio["parameters"]["model_id"] == "leaps-agent-daily-target-portfolio"
    assert portfolio["parameters"]["target_path"] == "data/operator-targets/LEaps/latest_target.json"
    assert portfolio["parameters"]["max_gross_exposure"] == 0.98
    assert portfolio["parameters"]["max_position_pct"] == 0.24
    assert portfolio["parameters"]["max_target_age_hours"] == 36.0
    assert portfolio["parameters"]["require_sleeve_id"] is True
    assert portfolio["parameters"]["scale_to_max_gross"] is True
    assert portfolio["parameters"]["emit_zero_for_missing_held_targets"] is True
    risk_params = sleeve["risk"]["parameters"]
    assert risk_params["regime_total_exposure_pct_by_currency"]["KRW"]["neutral"] == 0.98
    assert risk_params["intraday_entry_freeze_cap_pct_by_currency"]["KRW"] == 0.55
    assert risk_params["intraday_risk_off_cap_pct_by_currency"]["KRW"] == 0.35
    assert risk_params["intraday_guard_high_entry_freeze_return_pct"] == -0.015
    assert risk_params["intraday_guard_high_risk_off_return_pct"] == -0.02
    assert risk_params["intraday_guard_recovery_cap_pct_by_currency"]["KRW"] == 0.55
    assert risk_params["symbol_entry_block_intraday_return_pct"] == -0.12
    assert risk_params["symbol_entry_block_high_drawdown_pct"] == -0.10
    assert risk_params["symbol_entry_block_unrealized_loss_pct"] == -0.08
    assert risk_params["symbol_entry_block_sma10_buffer_pct"] == -0.07
    assert risk_params["symbol_entry_block_sma20_buffer_pct"] == -0.03
    assert risk_params["symbol_reduce_half_unrealized_loss_pct"] == -0.07
    assert risk_params["symbol_exit_unrealized_loss_pct"] == -0.1
    assert risk_params["symbol_reduce_half_high_drawdown_pct"] == -0.16
    assert risk_params["symbol_exit_high_drawdown_pct"] == -0.22
    assert risk_params["symbol_reduce_half_sma10_buffer_pct"] == -0.2
    assert risk_params["symbol_exit_sma20_buffer_pct"] == -0.2
    assert risk_params["symbol_guard_max_volatility_multiplier"] == 1.25
    assert risk_params["symbol_pullback_add_enabled"] is True
    assert risk_params["symbol_pullback_add_fraction"] == 0.35
    assert risk_params["symbol_pullback_add_min_intraday_return_pct"] == -0.12
    assert risk_params["symbol_pullback_add_min_unrealized_pnl_pct"] == -0.06
    assert risk_params["symbol_pullback_add_min_alpha_count"] == 0
    execution_params = sleeve["execution"]["parameters"]
    assert execution_params["dynamic_slice_notional_enabled"] is True
    assert execution_params["dynamic_slice_equity_pct"] == 0.2
    assert execution_params["dynamic_slice_min_notional"] == 1_000_000
    assert execution_params["dynamic_slice_max_notional"] == 5_000_000
    assert execution_params["dynamic_slice_liquidity_bps"] == 8.0
    assert execution_params["model_version"] == "4.4.1"
    assert execution_params["auction_volume_participation_enabled"] is False
    assert execution_params["volume_participation_use_liquidity_notional"] is True
    assert execution_params["volume_participation_min_notional"] == 2_000_000
    assert execution_params["reused_target_suppress_buy_add"] is False
    assert execution_params["reused_target_sell_no_trade_max_quantity_delta"] == 2
    assert execution_params["reused_target_sell_no_trade_max_notional"] == 300_000
    assert execution_params["reused_target_sell_no_trade_pct_of_target"] == 0.05
    assert execution_params["anti_oscillation_enabled"] is True
    assert execution_params["notional_rebalance_band_enabled"] is True
    assert execution_params["rebalance_no_trade_min_notional"] == 100_000
    assert execution_params["rebalance_no_trade_pct_of_target"] == 0.08
    assert execution_params["opposite_rebalance_cooldown_minutes"] == 15
    assert execution_params["opposite_rebalance_no_trade_max_quantity_delta"] == 2
    assert execution_params["opposite_rebalance_no_trade_max_notional"] == 300_000
    assert execution_params["opposite_rebalance_no_trade_pct_of_position"] == 0.05
    assert execution_params["risk_reentry_cooldown_minutes"] == 60
    assert sleeve["worker"]["cycle_interval_seconds"] == 300
    assert '"-IntervalSeconds", "10"' in startup_script


def test_leaps_growth_alphas_pass_through_temporal_feature_windows():
    now = datetime(2026, 5, 14)
    symbol_key = "KRX:005930"
    temporal_rows = [{"selected_flag": 1.0, "momentum_20": 0.10 + index * 0.001} for index in range(64)]
    cases = []

    conviction_values = _values(
        close=268_500,
        fast=247_000,
        slow=224_000,
        momentum=0.27,
        momentum_5=0.18,
        vol=0.07,
    )
    cases.append(("sleeves/LEaps/alphas/kospi_conviction.py", conviction_values))

    pullback_values = _values(
        close=100_000,
        fast=103_000,
        slow=92_000,
        momentum=0.16,
        momentum_5=-0.025,
        vol=0.06,
        rolling_high=106_000,
        rolling_low=96_000,
    )
    cases.append(("sleeves/LEaps/alphas/kospi_pullback_reversion.py", pullback_values))

    swing_values = _values(
        close=100_000,
        fast=99_000,
        slow=90_000,
        momentum=0.12,
        momentum_5=0.025,
        vol=0.05,
        rolling_high=104_000,
    )
    swing_values["sma_10_close"] = 98_000
    cases.append(("sleeves/LEaps/alphas/kospi_swing_rebalance.py", swing_values))

    for module_path, values in cases:
        module = _load(module_path)
        snapshot = _snapshot(
            now,
            {symbol_key: values},
            symbol_metadata={symbol_key: {"rl_temporal_features": temporal_rows}},
        )
        insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols((symbol_key,)))
        up_insights = [insight for insight in insights if insight.direction is InsightDirection.UP]

        assert up_insights, module_path
        assert up_insights[0].metadata["rl_temporal_features"] == temporal_rows


def test_growth_alphas_prefer_live_close_for_intraday_price_marks():
    now = datetime(2026, 5, 21, 10, 15)
    symbol_key = "KRX:080220"

    conviction_values = _values(
        close=94_000,
        fast=99_000,
        slow=96_000,
        momentum=0.12,
        momentum_5=0.035,
        vol=0.04,
        rolling_high=101_000,
    )
    conviction_values["live_close"] = 100_000
    conviction = _load("sleeves/LEaps/alphas/kospi_conviction.py")
    conviction_insights = conviction.generate(
        SnapshotContext.from_indicator_snapshot(_snapshot(now, {symbol_key: conviction_values})).with_input_symbols(
            (symbol_key,)
        )
    )

    assert [insight.symbol.key for insight in conviction_insights] == [symbol_key]
    assert conviction_insights[0].metadata["close"] == 100_000

    pullback_values = _values(
        close=94_000,
        fast=99_000,
        slow=94_000,
        momentum=0.14,
        momentum_5=0.035,
        vol=0.04,
        rolling_high=103_000,
        rolling_low=93_000,
    )
    pullback_values["live_close"] = 100_000
    pullback = _load("sleeves/LEaps/alphas/kospi_pullback_reversion.py")
    pullback_insights = pullback.generate(
        SnapshotContext.from_indicator_snapshot(_snapshot(now, {symbol_key: pullback_values})).with_input_symbols(
            (symbol_key,)
        )
    )

    assert [insight.symbol.key for insight in pullback_insights] == [symbol_key]
    assert pullback_insights[0].metadata["close"] == 100_000


def test_kospi_swing_rebalance_alpha_treats_live_breakouts_as_continuation_buys():
    module = _load("sleeves/LEaps/alphas/kospi_swing_rebalance.py")
    now = datetime(2026, 5, 21, 10, 15)
    values = _values(
        close=94_000,
        fast=101_000,
        slow=90_000,
        momentum=0.22,
        momentum_5=0.12,
        vol=0.05,
        rolling_high=103_000,
    )
    values["sma_10_close"] = 100_000
    values["live_close"] = 104_000
    snapshot = _snapshot(now, {"KRX:080220": values})

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:080220",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:080220"]
    assert insights[0].direction is InsightDirection.UP
    assert insights[0].reason == "kospi_swing_buy_breakout_continuation"
    assert insights[0].metadata["portfolio_action"] == "buy_breakout"
    assert insights[0].metadata["close"] == 104_000
    assert insights[0].metadata["pullback_from_high"] == 0.0


def test_volatility_trailing_stop_uses_model_state_high_watermark():
    module = _load("sleeves/LEaps/alphas/volatility_trailing_stop.py")
    now = datetime(2026, 5, 8)
    symbol_key = "KRX:005930"
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-volatility-trailing-stop",
                    namespace="trailing_stop",
                    symbol_key=symbol_key,
                ),
                value={"high_watermark_price": 120_000},
                reason="seed_high_watermark",
            ),
        )
    )
    snapshot = _snapshot(
        now,
        {
            symbol_key: _values(
                close=100_000,
                fast=102_000,
                slow=95_000,
                momentum=0.15,
                momentum_5=0.03,
                vol=0.05,
                rolling_high=105_000,
            )
        },
    )
    context = SnapshotContext.from_indicator_snapshot(
        snapshot,
        model_state=state_view,
    ).with_input_symbols((symbol_key,))

    insights = module.generate(context)
    patches = module.state_patches(context=context, insights=tuple(insights))

    assert [insight.symbol.key for insight in insights] == [symbol_key]
    assert insights[0].reason == "volatility_trailing_stop_triggered"
    assert insights[0].metadata["high_watermark_price"] == 120_000
    assert insights[0].metadata["rolling_high"] == 105_000
    assert patches[0].key.model_id == "leaps-volatility-trailing-stop"
    assert patches[0].key.namespace == "trailing_stop"
    assert patches[0].value["high_watermark_price"] == 120_000
    assert patches[0].value["last_price"] == 100_000


def test_volatility_trailing_stop_uses_live_close_for_intraday_stop_checks():
    module = _load("sleeves/LEaps/alphas/volatility_trailing_stop.py")
    now = datetime(2026, 5, 21, 10, 20)
    symbol_key = "KRX:005930"
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-volatility-trailing-stop",
                    namespace="trailing_stop",
                    symbol_key=symbol_key,
                ),
                value={"high_watermark_price": 120_000},
                reason="seed_high_watermark",
            ),
        )
    )
    values = _values(
        close=112_000,
        fast=113_000,
        slow=100_000,
        momentum=0.12,
        momentum_5=0.02,
        vol=0.05,
        rolling_high=118_000,
    )
    values["live_close"] = 100_000
    snapshot = _snapshot(now, {symbol_key: values})
    context = SnapshotContext.from_indicator_snapshot(
        snapshot,
        model_state=state_view,
    ).with_input_symbols((symbol_key,))

    insights = module.generate(context)
    patches = module.state_patches(context=context, insights=tuple(insights))

    assert [insight.symbol.key for insight in insights] == [symbol_key]
    assert insights[0].metadata["close"] == 100_000
    assert patches[0].value["last_price"] == 100_000


def test_volatility_trailing_stop_does_not_replace_seeded_high_watermark_with_rolling_high():
    module = _load("sleeves/LEaps/alphas/volatility_trailing_stop.py")
    symbol_key = "KRX:006400"
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-volatility-trailing-stop",
                    namespace="trailing_stop",
                    symbol_key=symbol_key,
                ),
                value={"high_watermark_price": 633_000},
                reason="seed_high_watermark",
            ),
        )
    )
    snapshot = _snapshot(
        datetime(2026, 5, 14, 16, 0),
        {
            symbol_key: _values(
                close=636_000,
                fast=630_000,
                slow=620_000,
                momentum=0.03,
                momentum_5=0.01,
                vol=0.08,
                rolling_high=712_000,
            )
        },
    )
    context = SnapshotContext.from_indicator_snapshot(
        snapshot,
        model_state=state_view,
    ).with_input_symbols((symbol_key,))

    patches = module.state_patches(context=context, insights=())

    assert patches[0].value["previous_high_watermark_price"] == 633_000
    assert patches[0].value["rolling_high"] == 712_000
    assert patches[0].value["high_watermark_price"] == 636_000


def test_leaps_rl_constructor_can_be_configured_as_complete_target_portfolio():
    module = _load("sleeves/LEaps/portfolios/rl_ppo_constructor.py")

    model = module.create_portfolio_model({"emit_zero_for_missing_held_targets": True, "policy_device": "cuda"})

    assert model.emit_zero_for_missing_held_targets is True
    assert model.policy_device == "cuda"


def test_leaps_rl_v2_observation_uses_state_aligned_features():
    from leaps_quant_engine.rl.portfolio_constructor import _observation_from_insights

    now = datetime(2026, 5, 14)
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="rl-portfolio-constructor",
                    namespace="target_anchor",
                    symbol_key=symbol.key,
                ),
                value={"target_percent": 0.25},
                reason="seed_previous_target",
            ),
        )
    )
    portfolio = Portfolio(cash=7_000_000, cash_by_currency={"KRW": 7_000_000})
    portfolio.holdings[symbol.key] = Holding(symbol, quantity=10, average_price=90_000)
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)}),
        portfolio=portfolio,
        active_insights=(),
        managed_symbols=(),
        model_state=state_view,
    )
    insight = Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=3),
        source_snapshot_id="test",
        alpha_id="leaps-kospi-conviction",
        alpha_version="0.1.0",
        confidence=0.8,
        score=0.42,
        metadata={
            "momentum": 0.20,
            "momentum_5": 0.05,
            "return_1": 0.01,
            "drawdown_20": 0.03,
            "volatility": 0.07,
        },
    )

    observation = _observation_from_insights(
        context,
        [insight],
        currency="KRW",
        top_k=2,
        feature_schema="v2_state",
    )

    assert observation.shape == (2, 10)
    assert observation[0, 0] == 1.0
    assert abs(observation[0, 1] - 0.20) < 1e-6
    assert abs(observation[0, 3] - 0.05) < 1e-6
    assert abs(observation[0, 4] - 0.01) < 1e-6
    assert abs(observation[0, 5] - 0.03) < 1e-6
    assert abs(observation[0, 7] - 0.125) < 1e-6
    assert abs(observation[0, 8] - 0.25) < 1e-6
    assert abs(observation[0, 9] - 0.125) < 1e-6
    assert observation[1].sum() == 0.0


def test_leaps_rl_v2_constructor_persists_target_anchor_without_smoothing():
    from leaps_quant_engine.rl import ReinforcementLearningPortfolioConstructionModel

    now = datetime(2026, 5, 14)
    symbol = Symbol("005930", "KRX")
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1_000_000)}),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(),
        managed_symbols=(),
        model_state=RuntimeModelStateView(InMemoryRuntimeStateStore(), default_sleeve_id="LEaps"),
    )
    model = ReinforcementLearningPortfolioConstructionModel(feature_schema="v2_state")

    patches = model.state_patches(
        context,
        (PortfolioAllocationTarget(symbol=symbol, target_percent=0.31, tag="test"),),
    )

    assert len(patches) == 1
    assert patches[0].key.model_id == "rl-portfolio-constructor"
    assert patches[0].key.namespace == "target_anchor"
    assert patches[0].key.symbol_key == symbol.key
    assert patches[0].value["target_percent"] == 0.31


def test_leaps_rl_constructor_caps_target_turnover_after_smoothing():
    from leaps_quant_engine.rl import ReinforcementLearningPortfolioConstructionModel

    now = datetime(2026, 5, 14)
    samsung = Symbol("005930", "KRX")
    hynix = Symbol("000660", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="rl-portfolio-constructor",
                    namespace="target_anchor",
                    symbol_key=samsung.key,
                ),
                value={"target_percent": 0.20},
                reason="seed_previous_target",
            ),
        )
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                samsung.key: Bar(samsung, now, 100_000, 100_000, 100_000, 100_000, 1_000_000),
                hynix.key: Bar(hynix, now, 200_000, 200_000, 200_000, 200_000, 1_000_000),
            },
        ),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(),
        managed_symbols=(),
        model_state=state_view,
    )
    model = ReinforcementLearningPortfolioConstructionModel(
        feature_schema="v2_state",
        max_target_turnover_pct=0.15,
    )

    capped = model._cap_target_turnover(
        context,
        (
            PortfolioAllocationTarget(symbol=samsung, target_percent=0.30, tag="test"),
            PortfolioAllocationTarget(symbol=hynix, target_percent=0.20, tag="test"),
        ),
    )

    by_key = {target.symbol.key: target for target in capped}
    assert abs(by_key[samsung.key].target_percent - 0.25) < 1e-6
    assert abs(by_key[hynix.key].target_percent - 0.10) < 1e-6
    assert "turnover_cap" in by_key[samsung.key].tag
    assert "turnover_cap" in by_key[hynix.key].tag


def test_research_adaptive_allocator_builds_cash_aware_top_k_targets():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 14)
    samsung = Symbol("005930", "KRX")
    hynix = Symbol("000660", "KRX")
    doosan = Symbol("034020", "KRX")
    model = module.create_portfolio_model(
        {
            "top_k": 2,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.55,
            "weak_gross_exposure": 0.30,
            "cash_bias": 0.05,
            "max_position_pct": 0.40,
            "score_temperature": 0.35,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                samsung.key: Bar(samsung, now, 80_000, 80_000, 80_000, 80_000, 1_000_000),
                hynix.key: Bar(hynix, now, 230_000, 230_000, 230_000, 230_000, 1_000_000),
                doosan.key: Bar(doosan, now, 60_000, 60_000, 60_000, 60_000, 1_000_000),
            },
        ),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(samsung, now, score=0.34, momentum=0.24, momentum_5=0.08, trend=0.22),
            _allocator_insight(hynix, now, score=0.22, momentum=0.18, momentum_5=0.04, trend=0.16),
            _allocator_insight(doosan, now, score=0.08, momentum=0.10, momentum_5=0.02, trend=0.10),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    by_symbol = {target.symbol.key: target for target in targets}
    assert set(by_symbol) == {samsung.key, hynix.key}
    assert by_symbol[samsung.key].target_percent > by_symbol[hynix.key].target_percent
    assert 0.0 < sum(target.target_percent for target in targets) <= 0.80
    assert all(target.tag.startswith("adaptive:research_adaptive_allocator") for target in targets)


def test_research_adaptive_allocator_reduces_exposure_when_breadth_is_weak():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 14)
    symbol = Symbol("005930", "KRX")
    model = module.create_portfolio_model(
        {
            "gross_exposure": 0.90,
            "neutral_gross_exposure": 0.60,
            "weak_gross_exposure": 0.25,
            "max_normalized_volatility": 0.30,
            "cash_bias": 0.0,
        }
    )
    base = {
        "sleeve_id": "LEaps",
        "data": DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 80_000, 80_000, 80_000, 80_000, 1_000_000)}),
        "portfolio": Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        "managed_symbols": (),
    }
    strong_context = PortfolioConstructionContext(
        **base,
        active_insights=(_allocator_insight(symbol, now, score=0.34, momentum=0.24, volatility=0.08, breadth=0.70),),
    )
    weak_context = PortfolioConstructionContext(
        **base,
        active_insights=(_allocator_insight(symbol, now, score=0.34, momentum=0.12, volatility=0.18, breadth=0.20),),
    )

    strong_total = sum(target.target_percent for target in model.create_targets(strong_context))
    weak_total = sum(target.target_percent for target in model.create_targets(weak_context))

    assert weak_total < strong_total
    assert weak_total <= 0.25


def test_research_adaptive_allocator_blocks_latest_flat_and_zeroes_held_symbol():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 14)
    selected = Symbol("000660", "KRX")
    held_missing = Symbol("034020", "KRX")
    portfolio = Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000})
    portfolio.holdings[held_missing.key] = Holding(held_missing, quantity=10, average_price=50_000)
    model = module.create_portfolio_model({"emit_zero_for_missing_held_targets": True})
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                selected.key: Bar(selected, now, 230_000, 230_000, 230_000, 230_000, 1_000_000),
                held_missing.key: Bar(held_missing, now, 60_000, 60_000, 60_000, 60_000, 1_000_000),
            },
        ),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(selected, now, score=0.30, momentum=0.20),
            _allocator_insight(held_missing, now - timedelta(minutes=1), score=0.30, momentum=0.20),
            Insight(
                sleeve_id="LEaps",
                symbol=held_missing,
                direction=InsightDirection.FLAT,
                generated_at=now,
                source_snapshot_id="test",
                alpha_id="leaps-volatility-trailing-stop",
                alpha_version="0.1.0",
            ),
        ),
        managed_symbols=(held_missing,),
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert by_symbol[selected.key].target_percent > 0
    assert by_symbol[held_missing.key].target_percent == 0.0
    assert by_symbol[held_missing.key].tag == "adaptive:research_adaptive_allocator:no_longer_in_target_portfolio"


def test_research_adaptive_allocator_prioritizes_same_timestamp_flat_over_up():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 14)
    held = Symbol("034020", "KRX")
    portfolio = Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000})
    portfolio.holdings[held.key] = Holding(held, quantity=10, average_price=50_000)
    model = module.create_portfolio_model({"emit_zero_for_missing_held_targets": True})
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={held.key: Bar(held, now, 60_000, 60_000, 60_000, 60_000, 1_000_000)},
        ),
        portfolio=portfolio,
        active_insights=(
            Insight(
                sleeve_id="LEaps",
                symbol=held,
                direction=InsightDirection.FLAT,
                generated_at=now,
                source_snapshot_id="test",
                alpha_id="leaps-volatility-trailing-stop",
                alpha_version="0.1.0",
            ),
            _allocator_insight(held, now, score=0.30, momentum=0.20),
        ),
        managed_symbols=(held,),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == held
    assert targets[0].target_percent == 0.0
    assert targets[0].tag == "adaptive:research_adaptive_allocator:no_longer_in_target_portfolio"


def test_research_adaptive_allocator_applies_partial_trim_without_full_exit():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 14, 10, 5)
    held = Symbol("005930", "KRX")
    challenger = Symbol("000660", "KRX")
    portfolio = Portfolio(cash=2_500_000, cash_by_currency={"KRW": 2_500_000})
    portfolio.holdings[held.key] = Holding(held, quantity=100, average_price=75_000)
    model = module.create_portfolio_model(
        {
            "top_k": 2,
            "gross_exposure": 0.90,
            "neutral_gross_exposure": 0.70,
            "weak_gross_exposure": 0.35,
            "cash_bias": 0.0,
            "max_position_pct": 0.80,
            "min_position_pct": 0.01,
            "score_temperature": 0.35,
            "emit_zero_for_missing_held_targets": True,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                held.key: Bar(held, now, 100_000, 100_000, 100_000, 100_000, 1_000_000),
                challenger.key: Bar(challenger, now, 150_000, 150_000, 150_000, 150_000, 1_000_000),
            },
        ),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(held, now - timedelta(minutes=1), score=0.36, momentum=0.24, momentum_5=0.04),
            _partial_trim_insight(held, now, target_multiplier=0.50),
            _allocator_insight(challenger, now, score=0.20, momentum=0.18, momentum_5=0.03),
        ),
        managed_symbols=(held,),
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert by_symbol[held.key].target_percent > 0.0
    assert by_symbol[held.key].target_percent <= 0.40
    assert "partial_trim=0.50" in by_symbol[held.key].tag
    assert by_symbol[held.key].tag != "adaptive:research_adaptive_allocator:no_longer_in_target_portfolio"


def test_research_adaptive_allocator_keeps_etf_safety_bucket_separate():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 15)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    inverse = Symbol("114800", "KRX")
    model = module.create_portfolio_model(
        {
            "top_k": 1,
            "gross_exposure": 0.95,
            "neutral_gross_exposure": 0.70,
            "weak_gross_exposure": 0.40,
            "cash_bias": 0.0,
            "max_position_pct": 0.80,
            "enable_etf_safety_bucket": True,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                stock.key: Bar(stock, now, 80_000, 80_000, 80_000, 80_000, 1_000_000),
                cash_like.key: Bar(cash_like, now, 104_000, 104_000, 104_000, 104_000, 1_000_000),
                inverse.key: Bar(inverse, now, 4_000, 4_000, 4_000, 4_000, 1_000_000),
            },
        ),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(stock, now, score=0.50, momentum=0.30, breadth=0.75),
            _etf_safety_insight(cash_like, now, target_pct=0.42, role="cash_like", stock_gross_cap=0.45),
            _etf_safety_insight(inverse, now, target_pct=0.08, role="inverse", stock_gross_cap=0.45),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)
    by_symbol = {target.symbol.key: target for target in targets}

    assert set(by_symbol) == {stock.key, cash_like.key, inverse.key}
    assert [target.symbol.key for target in targets[:2]] == [cash_like.key, inverse.key]
    assert 0.0 < by_symbol[stock.key].target_percent <= 0.45
    assert by_symbol[cash_like.key].target_percent == 0.42
    assert by_symbol[inverse.key].target_percent == 0.08
    assert "etf_safety:cash_like" in by_symbol[cash_like.key].tag


def test_research_adaptive_allocator_caps_inverse_to_stock_beta_exposure():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 15)
    stock = Symbol("036930", "KRX")
    cash_like = Symbol("488770", "KRX")
    inverse = Symbol("114800", "KRX")
    model = module.create_portfolio_model(
        {
            "top_k": 1,
            "gross_exposure": 0.15,
            "neutral_gross_exposure": 0.15,
            "weak_gross_exposure": 0.15,
            "cash_bias": 0.0,
            "max_position_pct": 0.15,
            "min_position_pct": 0.001,
            "enable_etf_safety_bucket": True,
            "etf_safety_max_total_pct": 0.80,
            "inverse_hedge_stock_beta_assumption": 1.25,
            "inverse_hedge_shock_buffer_pct": 0.02,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                stock.key: Bar(stock, now, 20_000, 20_000, 20_000, 20_000, 1_000_000),
                cash_like.key: Bar(cash_like, now, 104_000, 104_000, 104_000, 104_000, 1_000_000),
                inverse.key: Bar(inverse, now, 4_000, 4_000, 4_000, 4_000, 1_000_000),
            },
        ),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(stock, now, score=0.50, momentum=0.30, breadth=0.75),
            _etf_safety_insight(cash_like, now, target_pct=0.60, role="cash_like", stock_gross_cap=0.20),
            _etf_safety_insight(inverse, now, target_pct=0.20, role="inverse", stock_gross_cap=0.20),
        ),
        managed_symbols=(),
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert abs(by_symbol[stock.key].target_percent - 0.075) < 1e-9
    assert abs(by_symbol[inverse.key].target_percent - 0.11375) < 1e-9
    assert abs(by_symbol[cash_like.key].target_percent - 0.68625) < 1e-9
    assert "inverse_cap=0.114" in by_symbol[inverse.key].tag
    assert "inverse_realloc=0.086" in by_symbol[cash_like.key].tag


def test_research_adaptive_allocator_does_not_open_inverse_without_stock_exposure():
    module = _load("sleeves/LEaps/portfolios/research_adaptive_allocator.py")
    now = datetime(2026, 5, 15)
    cash_like = Symbol("488770", "KRX")
    inverse = Symbol("114800", "KRX")
    model = module.create_portfolio_model(
        {
            "enable_etf_safety_bucket": True,
            "etf_safety_max_total_pct": 0.80,
            "inverse_hedge_stock_beta_assumption": 1.25,
            "inverse_hedge_shock_buffer_pct": 0.02,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                cash_like.key: Bar(cash_like, now, 104_000, 104_000, 104_000, 104_000, 1_000_000),
                inverse.key: Bar(inverse, now, 4_000, 4_000, 4_000, 4_000, 1_000_000),
            },
        ),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _etf_safety_insight(cash_like, now, target_pct=0.60, role="cash_like", stock_gross_cap=0.20),
            _etf_safety_insight(inverse, now, target_pct=0.20, role="inverse", stock_gross_cap=0.20),
        ),
        managed_symbols=(),
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert set(by_symbol) == {cash_like.key}
    assert abs(by_symbol[cash_like.key].target_percent - 0.80) < 1e-9
    assert "inverse_realloc=0.200" in by_symbol[cash_like.key].tag


def test_v4_banded_momentum_retains_held_symbol_outside_entry_band():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    top_a = Symbol("000660", "KRX")
    top_b = Symbol("011070", "KRX")
    held = Symbol("005930", "KRX")
    challenger = Symbol("036930", "KRX")
    portfolio = Portfolio(cash=5_000_000, cash_by_currency={"KRW": 5_000_000})
    portfolio.holdings[held.key] = Holding(held, quantity=100, average_price=50_000)
    model = module.create_portfolio_model(
        {
            "entry_top_n": 2,
            "hold_top_n": 5,
            "trim_top_n": 8,
            "max_positions": 4,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.50,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": None,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (top_a, top_b, held, challenger), close=50_000),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(top_a, now, score=0.60, momentum=0.26),
            _allocator_insight(top_b, now, score=0.55, momentum=0.24),
            _allocator_insight(held, now, score=0.30, momentum=0.20),
            _allocator_insight(challenger, now, score=0.20, momentum=0.18),
        ),
        managed_symbols=(held,),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert held.key in by_symbol
    assert by_symbol[held.key].target_percent > 0.0
    assert ":hold:" in by_symbol[held.key].tag


def test_v4_banded_momentum_trims_held_symbol_between_hold_and_exit_bands():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    top_a = Symbol("000660", "KRX")
    top_b = Symbol("011070", "KRX")
    held = Symbol("005930", "KRX")
    portfolio = Portfolio(cash=5_000_000, cash_by_currency={"KRW": 5_000_000})
    portfolio.holdings[held.key] = Holding(held, quantity=100, average_price=50_000)
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "hold_top_n": 2,
            "trim_top_n": 5,
            "max_positions": 2,
            "trim_multiplier": 0.50,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": None,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (top_a, top_b, held), close=50_000),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(top_a, now, score=0.60, momentum=0.26),
            _allocator_insight(top_b, now, score=0.55, momentum=0.24),
            _allocator_insight(held, now, score=0.30, momentum=0.20),
        ),
        managed_symbols=(held,),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert by_symbol[held.key].target_percent == 0.25
    assert "rank_trim:3" in by_symbol[held.key].tag


def test_v4_banded_momentum_hard_exit_bypasses_turnover_cap():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    held = Symbol("005930", "KRX")
    portfolio = Portfolio(cash=5_000_000, cash_by_currency={"KRW": 5_000_000})
    portfolio.holdings[held.key] = Holding(held, quantity=100, average_price=50_000)
    model = module.create_portfolio_model(
        {
            "max_target_turnover_pct": 0.01,
            "min_position_pct": 0.001,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (held,), close=50_000),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(held, now, score=0.40, momentum=0.22),
            Insight(
                sleeve_id="LEaps",
                symbol=held,
                direction=InsightDirection.FLAT,
                generated_at=now,
                source_snapshot_id="test",
                alpha_id="leaps-volatility-trailing-stop",
                alpha_version="0.1.0",
                reason="trailing_stop_triggered",
            ),
        ),
        managed_symbols=(held,),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == held
    assert targets[0].target_percent == 0.0
    assert "hard_exit" in targets[0].tag
    assert "turnover_cap" not in targets[0].tag


def test_v4_banded_momentum_persists_alpha_attribution_with_normalized_scores():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    high = Symbol("000660", "KRX")
    low = Symbol("005930", "KRX")
    model = module.create_portfolio_model(
        {
            "entry_top_n": 2,
            "max_positions": 2,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": None,
            "daily_turnover_budget_pct": None,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (high, low), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(high, now, score=0.60, momentum=0.26),
            _allocator_insight(low, now, score=0.20, momentum=0.18),
            _allocator_insight(
                high,
                now,
                score=0.30,
                momentum=0.20,
                alpha_id="leaps-kospi-pullback-reversion",
            ),
        ),
        managed_symbols=(),
        model_state=RuntimeModelStateView(InMemoryRuntimeStateStore(), default_sleeve_id="LEaps"),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    targets = model.create_targets(context)
    patches = model.state_patches(context=context, targets=targets)
    high_patch = next(patch for patch in patches if patch.key.symbol_key == high.key)
    attribution = high_patch.value["attribution"]

    assert attribution["regime"] in {"risk_on", "strong_risk_on"}
    assert attribution["alpha_sources"]["leaps-kospi-conviction"]["normalized_score"] == 1.0
    assert "leaps-kospi-pullback-reversion" in attribution["alpha_sources"]
    assert attribution["components"]["multi_alpha"] > 0.0


def test_v4_banded_momentum_partial_trim_blocks_new_entry():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    symbol = Symbol("000660", "KRX")
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "max_positions": 1,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": None,
            "daily_turnover_budget_pct": None,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(symbol, now, score=0.60, momentum=0.26),
            _partial_trim_insight(symbol, now, target_multiplier=0.50),
        ),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    assert model.create_targets(context) == ()


def test_v4_banded_momentum_turnover_caps_new_entries():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    first = Symbol("000660", "KRX")
    second = Symbol("011070", "KRX")
    model = module.create_portfolio_model(
        {
            "entry_top_n": 2,
            "max_positions": 2,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": 0.10,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (first, second), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(
            _allocator_insight(first, now, score=0.60, momentum=0.26),
            _allocator_insight(second, now, score=0.55, momentum=0.24),
        ),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    targets = model.create_targets(context)

    assert abs(sum(target.target_percent for target in targets) - 0.10) < 1e-9
    assert all("turnover_cap" in target.tag for target in targets)


def test_v4_banded_momentum_respects_daily_turnover_budget_across_cycles():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    first = Symbol("000660", "KRX")
    second = Symbol("011070", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "max_positions": 1,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "max_target_turnover_pct": 0.20,
            "daily_turnover_budget_pct": 0.10,
        }
    )
    first_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (first,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(first, now, score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )
    first_targets = model.create_targets(first_context)
    store.apply_patches(model.state_patches(context=first_context, targets=first_targets), applied_at=now)

    second_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now + timedelta(minutes=5), (second,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(second, now + timedelta(minutes=5), score=0.70, momentum=0.30),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )
    second_targets = model.create_targets(second_context)

    assert abs(sum(target.target_percent for target in first_targets) - 0.10) < 1e-9
    assert len(second_targets) == 1
    assert second_targets[0].symbol == second
    assert second_targets[0].target_percent == 0.0
    assert "turnover_cap=0.000" in second_targets[0].tag


def test_v4_banded_momentum_holds_small_target_drift():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    symbol = Symbol("000660", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4-banded-momentum",
                    namespace="position_state",
                    symbol_key=symbol.key,
                ),
                value={
                    "status": "hold",
                    "target_percent": 0.20,
                    "entered_at": (now - timedelta(days=2)).isoformat(),
                    "updated_at": (now - timedelta(minutes=5)).isoformat(),
                },
                reason="seed_v4_target",
            ),
        ),
        applied_at=now,
    )
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "max_positions": 1,
            "gross_exposure": 0.90,
            "neutral_gross_exposure": 0.90,
            "weak_gross_exposure": 0.90,
            "max_position_pct": 0.22,
            "min_position_pct": 0.001,
            "target_drift_threshold_pct": 0.03,
            "max_target_turnover_pct": 0.20,
            "daily_turnover_budget_pct": 0.20,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(symbol, now, score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == 0.20
    assert "drift_hold=0.200" in targets[0].tag


def test_v4_banded_momentum_blocks_same_day_reentry_after_exit():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    symbol = Symbol("000660", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4-banded-momentum",
                    namespace="position_state",
                    symbol_key=symbol.key,
                ),
                value={
                    "status": "hard_exit",
                    "target_percent": 0.0,
                    "entered_at": "",
                    "updated_at": (now - timedelta(minutes=5)).isoformat(),
                },
                reason="seed_v4_exit",
            ),
        ),
        applied_at=now,
    )
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "max_positions": 1,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "reentry_cooldown_days": 1,
            "max_target_turnover_pct": None,
            "daily_turnover_budget_pct": None,
        }
    )
    same_day_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(symbol, now, score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )
    next_day_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now + timedelta(days=1), (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(symbol, now + timedelta(days=1), score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )

    assert model.create_targets(same_day_context) == ()
    assert model.create_targets(next_day_context)[0].target_percent > 0.0


def test_v4_banded_momentum_uses_hard_exit_specific_cooldown():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    symbol = Symbol("000660", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4-banded-momentum",
                    namespace="position_state",
                    symbol_key=symbol.key,
                ),
                value={
                    "status": "hard_exit",
                    "target_percent": 0.0,
                    "entered_at": "",
                    "updated_at": (now - timedelta(days=5)).isoformat(),
                },
                reason="seed_v4_hard_exit",
            ),
        ),
        applied_at=now,
    )
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "max_positions": 1,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "reentry_cooldown_days": 1,
            "hard_exit_cooldown_days": 7,
            "max_target_turnover_pct": None,
            "daily_turnover_budget_pct": None,
        }
    )
    blocked_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(symbol, now, score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )
    released_context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now + timedelta(days=7), (symbol,), close=50_000),
        portfolio=Portfolio(cash=10_000_000, cash_by_currency={"KRW": 10_000_000}),
        active_insights=(_allocator_insight(symbol, now + timedelta(days=7), score=0.60, momentum=0.26),),
        managed_symbols=(),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
        model_state=state_view,
    )

    assert model.create_targets(blocked_context) == ()
    assert model.create_targets(released_context)[0].target_percent > 0.0


def test_v4_banded_momentum_blocks_add_outside_entry_band():
    module = _load("sleeves/LEaps/portfolios/v4_banded_momentum.py")
    now = datetime(2026, 5, 20, 10, 0)
    top = Symbol("000660", "KRX")
    held = Symbol("005930", "KRX")
    portfolio = Portfolio(cash=9_000_000, cash_by_currency={"KRW": 9_000_000})
    portfolio.holdings[held.key] = Holding(held, quantity=20, average_price=50_000)
    model = module.create_portfolio_model(
        {
            "entry_top_n": 1,
            "hold_top_n": 2,
            "max_positions": 2,
            "gross_exposure": 0.80,
            "neutral_gross_exposure": 0.80,
            "weak_gross_exposure": 0.80,
            "max_position_pct": 0.80,
            "min_position_pct": 0.001,
            "add_requires_entry_band": True,
            "add_requires_unrealized_profit": True,
            "max_target_turnover_pct": None,
            "daily_turnover_budget_pct": None,
        }
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=_bars(now, (top, held), close=50_000),
        portfolio=portfolio,
        active_insights=(
            _allocator_insight(top, now, score=0.60, momentum=0.26),
            _allocator_insight(held, now, score=0.50, momentum=0.24),
        ),
        managed_symbols=(held,),
        target_portfolio_value_by_currency={"KRW": 10_000_000},
    )

    by_symbol = {target.symbol.key: target for target in model.create_targets(context)}

    assert by_symbol[held.key].target_percent == 0.10
    assert "hold_no_add" in by_symbol[held.key].tag


def test_kospi_conviction_alpha_filters_uncompensated_high_volatility():
    module = _load("sleeves/LEaps/alphas/kospi_conviction.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=100_000, fast=110_000, slow=90_000, momentum=0.25, momentum_5=0.05, vol=0.26),
            "KRX:000660": _values(close=150_000, fast=160_000, slow=120_000, momentum=0.62, momentum_5=0.15, vol=0.26),
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
    assert insights[0].metadata["entry_setup"] == "pullback"
    assert insights[0].metadata["momentum"] > 0


def test_kospi_pullback_reversion_alpha_emits_rebreak_setups():
    module = _load("sleeves/LEaps/alphas/kospi_pullback_reversion.py")
    now = datetime(2026, 5, 12)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=100_000,
                fast=99_500,
                slow=92_000,
                momentum=0.18,
                momentum_5=0.035,
                vol=0.06,
                rolling_high=102_000,
                rolling_low=94_000,
            ),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",))
    )

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].metadata["entry_setup"] == "rebreak"
    assert insights[0].metadata["pullback_from_high"] < 0.05


def test_kospi_pullback_reversion_alpha_requires_stabilization_in_volatility():
    module = _load("sleeves/LEaps/alphas/kospi_pullback_reversion.py")
    now = datetime(2026, 5, 12)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=97_000,
                fast=100_000,
                slow=88_000,
                momentum=0.18,
                momentum_5=-0.025,
                    vol=0.18,
                rolling_high=106_000,
                rolling_low=94_000,
            ),
            "KRX:000660": _values(
                close=100_000,
                fast=99_500,
                slow=90_000,
                momentum=0.20,
                momentum_5=0.035,
                    vol=0.18,
                rolling_high=102_000,
                rolling_low=93_000,
            ),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930", "KRX:000660"))
    )

    assert [insight.symbol.key for insight in insights] == ["KRX:000660"]
    assert insights[0].metadata["volatility_regime"] == "volatile_rebreak"


def test_kospi_swing_rebalance_alpha_buys_pullbacks_in_uptrend():
    module = _load("sleeves/LEaps/alphas/kospi_swing_rebalance.py")
    now = datetime(2026, 5, 14)
    values = _values(
        close=100_000,
        fast=99_000,
        slow=90_000,
        momentum=0.12,
        momentum_5=0.025,
        vol=0.05,
        rolling_high=104_000,
    )
    values["sma_10_close"] = 98_000
    snapshot = _snapshot(now, {"KRX:005930": values})

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].direction is InsightDirection.UP
    assert insights[0].alpha_id == "leaps-kospi-swing-rebalance"
    assert insights[0].reason == "kospi_swing_buy_pullback_in_uptrend"
    assert insights[0].metadata["portfolio_action"] == "buy_pullback"
    assert 0.018 <= insights[0].metadata["pullback_from_high"] <= 0.085


def test_kospi_swing_rebalance_alpha_partially_trims_ten_day_breaks():
    module = _load("sleeves/LEaps/alphas/kospi_swing_rebalance.py")
    now = datetime(2026, 5, 14)
    values = _values(
        close=99_000,
        fast=100_500,
        slow=90_000,
        momentum=0.11,
        momentum_5=-0.015,
        vol=0.05,
        rolling_high=104_000,
    )
    values["sma_10_close"] = 100_000
    snapshot = _snapshot(now, {"KRX:005930": values})

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].direction is InsightDirection.FLAT
    assert insights[0].reason == "kospi_swing_partial_trim_ten_day_break"
    assert insights[0].metadata["portfolio_action"] == "partial_trim"
    assert insights[0].metadata["target_multiplier"] == 0.50


def test_kospi_swing_rebalance_alpha_partially_trims_volatility_shocks():
    module = _load("sleeves/LEaps/alphas/kospi_swing_rebalance.py")
    now = datetime(2026, 5, 14)
    values = _values(
        close=100_000,
        fast=101_000,
        slow=90_000,
        momentum=0.11,
        momentum_5=-0.02,
        vol=0.22,
        rolling_high=120_000,
    )
    values["sma_10_close"] = 99_000
    snapshot = _snapshot(now, {"KRX:005930": values})

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].direction is InsightDirection.FLAT
    assert insights[0].reason == "kospi_swing_partial_trim_volatility_shock"
    assert insights[0].metadata["portfolio_action"] == "partial_trim"
    assert insights[0].metadata["target_multiplier"] == 0.55


def test_kospi_swing_rebalance_alpha_exits_twenty_day_breaks():
    module = _load("sleeves/LEaps/alphas/kospi_swing_rebalance.py")
    now = datetime(2026, 5, 14)
    values = _values(
        close=89_000,
        fast=92_000,
        slow=90_000,
        momentum=0.05,
        momentum_5=-0.03,
        vol=0.06,
        rolling_high=101_000,
    )
    values["sma_10_close"] = 92_000
    snapshot = _snapshot(now, {"KRX:005930": values})

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert [insight.symbol.key for insight in insights] == ["KRX:005930"]
    assert insights[0].direction is InsightDirection.FLAT
    assert insights[0].reason == "kospi_swing_exit_20dma_break"
    assert insights[0].metadata["portfolio_action"] == "exit"


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


def test_krx_etf_safety_alpha_moves_to_cash_and_inverse_after_shock():
    module = _load("sleeves/LEaps/alphas/krx_etf_safety.py")
    now = datetime(2026, 5, 15)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(
                close=117_200,
                fast=120_000,
                slow=106_566,
                momentum=0.11,
                momentum_5=-0.061,
                vol=0.05,
                rolling_high=124_855,
            ),
            "KRX:488770": _values(close=104_370, fast=104_300, slow=104_000, momentum=0.001, momentum_5=0.001, vol=0.001),
            "KRX:114800": _values(close=4_000, fast=3_950, slow=3_900, momentum=0.02, momentum_5=0.01, vol=0.05),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:069500", "KRX:488770", "KRX:114800"))
    )

    up_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.UP}
    assert set(up_by_symbol) == {"KRX:488770", "KRX:114800"}
    assert up_by_symbol["KRX:488770"].metadata["safety_regime"] == "shock"
    assert up_by_symbol["KRX:488770"].metadata["target_bucket_pct"] == 0.60
    assert up_by_symbol["KRX:114800"].metadata["target_bucket_pct"] == 0.20
    assert up_by_symbol["KRX:114800"].metadata["inverse_policy_action"] == "full"
    assert up_by_symbol["KRX:114800"].metadata["inverse_product_risk"] == "daily_reset_compounding_decay"
    assert up_by_symbol["KRX:488770"].metadata["stock_gross_cap"] == 0.20


def test_krx_etf_safety_alpha_decays_inverse_after_first_target_day():
    module = _load("sleeves/LEaps/alphas/krx_etf_safety.py")
    now = datetime(2026, 5, 16)
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            state_view.object_set(
                {
                    "target_active": True,
                    "last_target_date": "2026-05-15",
                    "target_day_count": 1,
                    "adjusted_inverse_pct": 0.20,
                    "policy_action": "full",
                },
                model_id=module.ALPHA_ID,
                namespace=module.INVERSE_POLICY_NAMESPACE,
                symbol_key=module.INVERSE_SYMBOL,
                reason="seed_inverse_policy",
                generated_at=now - timedelta(days=1),
            ),
        ),
        applied_at=now - timedelta(days=1),
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=117_200, fast=120_000, slow=106_566, momentum=0.11, momentum_5=-0.061, vol=0.05, rolling_high=124_855),
            "KRX:488770": _values(close=104_370, fast=104_300, slow=104_000, momentum=0.001, momentum_5=0.001, vol=0.001),
            "KRX:114800": _values(close=4_000, fast=3_950, slow=3_900, momentum=0.02, momentum_5=0.01, vol=0.05),
        },
    )
    context = SnapshotContext.from_indicator_snapshot(snapshot, model_state=state_view).with_input_symbols(
        ("KRX:069500", "KRX:488770", "KRX:114800")
    )

    insights = module.generate(context)

    up_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.UP}
    assert up_by_symbol["KRX:114800"].metadata["target_bucket_pct"] == 0.10
    assert up_by_symbol["KRX:114800"].metadata["inverse_target_day_count"] == 2
    assert up_by_symbol["KRX:114800"].metadata["inverse_policy_action"] == "decayed"
    assert up_by_symbol["KRX:488770"].metadata["target_bucket_pct"] == 0.70
    patches = module.state_patches(context=context, insights=tuple(insights))
    assert patches[0].value["target_day_count"] == 2
    assert patches[0].value["adjusted_inverse_pct"] == 0.10


def test_krx_etf_safety_alpha_blocks_inverse_after_max_target_days():
    module = _load("sleeves/LEaps/alphas/krx_etf_safety.py")
    now = datetime(2026, 5, 17)
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            state_view.object_set(
                {
                    "target_active": True,
                    "last_target_date": "2026-05-16",
                    "target_day_count": 2,
                    "adjusted_inverse_pct": 0.10,
                    "policy_action": "decayed",
                },
                model_id=module.ALPHA_ID,
                namespace=module.INVERSE_POLICY_NAMESPACE,
                symbol_key=module.INVERSE_SYMBOL,
                reason="seed_inverse_policy",
                generated_at=now - timedelta(days=1),
            ),
        ),
        applied_at=now - timedelta(days=1),
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=117_200, fast=120_000, slow=106_566, momentum=0.11, momentum_5=-0.061, vol=0.05, rolling_high=124_855),
            "KRX:488770": _values(close=104_370, fast=104_300, slow=104_000, momentum=0.001, momentum_5=0.001, vol=0.001),
            "KRX:114800": _values(close=4_000, fast=3_950, slow=3_900, momentum=0.02, momentum_5=0.01, vol=0.05),
        },
    )
    context = SnapshotContext.from_indicator_snapshot(snapshot, model_state=state_view).with_input_symbols(
        ("KRX:069500", "KRX:488770", "KRX:114800")
    )

    insights = module.generate(context)

    up_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.UP}
    flat_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.FLAT}
    assert "KRX:114800" not in up_by_symbol
    assert up_by_symbol["KRX:488770"].metadata["target_bucket_pct"] == 0.80
    assert flat_by_symbol["KRX:114800"].reason == "krx_etf_safety_inverse_holding_limit"
    assert flat_by_symbol["KRX:114800"].metadata["inverse_policy_action"] == "blocked_max_target_days"


def test_krx_etf_safety_alpha_rejects_leveraged_etfs_as_flat():
    module = _load("sleeves/LEaps/alphas/krx_etf_safety.py")
    now = datetime(2026, 5, 15)
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": _values(close=117_200, fast=120_000, slow=106_566, momentum=0.11, momentum_5=-0.061, vol=0.05, rolling_high=124_855),
            "KRX:122630": _values(close=65_000, fast=64_000, slow=63_000, momentum=0.04, momentum_5=0.02, vol=0.08),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:069500", "KRX:122630"))
    )

    flat_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.FLAT}
    assert flat_by_symbol["KRX:122630"].reason == "leveraged_etf_disabled_for_safety_bucket"
    assert flat_by_symbol["KRX:122630"].metadata["leveraged_etf_policy"] == "blocked"
    assert flat_by_symbol["KRX:122630"].metadata["product_risk"] == "daily_reset_compounding_decay"


def test_krx_etf_safety_alpha_uses_live_close_for_intraday_shock_without_rewriting_daily_reference():
    module = _load("sleeves/LEaps/alphas/krx_etf_safety.py")
    now = datetime(2026, 5, 15, 10, 45)
    benchmark_values = _values(
        close=124_800,
        fast=120_000,
        slow=106_566,
        momentum=0.11,
        momentum_5=0.02,
        vol=0.05,
        rolling_high=124_855,
    )
    benchmark_values["live_close"] = 117_200
    snapshot = _snapshot(
        now,
        {
            "KRX:069500": benchmark_values,
            "KRX:488770": _values(close=104_370, fast=104_300, slow=104_000, momentum=0.001, momentum_5=0.001, vol=0.001),
            "KRX:114800": _values(close=4_000, fast=3_950, slow=3_900, momentum=0.02, momentum_5=0.01, vol=0.05),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:069500", "KRX:488770", "KRX:114800"))
    )

    up_by_symbol = {insight.symbol.key: insight for insight in insights if insight.direction is InsightDirection.UP}
    assert up_by_symbol["KRX:488770"].metadata["safety_regime"] == "shock"
    assert up_by_symbol["KRX:488770"].metadata["benchmark_close"] == 124_800
    assert up_by_symbol["KRX:488770"].metadata["benchmark_live_close"] == 117_200
    assert up_by_symbol["KRX:488770"].metadata["benchmark_pullback_from_high"] > 0.055


def test_krx_etf_safety_selection_selects_domestic_etfs_not_stocks():
    module = _load("sleeves/LEaps/selections/krx_etf_safety.py")
    universe = parse_universe_definition(
        {
            "id": "krx-etf-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock"},
                {"ticker": "069500", "market": "KRX", "asset_type": "etf", "krw_safety_role": "benchmark"},
                {"ticker": "488770", "market": "KRX", "asset_type": "etf", "krw_safety_role": "cash_like"},
                {"ticker": "SPY", "market": "US", "asset_type": "etf"},
            ],
        }
    )

    result = module.KrxEtfSafetySelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="LEaps", universe=universe)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:488770", "KRX:069500"]
    assert result.rejected["KRX:005930"] == ("not_etf",)
    assert result.rejected["US:SPY"] == ("not_krx",)


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


def test_stock_momentum_selection_ignores_sector_metadata():
    module = _load("sleeves/LEaps/selections/stock_momentum.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "kr-sector-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock", "sector": "technology"},
                {"ticker": "000660", "market": "KRX", "asset_type": "stock", "sector": "technology"},
                {"ticker": "068270", "market": "KRX", "asset_type": "stock", "sector": "health_care"},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80_000, fast=82_000, slow=75_000, momentum=0.06, momentum_5=0.03, momentum_60=0.10, vol=0.03),
            "KRX:000660": _values(close=150_000, fast=160_000, slow=120_000, momentum=0.50, momentum_5=0.12, momentum_60=0.40, vol=0.04),
            "KRX:068270": _values(close=200_000, fast=205_000, slow=190_000, momentum=0.08, momentum_5=0.03, momentum_60=0.08, vol=0.03),
        },
    )

    result = module.StockMomentumSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="LEaps", universe=universe, indicator_snapshot=snapshot)
    )

    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:000660", "KRX:068270"]
    assert "sector" not in result.candidates["KRX:005930"].metadata
    assert "sector_relative_strength" not in result.candidates["KRX:005930"].metadata


def test_stock_momentum_selection_does_not_emit_sector_bonus_for_unknown_sector():
    module = _load("sleeves/LEaps/selections/stock_momentum.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "kr-unknown-sector-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock", "sector": "technology"},
                {"ticker": "011070", "market": "KRX", "asset_type": "stock"},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80_000, fast=82_000, slow=75_000, momentum=0.06, momentum_5=0.03, momentum_60=0.10, vol=0.03),
            "KRX:011070": _values(close=100_000, fast=112_000, slow=90_000, momentum=0.30, momentum_5=0.10, momentum_60=0.25, vol=0.05),
        },
    )

    result = module.StockMomentumSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="LEaps", universe=universe, indicator_snapshot=snapshot)
    )

    assert "sector" not in result.candidates["KRX:011070"].metadata
    assert "sector_relative_strength" not in result.candidates["KRX:011070"].metadata
    assert "sector_relative_strength" not in result.candidates["KRX:005930"].metadata


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


def test_kospi_growth_risk_equity_overlay_freezes_new_entries_after_drawdown():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 10, 30)
    samsung = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={samsung.key: Bar(samsung, now, 100_000, 100_000, 100_000, 100_000, 1000)},
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="regime_equity",
                    symbol_key="KRW",
                ),
                value={
                    "last_equity": 1_000_000,
                    "peak_equity": 1_000_000,
                    "trough_equity": 1_000_000,
                    "overlay": "none",
                },
                reason="seed_regime_equity",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(
        cash=760_000,
        cash_by_currency={"KRW": 760_000},
        holdings={samsung.key: Holding(samsung, quantity=2, average_price=100_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_equity_overlay_enabled": True,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(samsung, 6),),
            model_state=state_view,
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "regime_entry_freeze"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 2
    assert decision.metadata["market_regime"]["by_currency"]["KRW"]["overlay"] == "entry_freeze"
    assert batch.state_patches[0].key.namespace == "regime_equity"
    assert batch.state_patches[0].value["overlay"] == "entry_freeze"


def test_kospi_growth_risk_equity_overlay_caps_deeper_risk_off_exposure():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 13, 0)
    samsung = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={samsung.key: Bar(samsung, now, 100_000, 100_000, 100_000, 100_000, 1000)},
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="regime_equity",
                    symbol_key="KRW",
                ),
                value={
                    "last_equity": 1_000_000,
                    "peak_equity": 1_000_000,
                    "trough_equity": 1_000_000,
                    "overlay": "none",
                },
                reason="seed_regime_equity",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(
        cash=40_000,
        cash_by_currency={"KRW": 40_000},
        holdings={samsung.key: Holding(samsung, quantity=9, average_price=100_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"neutral": 1.0}},
            "regime_equity_overlay_enabled": True,
            "risk_off_cap_pct_by_currency": {"KRW": 0.70},
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(samsung, 9),),
            model_state=state_view,
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "currency_policy_clamped"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 6
    assert decision.metadata["market_regime"]["by_currency"]["KRW"]["overlay"] == "intraday_risk_off"
    assert decision.metadata["max_total_exposure_pct"] == 0.70


def test_kospi_growth_risk_intraday_guard_blocks_stock_entries_but_allows_cash_like_etf():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 9, 5)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    guard = Symbol("069500", "KRX")
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 90_000, 90_000, 90_000, 90_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_risk_off_return_pct": -0.04,
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.35},
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 5), PortfolioTarget(cash_like, 2)),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
        )
    )

    approved = {decision.original_target.symbol.key: decision.approved_target.quantity for decision in batch.decisions}
    assert approved == {stock.key: 0, cash_like.key: 2}
    assert batch.decisions[0].reason == "regime_entry_freeze"
    assert batch.decisions[0].metadata["market_regime"]["intraday_market_guard_active"] is True
    assert batch.decisions[1].metadata["market_regime"]["by_currency"]["KRW"]["overlay"] == "intraday_risk_off"


def test_kospi_growth_risk_intraday_guard_smoothly_caps_entries_when_enabled():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 10, 0)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    guard = Symbol("069500", "KRX")
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 98_500, 98_500, 98_500, 98_500, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_entry_freeze_return_pct": -0.01,
            "intraday_risk_off_return_pct": -0.02,
            "intraday_entry_freeze_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.25},
            "intraday_guard_smoothing_enabled": True,
            "intraday_guard_cap_curve": "linear",
            "intraday_guard_hard_entry_freeze": False,
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 8), PortfolioTarget(cash_like, 2)),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
        )
    )

    approved = {decision.original_target.symbol.key: decision.approved_target.quantity for decision in batch.decisions}
    regime = batch.decisions[0].metadata["market_regime"]["by_currency"]["KRW"]
    assert approved == {stock.key: 3, cash_like.key: 2}
    assert batch.decisions[0].reason == "currency_policy_clamped"
    assert regime["overlay"] == "entry_freeze"
    assert regime["entry_freeze"] is False
    assert regime["hard_entry_freeze"] is False
    assert regime["smoothing_enabled"] is True
    assert regime["max_total_exposure_pct"] == pytest.approx(0.35)


def test_kospi_growth_risk_intraday_guard_releases_probe_cap_after_confirmed_rebound():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 13, 0)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    guard = Symbol("069500", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="intraday_guard",
                    symbol_key=guard.key,
                ),
                value={
                    "session_date": now.date().isoformat(),
                    "session_high_price": 100_000,
                    "session_low_price": 96_000,
                    "current_price": 96_600,
                    "recovery_count": 1,
                    "overlay": "intraday_risk_off",
                },
                reason="seed_intraday_recovery",
            ),
        ),
        applied_at=now,
    )
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 97_000, 97_000, 97_000, 97_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_entry_freeze_return_pct": -0.01,
            "intraday_risk_off_return_pct": -0.02,
            "intraday_entry_freeze_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.25},
            "intraday_guard_smoothing_enabled": True,
            "intraday_guard_hard_entry_freeze": False,
            "intraday_guard_recovery_enabled": True,
            "intraday_guard_recovery_from_low_pct": 0.006,
            "intraday_guard_recovery_confirmation_cycles": 2,
            "intraday_guard_recovery_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 8),),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
            model_state=state_view,
        )
    )

    decision = batch.decisions[0]
    regime = decision.metadata["market_regime"]["by_currency"]["KRW"]
    assert decision.reason == "currency_policy_clamped"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 4
    assert regime["overlay"] == "entry_freeze"
    assert regime["trigger"] == "recovery_from_session_low"
    assert regime["underlying_trigger"] == "reference_return"
    assert regime["max_total_exposure_pct"] == pytest.approx(0.45)
    assert regime["recovery_release_active"] is True
    assert batch.state_patches[0].value["session_low_price"] == 96_000
    assert batch.state_patches[0].value["recovery_count"] == 2


def test_kospi_growth_risk_intraday_guard_waits_for_recovery_confirmation():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 13, 1)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    guard = Symbol("069500", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="intraday_guard",
                    symbol_key=guard.key,
                ),
                value={
                    "session_date": now.date().isoformat(),
                    "session_high_price": 100_000,
                    "session_low_price": 96_000,
                    "current_price": 96_600,
                    "recovery_count": 0,
                    "overlay": "intraday_risk_off",
                },
                reason="seed_intraday_recovery",
            ),
        ),
        applied_at=now,
    )
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 97_000, 97_000, 97_000, 97_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_entry_freeze_return_pct": -0.01,
            "intraday_risk_off_return_pct": -0.02,
            "intraday_entry_freeze_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.25},
            "intraday_guard_smoothing_enabled": True,
            "intraday_guard_hard_entry_freeze": False,
            "intraday_guard_recovery_enabled": True,
            "intraday_guard_recovery_from_low_pct": 0.006,
            "intraday_guard_recovery_confirmation_cycles": 2,
            "intraday_guard_recovery_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 8),),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
            model_state=state_view,
        )
    )

    decision = batch.decisions[0]
    regime = decision.metadata["market_regime"]["by_currency"]["KRW"]
    assert decision.reason == "currency_policy_clamped"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 2
    assert regime["overlay"] == "intraday_risk_off"
    assert regime["trigger"] == "reference_return"
    assert regime["max_total_exposure_pct"] == pytest.approx(0.25)
    assert regime["recovery_release_active"] is False
    assert batch.state_patches[0].value["recovery_count"] == 1


def test_kospi_growth_risk_symbol_guard_blocks_adding_to_losing_holding():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 30)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 101_000, 98_000, 98_000, 1000)},
    )
    portfolio = Portfolio(
        cash=1_000_000,
        cash_by_currency={"KRW": 1_000_000},
        holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_entry_block_intraday_return_pct": -0.025,
            "symbol_entry_block_high_drawdown_pct": -0.04,
            "symbol_entry_block_unrealized_loss_pct": -0.015,
            "symbol_reduce_half_unrealized_loss_pct": -0.035,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(stock, 15),),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_entry_block"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 10
    assert decision.metadata["trigger"] == "unrealized_loss"
    assert decision.metadata["unrealized_pnl_pct"] == pytest.approx(-0.02)


def test_kospi_growth_risk_symbol_guard_blocks_add_on_sma_break_without_selling():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 32)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 101_000, 99_400, 99_400, 1000)},
    )
    portfolio = Portfolio(
        cash=1_000_000,
        cash_by_currency={"KRW": 1_000_000},
        holdings={stock.key: Holding(stock, quantity=10, average_price=98_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_entry_block_sma10_buffer_pct": -0.005,
            "symbol_entry_block_sma20_buffer_pct": -0.005,
            "symbol_reduce_half_sma10_buffer_pct": -0.20,
            "symbol_exit_sma20_buffer_pct": -0.20,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(stock, 15),),
            active_insights=(
                Insight(
                    sleeve_id="LEaps",
                    symbol=stock,
                    direction=InsightDirection.UP,
                    generated_at=now,
                    source_snapshot_id="test",
                    alpha_id="leaps-kospi-swing-rebalance",
                    alpha_version="0.1.0",
                    metadata={"sma10": 100_000.0, "sma20": 98_000.0, "market_breadth": 0.7, "momentum": 0.2},
                ),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_entry_block"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 10
    assert decision.metadata["trigger"] == "sma10_add_block"


def test_kospi_growth_risk_symbol_guard_reduces_half_on_deeper_loss():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 35)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 100_000, 96_000, 96_000, 1000)},
    )
    portfolio = Portfolio(
        cash=1_000_000,
        cash_by_currency={"KRW": 1_000_000},
        holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_reduce_half_unrealized_loss_pct": -0.035,
            "symbol_exit_unrealized_loss_pct": -0.06,
            "symbol_reduce_fraction": 0.5,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(stock, 15),),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_reduce_half"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 5
    assert decision.metadata["trigger"] == "unrealized_loss"
    assert batch.state_patches[0].key.namespace == "symbol_guard"
    assert batch.state_patches[0].value["anchor_quantity"] == 10


def test_kospi_growth_risk_symbol_guard_tightens_low_volatility_loss_guard():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 35)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 100_000, 97_000, 97_000, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_reduce_half_unrealized_loss_pct": -0.035,
            "symbol_exit_unrealized_loss_pct": -0.06,
            "symbol_reduce_fraction": 0.5,
            "symbol_guard_volatility_adjusted_enabled": True,
            "symbol_guard_reference_volatility_pct": 0.04,
            "symbol_guard_min_volatility_multiplier": 0.75,
            "symbol_guard_max_volatility_multiplier": 1.75,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=1_000_000,
                cash_by_currency={"KRW": 1_000_000},
                holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
            ),
            targets=(PortfolioTarget(stock, 15),),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.02),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_reduce_half"
    assert decision.metadata["volatility_multiplier"] == pytest.approx(0.75)
    assert decision.metadata["thresholds"]["reduce_half_unrealized_loss_pct"] == pytest.approx(-0.02625)


def test_kospi_growth_risk_symbol_guard_allows_wider_noise_for_high_volatility_holding():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 35)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 100_000, 96_000, 96_000, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_reduce_half_unrealized_loss_pct": -0.035,
            "symbol_exit_unrealized_loss_pct": -0.06,
            "symbol_reduce_fraction": 0.5,
            "symbol_guard_volatility_adjusted_enabled": True,
            "symbol_guard_reference_volatility_pct": 0.04,
            "symbol_guard_min_volatility_multiplier": 0.75,
            "symbol_guard_max_volatility_multiplier": 1.75,
        }
    )

    context = RiskManagementContext(
        sleeve_id="LEaps",
        data=data,
        portfolio=Portfolio(
            cash=1_000_000,
            cash_by_currency={"KRW": 1_000_000},
            holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
        ),
        targets=(PortfolioTarget(stock, 10),),
        active_insights=(
            _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
        ),
    )
    batch = model.manage_risk(context)

    decision = batch.decisions[0]
    assert decision.reason == "approved"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 10
    metrics = model._symbol_guard_metrics(context, PortfolioTarget(stock, 10), 96_000, 10)
    assert metrics["volatility_multiplier"] == pytest.approx(1.75)
    assert metrics["thresholds"]["reduce_half_unrealized_loss_pct"] == pytest.approx(-0.06125)


def test_kospi_growth_risk_symbol_guard_caps_high_volatility_entry_looseness():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 35)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 100_000, 96_700, 96_700, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_entry_block_intraday_return_pct": -0.025,
            "symbol_guard_volatility_adjusted_enabled": True,
            "symbol_guard_reference_volatility_pct": 0.04,
            "symbol_guard_min_volatility_multiplier": 0.75,
            "symbol_guard_max_volatility_multiplier": 1.75,
            "symbol_guard_entry_max_volatility_multiplier": 1.25,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 5),),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_entry_block"
    assert decision.metadata["trigger"] == "intraday_return"
    assert decision.metadata["entry_volatility_multiplier"] == pytest.approx(1.25)
    assert decision.metadata["thresholds"]["entry_block_intraday_return_pct"] == pytest.approx(-0.03125)


def test_kospi_growth_risk_symbol_guard_allows_partial_add_on_strong_pullback():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 22, 11, 13)
    stock = Symbol("036930", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 185_300, 215_000, 185_200, 197_700, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_entry_block_high_drawdown_pct": -0.055,
            "symbol_reduce_half_high_drawdown_pct": -0.09,
            "symbol_exit_high_drawdown_pct": -0.12,
            "symbol_guard_volatility_adjusted_enabled": True,
            "symbol_guard_reference_volatility_pct": 0.04,
            "symbol_guard_max_volatility_multiplier": 1.25,
            "symbol_guard_entry_max_volatility_multiplier": 1.25,
            "symbol_pullback_add_enabled": True,
            "symbol_pullback_add_fraction": 0.5,
            "symbol_pullback_add_min_alpha_count": 2,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=10_000_000,
                cash_by_currency={"KRW": 10_000_000},
                holdings={stock.key: Holding(stock, quantity=1, average_price=190_800)},
            ),
            targets=(PortfolioTarget(stock, 14),),
            active_insights=(
                Insight(
                    sleeve_id="LEaps",
                    symbol=stock,
                    direction=InsightDirection.UP,
                    generated_at=now,
                    source_snapshot_id="test",
                    alpha_id="leaps-kospi-conviction",
                    alpha_version="0.1.0",
                    metadata={
                        "sma10": 165_260.0,
                        "sma20": 145_540.0,
                        "volatility": 0.12588149763722284,
                    },
                ),
                Insight(
                    sleeve_id="LEaps",
                    symbol=stock,
                    direction=InsightDirection.UP,
                    generated_at=now,
                    source_snapshot_id="test",
                    alpha_id="leaps-kospi-pullback-reversion",
                    alpha_version="0.1.0",
                    metadata={},
                ),
                Insight(
                    sleeve_id="LEaps",
                    symbol=stock,
                    direction=InsightDirection.UP,
                    generated_at=now,
                    source_snapshot_id="test",
                    alpha_id="leaps-kospi-swing-rebalance",
                    alpha_version="0.1.0",
                    metadata={},
                ),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_pullback_add"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 8
    assert decision.metadata["trigger"] == "session_high_drawdown"
    assert decision.metadata["drawdown_from_session_high"] == pytest.approx(-0.08046511627906971)
    assert decision.metadata["thresholds"]["entry_block_high_drawdown_pct"] == pytest.approx(-0.06875)


def test_kospi_growth_risk_symbol_guard_stages_rebalance_on_agent_pullback_without_alpha_metadata():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 28, 15, 39)
    stock = Symbol("036930", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 234_500, 235_500, 201_000, 208_000, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_entry_block_intraday_return_pct": -0.12,
            "symbol_entry_block_high_drawdown_pct": -0.10,
            "symbol_entry_block_unrealized_loss_pct": -0.08,
            "symbol_reduce_half_unrealized_loss_pct": -0.07,
            "symbol_exit_unrealized_loss_pct": -0.10,
            "symbol_reduce_half_high_drawdown_pct": -0.16,
            "symbol_exit_high_drawdown_pct": -0.22,
            "symbol_pullback_add_enabled": True,
            "symbol_pullback_add_fraction": 0.35,
            "symbol_pullback_add_min_intraday_return_pct": -0.12,
            "symbol_pullback_add_min_unrealized_pnl_pct": -0.06,
            "symbol_pullback_add_min_alpha_count": 0,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=10_000_000,
                cash_by_currency={"KRW": 10_000_000},
                holdings={stock.key: Holding(stock, quantity=5, average_price=217_870)},
            ),
            targets=(PortfolioTarget(stock, 11),),
            active_insights=(),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_pullback_add"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 8
    assert decision.metadata["trigger"] == "session_high_drawdown"
    assert decision.metadata["signal_alpha_ids"] == []


def test_kospi_growth_risk_symbol_guard_does_not_repeat_half_reduce_after_state_mark():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 36)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 100_000, 96_000, 96_000, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_reduce_half_unrealized_loss_pct": -0.035,
            "symbol_exit_unrealized_loss_pct": -0.06,
            "symbol_reduce_fraction": 0.5,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    first = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=1_000_000,
                cash_by_currency={"KRW": 1_000_000},
                holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
            ),
            targets=(PortfolioTarget(stock, 15),),
            model_state=state_view,
        )
    )
    store.apply_patches(first.state_patches, applied_at=now)

    second = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=1_000_000,
                cash_by_currency={"KRW": 1_000_000},
                holdings={stock.key: Holding(stock, quantity=5, average_price=100_000)},
            ),
            targets=(PortfolioTarget(stock, 15),),
            model_state=state_view,
        )
    )

    decision = second.decisions[0]
    assert decision.reason == "symbol_guard_reduce_half"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 5
    assert decision.metadata["already_reduced"] is True
    assert decision.metadata["anchor_quantity"] == 10


def test_kospi_growth_risk_symbol_guard_clear_preserves_last_risk_event():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 38)
    event_at = now - timedelta(minutes=20)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 101_000, 99_000, 100_000, 1000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_guard_recovery_confirmation_cycles": 1,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="symbol_guard",
                    symbol_key=stock.key.upper(),
                ),
                value={
                    "status": "recovering",
                    "last_risk_status": "exited",
                    "last_risk_trigger": "sma20_break",
                    "last_risk_event_at": event_at.isoformat(),
                    "updated_at": (now - timedelta(minutes=1)).isoformat(),
                },
                reason="seed_recovering_symbol_guard",
            ),
        ),
        applied_at=now,
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=1_000_000,
                cash_by_currency={"KRW": 1_000_000},
                holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
            ),
            targets=(PortfolioTarget(stock, 10),),
            model_state=state_view,
        )
    )
    store.apply_patches(batch.state_patches, applied_at=now)
    record = state_view.get(
        model_id="leaps-kospi-growth-us-hedge-risk",
        namespace="symbol_guard",
        symbol_key=stock.key.upper(),
    )

    assert record is not None
    assert record.value["status"] == "clear"
    assert record.value["last_risk_status"] == "exited"
    assert record.value["last_risk_trigger"] == "sma20_break"
    assert record.value["last_risk_event_at"] == event_at.isoformat()


def test_kospi_growth_risk_symbol_guard_exits_on_twenty_day_break():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 10, 40)
    stock = Symbol("417840", "KRX")
    data = DataSlice(
        time=now,
        bars={stock.key: Bar(stock, now, 100_000, 101_000, 99_400, 99_400, 1000)},
    )
    portfolio = Portfolio(
        cash=1_000_000,
        cash_by_currency={"KRW": 1_000_000},
        holdings={stock.key: Holding(stock, quantity=10, average_price=100_000)},
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "symbol_guard_enabled": True,
            "symbol_exit_sma20_buffer_pct": -0.005,
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(PortfolioTarget(stock, 10),),
            active_insights=(
                Insight(
                    sleeve_id="LEaps",
                    symbol=stock,
                    direction=InsightDirection.UP,
                    generated_at=now,
                    source_snapshot_id="test",
                    alpha_id="leaps-kospi-swing-rebalance",
                    alpha_version="0.1.0",
                    metadata={"sma20": 100_000.0, "market_breadth": 0.7, "momentum": 0.2, "volatility": 0.08},
                ),
            ),
        )
    )

    decision = batch.decisions[0]
    assert decision.reason == "symbol_guard_exit"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 0
    assert "risk:symbol_guard_exit" in decision.approved_target.tag
    assert decision.metadata["trigger"] == "sma20_break"


def test_kospi_growth_risk_intraday_high_drawdown_blocks_reversal_entries():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 10, 5)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    guard = Symbol("069500", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="intraday_guard",
                    symbol_key=guard.key,
                ),
                value={
                    "session_date": now.date().isoformat(),
                    "session_high_price": 105_000,
                },
                reason="seed_intraday_guard_high",
            ),
        )
    )
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 103_000, 103_000, 103_000, 103_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_entry_freeze_return_pct": -0.02,
            "intraday_risk_off_return_pct": -0.04,
            "intraday_guard_high_drawdown_enabled": True,
            "intraday_guard_high_entry_freeze_return_pct": -0.005,
            "intraday_guard_high_risk_off_return_pct": -0.012,
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.35},
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
            targets=(PortfolioTarget(stock, 5), PortfolioTarget(cash_like, 2)),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
            model_state=state_view,
        )
    )

    approved = {decision.original_target.symbol.key: decision.approved_target.quantity for decision in batch.decisions}
    regime = batch.decisions[0].metadata["market_regime"]
    assert approved == {stock.key: 0, cash_like.key: 2}
    assert regime["by_currency"]["KRW"]["trigger"] == "session_high_drawdown"
    assert regime["by_currency"]["KRW"]["overlay"] == "intraday_risk_off"
    assert batch.state_patches[0].key.namespace == "intraday_guard"
    assert batch.state_patches[0].value["session_high_price"] == 105_000


def test_kospi_growth_risk_intraday_guard_caps_stocks_without_blocking_safety_hedges():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 15, 10, 30)
    stock = Symbol("005930", "KRX")
    cash_like = Symbol("488770", "KRX")
    inverse = Symbol("114800", "KRX")
    guard = Symbol("069500", "KRX")
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="intraday_guard",
                    symbol_key=guard.key,
                ),
                value={
                    "session_date": now.date().isoformat(),
                    "session_high_price": 105_000,
                },
                reason="seed_intraday_guard_high",
            ),
        )
    )
    data = DataSlice(
        time=now,
        bars={
            stock.key: Bar(stock, now, 100_000, 100_000, 100_000, 100_000, 1000),
            cash_like.key: Bar(cash_like, now, 100_000, 100_000, 100_000, 100_000, 1000),
            inverse.key: Bar(inverse, now, 100_000, 100_000, 100_000, 100_000, 1000),
            guard.key: Bar(guard, now, 102_000, 102_000, 102_000, 102_000, 1000),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_guard_high_drawdown_enabled": True,
            "intraday_guard_high_risk_off_return_pct": -0.012,
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.25},
            "intraday_guard_exempt_symbols": [cash_like.key, inverse.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=Portfolio(
                cash=1_000_000,
                cash_by_currency={"KRW": 1_000_000},
                holdings={stock.key: Holding(stock, quantity=8, average_price=100_000)},
            ),
            targets=(PortfolioTarget(cash_like, 4), PortfolioTarget(inverse, 2), PortfolioTarget(stock, 8)),
            active_insights=(
                _regime_insight(stock, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=100_000),
            ),
            model_state=state_view,
        )
    )

    approved = {decision.original_target.symbol.key: decision.approved_target.quantity for decision in batch.decisions}
    regime = batch.decisions[0].metadata["market_regime"]
    assert approved == {cash_like.key: 4, inverse.key: 2, stock.key: 4}
    assert regime["by_currency"]["KRW"]["overlay"] == "intraday_risk_off"
    assert regime["by_currency"]["KRW"]["max_total_exposure_pct"] == 0.25
    assert batch.decisions[2].reason == "currency_policy_clamped"


def test_kospi_growth_risk_intraday_cap_deleverages_existing_holdings_without_new_targets():
    module = _load("sleeves/LEaps/risks/kospi_growth_us_hedge.py")
    now = datetime(2026, 5, 19, 14, 50)
    weak = Symbol("011070", "KRX")
    strong = Symbol("032830", "KRX")
    guard = Symbol("069500", "KRX")
    cash_like = Symbol("488770", "KRX")
    data = DataSlice(
        time=now,
        bars={
            weak.key: Bar(weak, now, 790_000, 790_000, 790_000, 790_000, 1000),
            strong.key: Bar(strong, now, 313_500, 313_500, 313_500, 313_500, 1000),
            guard.key: Bar(guard, now, 117_840, 116_480, 111_755, 114_750, 1000),
        },
    )
    portfolio = Portfolio(
        cash=9_351_007,
        cash_by_currency={"KRW": 9_351_007},
        holdings={
            weak.key: Holding(weak, quantity=2, average_price=809_000),
            strong.key: Holding(strong, quantity=8, average_price=312_375),
        },
    )
    model = module.create_risk_model(
        {
            "max_position_pct_by_currency": {"KRW": 1.0},
            "max_total_exposure_pct_by_currency": {"KRW": 1.0},
            "cash_buffer_pct_by_currency": {"KRW": 0.0},
            "regime_exposure_enabled": True,
            "regime_total_exposure_pct_by_currency": {"KRW": {"strong_risk_on": 1.0}},
            "intraday_market_guard_enabled": True,
            "intraday_guard_symbol": guard.key,
            "intraday_entry_freeze_return_pct": -0.01,
            "intraday_risk_off_return_pct": -0.02,
            "intraday_entry_freeze_cap_pct_by_currency": {"KRW": 0.45},
            "intraday_risk_off_cap_pct_by_currency": {"KRW": 0.25},
            "intraday_guard_smoothing_enabled": True,
            "intraday_guard_hard_entry_freeze": False,
            "intraday_guard_exempt_symbols": [cash_like.key],
        }
    )

    batch = model.manage_risk(
        RiskManagementContext(
            sleeve_id="LEaps",
            data=data,
            portfolio=portfolio,
            targets=(),
            active_insights=(
                _regime_insight(weak, now, breadth=0.70, momentum=0.20, volatility=0.08),
                _regime_insight(strong, now, breadth=0.70, momentum=0.22, volatility=0.08),
                _intraday_guard_reference_insight(cash_like, now, guard_symbol=guard.key, reference_price=117_840),
            ),
        )
    )

    assert len(batch.decisions) == 1
    decision = batch.decisions[0]
    assert decision.original_target.symbol == weak
    assert decision.original_target.tag == "risk:exposure_cap_deleverage"
    assert decision.reason == "currency_policy_clamped"
    assert decision.approved_target is not None
    assert decision.approved_target.quantity == 1
    assert decision.metadata["max_total_exposure_pct"] == 0.25


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


def test_leaps_execution_subtracts_open_buy_from_unordered_quantity():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    portfolio = Portfolio(
        cash=3_000_000,
        holdings={symbol.key: Holding(symbol, quantity=70, average_price=90_000)},
    )
    pending = PendingOrderState.from_order_tickets(
        (
            _order_ticket(
                symbol,
                side=OrderSide.BUY,
                quantity=30,
                sleeve_id="LEaps",
                created_at=now,
            ),
        ),
        sleeve_id="LEaps",
        as_of=now,
    )
    model = module.create_execution_model(
        {"max_slice_notional": 10_000_000, "max_daily_volume_participation_bps": 10_000}
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        [PortfolioTarget(symbol, 100, tag="entry")],
        execution_context=ExecutionContext(
            sleeve_id="LEaps",
            generated_at=now,
            portfolio=portfolio,
            data=data,
            approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
            pending_orders=pending,
            target_batch_id="order-sizing-1",
            source_target_batch_id="portfolio-target-1",
        ),
    )

    assert orders == []


def test_leaps_execution_orders_only_open_buy_remaining_quantity():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    portfolio = Portfolio(
        cash=3_000_000,
        holdings={symbol.key: Holding(symbol, quantity=70, average_price=90_000)},
    )
    pending = PendingOrderState.from_order_tickets(
        (
            _order_ticket(
                symbol,
                side=OrderSide.BUY,
                quantity=10,
                sleeve_id="LEaps",
                created_at=now,
            ),
        ),
        sleeve_id="LEaps",
        as_of=now,
    )
    model = module.create_execution_model(
        {"max_slice_notional": 10_000_000, "max_daily_volume_participation_bps": 10_000}
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        [PortfolioTarget(symbol, 100, tag="entry")],
        execution_context=ExecutionContext(
            sleeve_id="LEaps",
            generated_at=now,
            portfolio=portfolio,
            data=data,
            approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
            pending_orders=pending,
            target_batch_id="order-sizing-1",
            source_target_batch_id="portfolio-target-1",
        ),
    )

    assert [order.quantity for order in orders] == [20]
    assert orders[0].metadata["raw_delta_quantity"] == 30
    assert orders[0].metadata["unordered_delta_quantity"] == 20
    assert orders[0].metadata["pending_buy_quantity"] == 10
    assert orders[0].metadata["projected_quantity"] == 80
    assert orders[0].metadata["target_batch_id"] == "order-sizing-1"
    assert orders[0].metadata["source_target_batch_id"] == "portfolio-target-1"


def test_leaps_execution_hard_exit_can_bypass_pending_sell_suppression():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=100, average_price=90_000)})
    pending = PendingOrderState.from_order_tickets(
        (
            _order_ticket(
                symbol,
                side=OrderSide.SELL,
                quantity=100,
                sleeve_id="LEaps",
                created_at=now,
            ),
        ),
        sleeve_id="LEaps",
        as_of=now,
    )
    model = module.create_execution_model(
        {"max_slice_notional": 10_000_000, "max_daily_volume_participation_bps": 10_000}
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        [PortfolioTarget(symbol, 0, tag="hard_exit:trailing_stop")],
        execution_context=ExecutionContext(
            sleeve_id="LEaps",
            generated_at=now,
            portfolio=portfolio,
            data=data,
            approved_targets=(PortfolioTarget(symbol, 0, tag="hard_exit:trailing_stop"),),
            pending_orders=pending,
        ),
    )

    assert [order.quantity for order in orders] == [100]
    assert orders[0].side is OrderSide.SELL
    assert orders[0].metadata["unordered_quantity_bypassed"] is True
    assert orders[0].metadata["pending_sell_quantity"] == 100


def test_leaps_execution_v41_suppresses_reused_target_adds_after_target_seen():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "reused_target_suppress_buy_add": True,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    first_portfolio = Portfolio(
        cash=3_000_000,
        holdings={symbol.key: Holding(symbol, quantity=70, average_price=90_000)},
    )
    first_context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=first_portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-1",
        source_target_batch_id="portfolio-target-1",
    )

    first_orders = tuple(
        model.create_orders(
            "LEaps",
            first_portfolio,
            data,
            list(first_context.approved_targets),
            execution_context=first_context,
        )
    )
    store.apply_patches(model.state_patches(context=first_context, orders=first_orders), applied_at=now)
    second_portfolio = Portfolio(
        cash=3_000_000,
        holdings={symbol.key: Holding(symbol, quantity=100, average_price=90_000)},
    )
    second_context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now + timedelta(minutes=1),
        portfolio=second_portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 105, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-2",
        source_target_batch_id="portfolio-target-1",
    )

    second_orders = model.create_orders(
        "LEaps",
        second_portfolio,
        data,
        list(second_context.approved_targets),
        execution_context=second_context,
    )

    assert [order.quantity for order in first_orders] == [30]
    assert second_orders == []


def test_leaps_execution_v41_allows_adds_on_fresh_target_batch():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "reused_target_suppress_buy_add": True,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={"source_target_batch_id": "portfolio-target-old", "target_quantity": 100},
                reason="seed_prior_target",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(
        cash=3_000_000,
        holdings={symbol.key: Holding(symbol, quantity=100, average_price=90_000)},
    )
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 105, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-1",
        source_target_batch_id="portfolio-target-new",
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        list(context.approved_targets),
        execution_context=context,
    )

    assert [order.quantity for order in orders] == [5]
    assert orders[0].metadata["reused_source_target_seen"] is False


def test_leaps_execution_v41_suppresses_small_reused_target_sells():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "reused_target_suppress_buy_add": True,
            "reused_target_sell_no_trade_max_quantity_delta": 2,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={"source_target_batch_id": "portfolio-target-1", "target_quantity": 100},
                reason="seed_prior_target",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=100, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 99, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-1",
        source_target_batch_id="portfolio-target-1",
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        list(context.approved_targets),
        execution_context=context,
    )

    assert orders == []


def test_leaps_execution_v41_allows_large_reused_target_sells():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "reused_target_suppress_buy_add": True,
            "reused_target_sell_no_trade_max_quantity_delta": 2,
            "reused_target_sell_no_trade_max_notional": 300_000,
            "reused_target_sell_no_trade_pct_of_target": 0.05,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={"source_target_batch_id": "portfolio-target-1", "target_quantity": 100},
                reason="seed_prior_target",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=100, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 50, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-1",
        source_target_batch_id="portfolio-target-1",
    )

    orders = model.create_orders(
        "LEaps",
        portfolio,
        data,
        list(context.approved_targets),
        execution_context=context,
    )

    assert [order.quantity for order in orders] == [50]
    assert orders[0].side is OrderSide.SELL
    assert orders[0].metadata["reused_source_target_seen"] is True


def test_leaps_execution_v42_suppresses_small_buyback_after_recent_sell():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "opposite_rebalance_cooldown_minutes": 60,
            "opposite_rebalance_no_trade_max_quantity_delta": 2,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 26,
                    "last_order_side": "sell",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_buy",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=1_000_000, holdings={symbol.key: Holding(symbol, quantity=26, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 27, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "opposite_rebalance_cooldown"


def test_leaps_execution_v42_allows_same_direction_rebalance_adds():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "opposite_rebalance_cooldown_minutes": 60,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 27,
                    "last_order_side": "buy",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_buy",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=1_000_000, holdings={symbol.key: Holding(symbol, quantity=27, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 31, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)

    assert [order.quantity for order in orders] == [4]
    assert orders[0].side is OrderSide.BUY


def test_leaps_execution_v42_allows_large_buyback_after_recent_sell():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "opposite_rebalance_cooldown_minutes": 60,
            "opposite_rebalance_no_trade_max_quantity_delta": 2,
            "opposite_rebalance_no_trade_max_notional": 300_000,
            "opposite_rebalance_no_trade_pct_of_position": 0.05,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 50,
                    "last_order_side": "sell",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_buy",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=10_000_000, holdings={symbol.key: Holding(symbol, quantity=50, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)

    assert [order.quantity for order in orders] == [50]
    assert orders[0].side is OrderSide.BUY


def test_leaps_execution_can_block_any_opposite_rebalance_during_cooldown():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "opposite_rebalance_cooldown_minutes": 60,
            "opposite_rebalance_require_small_change": False,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 50,
                    "last_order_side": "sell",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_sell",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=10_000_000, holdings={symbol.key: Holding(symbol, quantity=50, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)

    assert orders == []


def test_leaps_execution_v42_blocks_buyback_after_symbol_guard_reduction():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "risk_reentry_cooldown_minutes": 60,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="symbol_guard",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "status": "reduced",
                    "anchor_quantity": 100,
                    "last_approved_quantity": 50,
                    "updated_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_risk_reduction",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=50, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "risk_guard_reentry_cooldown"


def test_leaps_execution_blocks_buyback_during_symbol_guard_recovery():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "risk_reentry_cooldown_minutes": 60,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="symbol_guard",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "status": "recovering",
                    "anchor_quantity": None,
                    "last_approved_quantity": 100,
                    "recovery_confirmation_count": 2,
                    "updated_at": (now - timedelta(minutes=5)).isoformat(),
                },
                reason="seed_symbol_guard_recovery",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=50, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)

    assert orders == []


def test_leaps_execution_blocks_buyback_after_symbol_guard_clears_with_recent_event():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "risk_reentry_cooldown_minutes": 60,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-kospi-growth-us-hedge-risk",
                    namespace="symbol_guard",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "status": "clear",
                    "last_approved_quantity": 50,
                    "last_risk_status": "exited",
                    "last_risk_trigger": "sma20_break",
                    "last_risk_event_at": (now - timedelta(minutes=20)).isoformat(),
                    "updated_at": (now - timedelta(minutes=1)).isoformat(),
                },
                reason="seed_recent_cleared_symbol_guard_event",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=50, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 100, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "risk_guard_reentry_cooldown"


def test_leaps_execution_prefers_sell_when_same_cycle_targets_conflict():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=10, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(
            PortfolioTarget(symbol, 5, tag="risk:symbol_guard_reduce_half"),
            PortfolioTarget(symbol, 15, tag="entry"),
        ),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert [(order.side, order.quantity) for order in orders] == [(OrderSide.SELL, 5)]
    assert state is not None
    assert state.value["suppression_reason"] == "same_cycle_opposite_target_conflict"


def test_leaps_execution_blocks_buyback_after_recent_risk_reduction_target_state():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "risk_reentry_cooldown_minutes": 60,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 5,
                    "last_order_side": "sell",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                    "last_reduction_at": (now - timedelta(minutes=20)).isoformat(),
                    "last_reduction_reason": "currency_policy_reduce",
                },
                reason="seed_recent_risk_reduction_target_state",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=5, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 10, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "risk_guard_reentry_cooldown"


def test_leaps_execution_blocks_opposite_order_under_same_source_target():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 11, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "same_source_opposite_rebalance_guard": True,
            "opposite_rebalance_cooldown_minutes": 0,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-20260508",
                    "target_quantity": 10,
                    "last_order_side": "sell",
                    "last_ordered_at": (now - timedelta(hours=2)).isoformat(),
                    "last_order_source_target_batch_id": "portfolio-target-20260508",
                },
                reason="seed_same_source_recent_sell",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=5_000_000, holdings={symbol.key: Holding(symbol, quantity=10, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 15, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-reused",
        source_target_batch_id="portfolio-target-20260508",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "same_source_opposite_rebalance"


def test_leaps_execution_v43_suppresses_small_notional_rebalance_drift():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "notional_rebalance_band_enabled": True,
            "rebalance_no_trade_min_notional": 500_000,
            "rebalance_no_trade_pct_of_target": 0.08,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    portfolio = Portfolio(cash=1_000_000, holdings={symbol.key: Holding(symbol, quantity=30, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 34, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "rebalance_notional_no_trade_band"
    assert state.value["suppressed_notional"] == 400_000
    assert state.value["suppressed_threshold_notional"] == 500_000


def test_leaps_execution_v43_allows_large_notional_rebalance_drift():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "notional_rebalance_band_enabled": True,
            "rebalance_no_trade_min_notional": 500_000,
            "rebalance_no_trade_pct_of_target": 0.08,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    portfolio = Portfolio(cash=1_000_000, holdings={symbol.key: Holding(symbol, quantity=30, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 37, tag="entry"),),
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)

    assert [order.quantity for order in orders] == [7]
    assert orders[0].side is OrderSide.BUY


def test_leaps_execution_v43_does_not_block_new_entry_or_risk_exit_with_notional_band():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "notional_rebalance_band_enabled": True,
            "rebalance_no_trade_min_notional": 500_000,
            "rebalance_no_trade_pct_of_target": 0.08,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    entry_portfolio = Portfolio(cash=1_000_000)
    entry_context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=entry_portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 4, tag="entry"),),
        target_batch_id="order-sizing-entry",
        source_target_batch_id="portfolio-target-entry",
    )
    exit_portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=4, average_price=100_000)})
    exit_context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=exit_portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 0, tag="risk:symbol_guard_exit"),),
        target_batch_id="order-sizing-exit",
        source_target_batch_id="portfolio-target-exit",
    )

    entry_orders = model.create_orders(
        "LEaps",
        entry_portfolio,
        data,
        list(entry_context.approved_targets),
        execution_context=entry_context,
    )
    exit_orders = model.create_orders(
        "LEaps",
        exit_portfolio,
        data,
        list(exit_context.approved_targets),
        execution_context=exit_context,
    )

    assert [order.quantity for order in entry_orders] == [4]
    assert entry_orders[0].side is OrderSide.BUY
    assert [order.quantity for order in exit_orders] == [4]
    assert exit_orders[0].side is OrderSide.SELL


def test_leaps_execution_v43_suppresses_small_sell_after_recent_buy():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8, 10, 0)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100_000, 100_000, 100_000, 100_000, 1000)})
    model = module.create_execution_model(
        {
            "anti_oscillation_enabled": True,
            "opposite_rebalance_cooldown_minutes": 60,
            "opposite_rebalance_no_trade_max_notional": 500_000,
            "opposite_rebalance_no_trade_pct_of_position": 0.08,
            "max_slice_notional": 10_000_000,
            "max_daily_volume_participation_bps": 10_000,
        }
    )
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="leaps-v4.3-notional-band-execution",
                    namespace="target_fulfillment",
                    symbol_key=symbol.key.upper(),
                ),
                value={
                    "source_target_batch_id": "portfolio-target-old",
                    "target_quantity": 30,
                    "last_order_side": "buy",
                    "last_ordered_at": (now - timedelta(minutes=20)).isoformat(),
                },
                reason="seed_recent_buy",
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=0, holdings={symbol.key: Holding(symbol, quantity=30, average_price=90_000)})
    context = ExecutionContext(
        sleeve_id="LEaps",
        generated_at=now,
        portfolio=portfolio,
        data=data,
        approved_targets=(PortfolioTarget(symbol, 27, tag="entry"),),
        model_state=state_view,
        target_batch_id="order-sizing-fresh",
        source_target_batch_id="portfolio-target-fresh",
    )

    orders = model.create_orders("LEaps", portfolio, data, list(context.approved_targets), execution_context=context)
    store.apply_patches(model.state_patches(context=context, orders=tuple(orders)), applied_at=now)
    state = state_view.get(
        model_id="leaps-v4.3-notional-band-execution",
        namespace="target_fulfillment",
        symbol_key=symbol.key.upper(),
    )

    assert orders == []
    assert state is not None
    assert state.value["suppression_reason"] == "opposite_rebalance_cooldown"


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


def test_leaps_execution_uses_dynamic_slice_notional_from_equity_and_liquidity():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 8)
    symbol = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={
            symbol.key: Bar(
                symbol,
                now,
                100_000,
                100_000,
                100_000,
                100_000,
                1_000_000,
                metadata={"rolling_dollar_volume_20": 2_000_000_000},
            )
        },
    )
    model = module.create_execution_model(
        {
            "dynamic_slice_notional_enabled": True,
            "dynamic_slice_equity_pct": 0.20,
            "dynamic_slice_min_notional": 500_000,
            "dynamic_slice_max_notional": 5_000_000,
            "dynamic_slice_liquidity_bps": 8.0,
            "max_slice_notional": 2_000_000,
            "max_slices": 3,
        }
    )

    orders = model.create_orders(
        "LEaps",
        Portfolio(cash=30_000_000, cash_by_currency={"KRW": 30_000_000}),
        data,
        [PortfolioTarget(symbol, 40, tag="entry")],
    )

    assert [order.quantity for order in orders] == [16, 16, 8]
    assert orders[0].metadata["slice_notional_policy"] == "dynamic"
    assert orders[0].metadata["slice_notional_source"] == "equity_pct"
    assert orders[0].metadata["slice_liquidity_source"] == "rolling_dollar_volume_20"
    assert orders[0].metadata["slice_liquidity_cap"] == 1_600_000


def test_leaps_execution_v44_skips_tiny_snapshot_volume_cap_in_regular_auction():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 22, 8, 52)
    symbol = Symbol("440110", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 118_050, 118_050, 118_050, 118_050, 4)})
    model = module.create_execution_model(
        {
            "auction_volume_participation_enabled": False,
            "regular_auction_buy_multiplier": 0.65,
            "max_daily_volume_participation_bps": 50,
            "max_slice_notional": 10_000_000,
            "max_slices": 3,
        }
    )

    orders = model.create_orders(
        "LEaps",
        Portfolio(cash=5_000_000),
        data,
        [PortfolioTarget(symbol, 10, tag="entry")],
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="regular_open_auction",
            is_orderable=True,
            is_regular_market_open=True,
            source="test",
        ),
    )

    assert [order.quantity for order in orders] == [6]
    assert orders[0].metadata["session_policy"] == "regular_auction"
    assert orders[0].metadata["session_quantity_clamp"] == "reduced_size"
    assert orders[0].metadata["participation_cap"] == "skipped_auction_phase"


def test_leaps_execution_v441_uses_min_notional_floor_for_tiny_live_volume():
    module = _load("sleeves/LEaps/executions/leaps_immediate.py")
    now = datetime(2026, 5, 22, 9, 2)
    symbol = Symbol("036930", "KRX")
    data = DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 185_250, 185_250, 185_250, 185_250, 324)})
    model = module.create_execution_model(
        {
            "max_daily_volume_participation_bps": 50,
            "volume_participation_min_notional": 2_000_000,
            "max_slice_notional": 10_000_000,
            "max_slices": 3,
        }
    )

    orders = model.create_orders(
        "LEaps",
        Portfolio(cash=5_000_000),
        data,
        [PortfolioTarget(symbol, 9, tag="entry")],
        market_session=MarketSession(
            market_scope="domestic",
            session_phase="regular_continuous",
            is_orderable=True,
            is_regular_market_open=True,
            source="test",
        ),
    )

    assert [order.quantity for order in orders] == [9]
    assert orders[0].metadata["participation_volume_source"] == "bar_volume"
    assert orders[0].metadata["participation_cap_quantity_before_floor"] == 1
    assert orders[0].metadata["participation_cap_floor"] == "min_notional"


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


def _order_ticket(
    symbol: Symbol,
    *,
    side: OrderSide,
    quantity: int,
    sleeve_id: str,
    created_at: datetime,
    status: OrderTicketStatus = OrderTicketStatus.SUBMITTED,
    filled_quantity: int = 0,
) -> OrderTicket:
    return OrderTicket(
        ticket_id=f"ticket-{side.value}-{symbol.ticker}",
        order_intent_id=f"intent-{side.value}-{symbol.ticker}",
        batch_id="batch-1",
        sleeve_id=sleeve_id,
        symbol=symbol,
        side=side,
        quantity=quantity,
        reference_price=100_000,
        status=status,
        filled_quantity=filled_quantity,
        created_at=created_at,
    )


def _snapshot(
    now: datetime,
    values: dict[str, dict[str, float]],
    *,
    symbol_metadata: dict[str, dict[str, Any]] | None = None,
) -> IndicatorSnapshot:
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
        symbol_metadata=symbol_metadata or {},
    )


def _values(
    *,
    close: float,
    fast: float,
    slow: float,
    momentum: float,
    momentum_5: float,
    vol: float,
    momentum_60: float | None = None,
    rolling_high: float | None = None,
    rolling_low: float | None = None,
) -> dict[str, float]:
    return {
        "close": close,
        "identity_close": close,
        "ema_8_close": fast,
        "sma_20_close": slow,
        "roc_20_close": momentum,
        "roc_60_close": momentum if momentum_60 is None else momentum_60,
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


def _allocator_insight(
    symbol: Symbol,
    now: datetime,
    *,
    score: float,
    momentum: float,
    momentum_5: float = 0.04,
    trend: float = 0.14,
    volatility: float = 0.08,
    breadth: float = 0.70,
    alpha_id: str = "leaps-kospi-conviction",
    confidence: float = 0.8,
) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=3),
        source_snapshot_id="test",
        alpha_id=alpha_id,
        alpha_version="0.1.0",
        confidence=confidence,
        score=score,
        metadata={
            "market_breadth": breadth,
            "momentum": momentum,
            "momentum_5": momentum_5,
            "trend_strength": trend,
            "volatility": volatility,
        },
    )


def _bars(now: datetime, symbols: tuple[Symbol, ...], *, close: float) -> DataSlice:
    return DataSlice(
        time=now,
        bars={
            symbol.key: Bar(symbol, now, close, close, close, close, 1_000_000)
            for symbol in symbols
        },
    )


def _partial_trim_insight(symbol: Symbol, now: datetime, *, target_multiplier: float) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.FLAT,
        generated_at=now,
        expires_at=now + timedelta(days=1),
        source_snapshot_id="test",
        alpha_id="leaps-kospi-swing-rebalance",
        alpha_version="0.1.0",
        confidence=0.8,
        score=0.2,
        metadata={
            "portfolio_action": "partial_trim",
            "target_multiplier": target_multiplier,
            "momentum": 0.12,
            "momentum_5": -0.01,
            "trend_strength": 0.10,
            "volatility": 0.08,
        },
    )


def _etf_safety_insight(
    symbol: Symbol,
    now: datetime,
    *,
    target_pct: float,
    role: str,
    stock_gross_cap: float,
) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=3),
        source_snapshot_id="test",
        alpha_id="leaps-krx-etf-safety",
        alpha_version="0.1.0",
        confidence=0.8,
        weight=target_pct,
        score=0.5,
        metadata={
            "safety_regime": "shock",
            "target_role": role,
            "target_bucket_pct": target_pct,
            "stock_gross_cap": stock_gross_cap,
        },
    )


def _intraday_guard_reference_insight(
    symbol: Symbol,
    now: datetime,
    *,
    guard_symbol: str,
    reference_price: float,
) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=3),
        source_snapshot_id="test",
        alpha_id="leaps-krx-etf-safety",
        alpha_version="0.1.0",
        confidence=0.8,
        score=0.5,
        metadata={
            "benchmark_symbol": guard_symbol,
            "benchmark_close": reference_price,
            "safety_regime": "risk_on",
            "target_role": "cash_like",
            "target_bucket_pct": 0.12,
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

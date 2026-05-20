from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys

from leaps_quant_engine.alpha import InsightDirection
from leaps_quant_engine.alpha import SnapshotContext
from leaps_quant_engine.execution_model_loader import PythonExecutionModelLoader
from leaps_quant_engine.framework.portfolio_construction import PortfolioConstructionContext
from leaps_quant_engine.framework.portfolio_model_loader import PythonPortfolioConstructionModelLoader
from leaps_quant_engine.framework.risk_model_loader import PythonRiskManagementModelLoader
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, RuntimeModelStateView, StatePatch
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue
from leaps_quant_engine.universe.loader import parse_universe_definition
from leaps_quant_engine.universe.selection import UniverseSelectionContext


ROOT = Path(__file__).resolve().parents[1]
SLEEVE = ROOT / "sleeves" / "semiconduct-kor"


def test_semiconduct_kor_selection_filters_and_ranks_semiconductor_stocks():
    module = _load("sleeves/semiconduct-kor/selections/semiconductor_momentum.py")
    now = datetime(2026, 5, 8)
    universe = parse_universe_definition(
        {
            "id": "semiconductor-selection-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock", "industry": "semiconductor_memory", "theme": ["semiconductor", "hbm"]},
                {"ticker": "042700", "market": "KRX", "asset_type": "stock", "industry": "semiconductor_equipment", "theme": ["semiconductor", "equipment"]},
                {"ticker": "035420", "market": "KRX", "asset_type": "stock", "sector": "communication_services"},
                {"ticker": "471760", "market": "KRX", "asset_type": "etf", "is_etf": True, "theme": ["semiconductor"]},
            ],
        }
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, fast=79000, slow=76000, momentum=0.08, momentum_5=0.03, vol=0.03),
            "KRX:042700": _values(close=300000, fast=292000, slow=270000, momentum=0.20, momentum_5=0.07, vol=0.04),
            "KRX:035420": _values(close=220000, fast=215000, slow=210000, momentum=0.50, momentum_5=0.10, vol=0.04),
            "KRX:471760": _values(close=12000, fast=11800, slow=11500, momentum=0.30, momentum_5=0.08, vol=0.03),
        },
    )

    result = module.SemiconductorMomentumSelectionModel(max_active_symbols=2).select(
        UniverseSelectionContext(sleeve_id="semiconduct-kor", universe=universe, indicator_snapshot=snapshot)
    )

    assert result.selection_id == "semiconduct-kor-momentum"
    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:042700", "KRX:005930"]
    assert result.rejected["KRX:035420"] == ("not_semiconductor_profile",)
    assert result.rejected["KRX:471760"] == ("not_stock_candidate",)


def test_semiconduct_kor_samsung_core_selection_always_selects_samsung():
    module = _load("sleeves/semiconduct-kor/selections/samsung_core.py")
    universe = parse_universe_definition(
        {
            "id": "samsung-core-test",
            "market": "KRX",
            "symbols": [
                {"ticker": "005930", "market": "KRX", "asset_type": "stock"},
                {"ticker": "042700", "market": "KRX", "asset_type": "stock"},
            ],
        }
    )

    result = module.SamsungCoreSelectionModel().select(
        UniverseSelectionContext(sleeve_id="semiconduct-kor", universe=universe)
    )

    assert result.selection_id == "semiconduct-kor-samsung-core"
    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:005930"]
    assert result.rejected["KRX:042700"] == ("not_samsung_core",)


def test_semiconduct_kor_alpha_emits_ranked_up_insights_only_for_healthy_trends():
    module = _load("sleeves/semiconduct-kor/alphas/semiconductor_momentum.py")
    now = datetime(2026, 5, 8)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, fast=79000, slow=76000, momentum=0.08, momentum_5=0.03, vol=0.03),
            "KRX:042700": _values(close=300000, fast=292000, slow=270000, momentum=0.20, momentum_5=0.07, vol=0.04),
            "KRX:000990": _values(close=45000, fast=46000, slow=48000, momentum=-0.04, momentum_5=-0.02, vol=0.05),
        },
    )

    context = SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(
        ("KRX:005930", "KRX:042700", "KRX:000990")
    )
    insights = module.generate(context)

    assert [insight.symbol.key for insight in insights] == ["KRX:042700", "KRX:005930"]
    assert insights[0].alpha_id == "semiconduct-kor-momentum"
    assert insights[0].sleeve_id == "semiconduct-kor"
    assert insights[0].direction.value == "up"
    assert insights[0].group_id == "krw-semiconductor"


def test_semiconduct_kor_samsung_steward_alpha_holds_or_trims_samsung():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 8)
    healthy = _snapshot(
        now,
        {
            "KRX:005930": _values(close=80000, fast=79000, slow=76000, momentum=0.08, momentum_5=0.03, vol=0.03),
        },
    )
    weak = _snapshot(
        now,
        {
            "KRX:005930": _values(close=74000, fast=75000, slow=76000, momentum=-0.03, momentum_5=-0.01, vol=0.03),
        },
    )

    healthy_insights = module.generate(SnapshotContext.from_indicator_snapshot(healthy).with_input_symbols(("KRX:005930",)))
    weak_insights = module.generate(SnapshotContext.from_indicator_snapshot(weak).with_input_symbols(("KRX:005930",)))

    assert healthy_insights[0].alpha_id == "semiconduct-kor-samsung-steward"
    assert healthy_insights[0].direction is InsightDirection.UP
    assert healthy_insights[0].metadata["target_percent"] == 1.0
    assert healthy_insights[0].metadata["action"] == "core_hold"
    assert weak_insights[0].direction is InsightDirection.FLAT
    assert weak_insights[0].metadata["target_percent"] == 0.65
    assert weak_insights[0].metadata["action"] == "cash_reserve_trim"


def test_semiconduct_kor_samsung_steward_alpha_accumulates_confirmed_dips():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 8)
    dip = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=74500,
                fast=75500,
                slow=76000,
                sma60=73000,
                sma120=70000,
                momentum=0.01,
                momentum_5=0.012,
                momentum_60=0.04,
                vol=0.035,
                rolling_high=81000,
                rolling_low=74000,
                zscore=-1.8,
                drawdown=-0.08,
                bar_return=0.012,
                clv=0.45,
            ),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(dip).with_input_symbols(("KRX:005930",)))

    assert insights[0].direction is InsightDirection.UP
    assert insights[0].metadata["phase"] == "accumulation"
    assert insights[0].metadata["action"] == "accumulate_standard_dip"
    assert insights[0].metadata["target_delta_percent"] == 0.15
    assert insights[0].metadata["max_target_percent"] == 0.9


def test_semiconduct_kor_samsung_steward_alpha_marks_risk_capitulation_accumulation():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 15)
    capitulation = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=70000,
                fast=70500,
                slow=73000,
                sma60=76000,
                sma120=75000,
                momentum=-0.04,
                momentum_5=-0.01,
                momentum_60=-0.02,
                vol=0.04,
                rolling_high=80000,
                rolling_low=69000,
                zscore=-1.5,
                drawdown=-0.125,
                bar_return=-0.01,
                clv=-0.2,
            ),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(capitulation).with_input_symbols(("KRX:005930",)))

    assert insights[0].direction is InsightDirection.FLAT
    assert insights[0].metadata["phase"] == "capitulation"
    assert insights[0].metadata["action"] == "risk_capitulation_accumulate"
    assert insights[0].metadata["target_percent"] == 0.35
    assert insights[0].metadata["target_delta_percent"] == 0.05
    assert insights[0].metadata["max_target_percent"] == 0.45
    assert round(insights[0].metadata["capitulation_trigger_price"], 6) == 65450.0


def test_semiconduct_kor_samsung_steward_alpha_plans_capitulation_for_high_volatility_risk_off():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 15)
    volatile = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=296000,
                fast=272750,
                slow=240775,
                sma60=208123,
                sma120=167883,
                momentum=0.43,
                momentum_5=0.09,
                momentum_60=0.76,
                vol=0.097,
                rolling_high=296000,
                rolling_low=211000,
                zscore=1.92,
                drawdown=0.0,
                bar_return=0.04,
                clv=0.6,
            ),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(volatile).with_input_symbols(("KRX:005930",)))

    assert insights[0].metadata["regime"] == "risk_off"
    assert insights[0].metadata["action"] == "risk_capitulation_accumulate"
    assert round(insights[0].metadata["capitulation_trigger_price"], 2) == 276760.0


def test_semiconduct_kor_samsung_steward_alpha_stages_reentry_after_stress_repair():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 20)
    repair = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=276000,
                fast=274500,
                slow=272000,
                sma60=280000,
                sma120=250000,
                momentum=-0.005,
                momentum_5=0.018,
                momentum_60=0.10,
                vol=0.055,
                rolling_high=299500,
                rolling_low=263500,
                zscore=0.2,
                drawdown=-0.078,
                bar_return=0.018,
                clv=0.55,
            ),
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(repair).with_input_symbols(("KRX:005930",)))

    assert insights[0].direction is InsightDirection.UP
    assert insights[0].metadata["phase"] == "reentry"
    assert insights[0].metadata["action"] == "accumulate_reentry_reclaim"
    assert insights[0].metadata["target_delta_percent"] == 0.20
    assert insights[0].metadata["max_target_percent"] == 0.45
    assert insights[0].metadata["reentry_stage"] == "reclaim"


def test_semiconduct_kor_samsung_steward_alpha_holds_reentry_target_during_cooldown():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    now = datetime(2026, 5, 21)
    store = InMemoryRuntimeStateStore()
    state = RuntimeModelStateView(store=store, default_sleeve_id="semiconduct-kor")
    store.apply_patches(
        (
            StatePatch(
                key=state.key(
                    model_id=module.ALPHA_ID,
                    namespace=module.STATE_NAMESPACE,
                    symbol_key="KRX:005930",
                ),
                value={
                    "last_accumulation_at": "2026-05-20T10:00:00",
                    "last_action": "accumulate_reentry_reclaim",
                    "last_target_percent": 0.45,
                },
            ),
        ),
        applied_at=now,
    )
    healed = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=286000,
                fast=285000,
                slow=282000,
                sma60=281000,
                sma120=250000,
                momentum=0.04,
                momentum_5=0.02,
                momentum_60=0.10,
                vol=0.04,
                rolling_high=299500,
                rolling_low=263500,
                zscore=0.5,
                drawdown=-0.045,
                bar_return=0.01,
                clv=0.6,
            ),
        },
    )

    insights = module.generate(
        SnapshotContext.from_indicator_snapshot(healed, model_state=state).with_input_symbols(("KRX:005930",))
    )

    assert insights[0].metadata["phase"] == "cooldown"
    assert insights[0].metadata["action"] == "accumulation_cooldown_hold"
    assert insights[0].metadata["target_percent"] == 0.45


def test_semiconduct_kor_strike_reentry_alpha_waits_while_strike_risk_is_on():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_strike_risk_reentry.py")
    now = datetime(2026, 5, 20)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=267500,
                fast=271000,
                slow=274000,
                sma60=281000,
                sma120=250000,
                momentum=-0.03,
                momentum_5=-0.02,
                momentum_60=0.08,
                vol=0.07,
                rolling_high=299500,
                rolling_low=263500,
                drawdown=-0.107,
                bar_return=-0.029,
                clv=0.2,
            ),
        },
        metadata={"KRX:005930": {"strike_risk_status": "on", "confidence": 0.9, "source_count": 3}},
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert insights == []


def test_semiconduct_kor_strike_reentry_alpha_blocks_falling_knife_after_risk_off_candidate():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_strike_risk_reentry.py")
    now = datetime(2026, 5, 21)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=254000,
                fast=260000,
                slow=274000,
                sma60=281000,
                sma120=250000,
                momentum=-0.07,
                momentum_5=-0.035,
                momentum_60=0.02,
                vol=0.095,
                rolling_high=299500,
                rolling_low=253000,
                drawdown=-0.152,
                bar_return=-0.045,
                clv=0.15,
            ),
        },
        metadata={"KRX:005930": {"strike_risk_status": "off_candidate", "confidence": 0.7, "source_count": 2}},
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert insights == []


def test_semiconduct_kor_strike_reentry_alpha_emits_buy_only_dynamic_probe():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_strike_risk_reentry.py")
    now = datetime(2026, 5, 21)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=271500,
                fast=270500,
                slow=275000,
                sma60=281000,
                sma120=250000,
                momentum=-0.025,
                momentum_5=0.012,
                momentum_60=0.06,
                vol=0.065,
                rolling_high=299500,
                rolling_low=263500,
                drawdown=-0.093,
                bar_return=0.015,
                clv=0.48,
            ),
        },
        metadata={
            "KRX:005930": {
                "strike_risk_status": "off_candidate",
                "confidence": 0.72,
                "source_count": 2,
                "reason": "strike delayed and talks reopened",
            }
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert len(insights) == 1
    assert insights[0].alpha_id == "semiconduct-kor-samsung-strike-reentry"
    assert insights[0].direction is InsightDirection.UP
    assert insights[0].metadata["action"] == "accumulate_strike_probe"
    assert insights[0].metadata["target_percent"] == 0.25
    assert insights[0].metadata["dynamic_gate"] == "recent_low_rebound_without_fixed_price_anchor"
    assert "trigger_price" not in insights[0].metadata


def test_semiconduct_kor_strike_reentry_alpha_emits_stronger_buy_after_confirmed_reclaim():
    module = _load("sleeves/semiconduct-kor/alphas/samsung_strike_risk_reentry.py")
    now = datetime(2026, 5, 22)
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=282500,
                fast=281500,
                slow=279000,
                sma60=284000,
                sma120=250000,
                momentum=0.018,
                momentum_5=0.02,
                momentum_60=0.08,
                vol=0.05,
                rolling_high=299500,
                rolling_low=263500,
                drawdown=-0.057,
                bar_return=0.026,
                clv=0.62,
            ),
        },
        metadata={
            "KRX:005930": {
                "strike_risk_status": "off_confirmed",
                "confidence": 0.86,
                "source_count": 3,
                "reason": "strike cancelled and production normalized",
            }
        },
    )

    insights = module.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))

    assert insights[0].direction is InsightDirection.UP
    assert insights[0].metadata["action"] == "accumulate_strike_reclaim"
    assert insights[0].metadata["target_percent"] == 0.45
    assert insights[0].metadata["target_delta_percent"] == 0.20


def test_semiconduct_kor_buy_only_portfolio_never_targets_below_current_holding():
    alpha = _load("sleeves/semiconduct-kor/alphas/samsung_strike_risk_reentry.py")
    portfolio_module = _load("sleeves/semiconduct-kor/portfolios/samsung_buy_only.py")
    now = datetime(2026, 5, 21)
    samsung = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={samsung.key: Bar(symbol=samsung, time=now, open=271500, high=273000, low=266000, close=271500, volume=1_000_000)},
    )
    portfolio = Portfolio(
        cash=2_715_000,
        cash_by_currency={"KRW": 2_715_000},
        holdings={samsung.key: Holding(samsung, quantity=70, average_price=260000)},
    )
    snapshot = _snapshot(
        now,
        {
            "KRX:005930": _values(
                close=271500,
                fast=270500,
                slow=275000,
                sma60=281000,
                sma120=250000,
                momentum=-0.025,
                momentum_5=0.012,
                momentum_60=0.06,
                vol=0.065,
                rolling_high=299500,
                rolling_low=263500,
                drawdown=-0.093,
                bar_return=0.015,
                clv=0.48,
            ),
        },
        metadata={"KRX:005930": {"strike_risk_status": "off_candidate", "confidence": 0.72, "source_count": 2}},
    )
    insight = alpha.generate(SnapshotContext.from_indicator_snapshot(snapshot).with_input_symbols(("KRX:005930",)))[0]
    model = portfolio_module.create_portfolio_model({"max_target_percent": 1.0, "min_cash_to_add_pct": 0.01})

    targets = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=data,
            portfolio=portfolio,
            active_insights=(insight,),
            managed_symbols=(samsung,),
        )
    )

    current_percent = (70 * 271500) / ((70 * 271500) + 2_715_000)
    assert round(targets[0].target_percent, 6) == round(current_percent, 6)
    assert targets[0].target_percent > insight.metadata["target_percent"]
    assert targets[0].tag == "samsung_buy_only:semiconduct-kor-samsung-strike-reentry:accumulate_strike_probe:up"


def test_semiconduct_kor_samsung_steward_portfolio_manages_existing_position_with_zero_cash():
    alpha = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    portfolio_module = _load("sleeves/semiconduct-kor/portfolios/samsung_steward.py")
    now = datetime(2026, 5, 8)
    samsung = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(
                symbol=samsung,
                time=now,
                open=80000,
                high=80500,
                low=79000,
                close=80000,
                volume=1_000_000,
            )
        },
    )
    portfolio = Portfolio(
        cash=0,
        cash_by_currency={"KRW": 0},
        holdings={samsung.key: Holding(samsung, quantity=100, average_price=70000)},
    )
    model = portfolio_module.create_portfolio_model({"max_target_percent": 1.0})
    healthy_insight = alpha.generate(
        SnapshotContext.from_indicator_snapshot(
            _snapshot(
                now,
                {"KRX:005930": _values(close=80000, fast=79000, slow=76000, momentum=0.08, momentum_5=0.03, vol=0.03)},
            )
        ).with_input_symbols(("KRX:005930",))
    )[0]
    trim_insight = alpha.generate(
        SnapshotContext.from_indicator_snapshot(
            _snapshot(
                now,
                {"KRX:005930": _values(close=74000, fast=75000, slow=76000, momentum=-0.03, momentum_5=-0.01, vol=0.03)},
            )
        ).with_input_symbols(("KRX:005930",))
    )[0]

    hold_targets = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=data,
            portfolio=portfolio,
            active_insights=(healthy_insight,),
            managed_symbols=(samsung,),
        )
    )
    trim_targets = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=data,
            portfolio=portfolio,
            active_insights=(trim_insight,),
            managed_symbols=(samsung,),
        )
    )

    assert hold_targets[0].symbol.key == "KRX:005930"
    assert hold_targets[0].target_percent == 1.0
    assert trim_targets[0].target_percent == 0.65
    assert trim_targets[0].tag == "samsung_steward:semiconduct-kor-samsung-steward:cash_reserve_trim:flat"


def test_semiconduct_kor_samsung_steward_portfolio_uses_cash_for_staged_dip_adds():
    alpha = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    portfolio_module = _load("sleeves/semiconduct-kor/portfolios/samsung_steward.py")
    now = datetime(2026, 5, 8)
    samsung = Symbol("005930", "KRX")
    data = DataSlice(
        time=now,
        bars={
            samsung.key: Bar(
                symbol=samsung,
                time=now,
                open=74500,
                high=75000,
                low=73500,
                close=74500,
                volume=1_000_000,
            )
        },
    )
    portfolio = Portfolio(
        cash=2_980_000,
        cash_by_currency={"KRW": 2_980_000},
        holdings={samsung.key: Holding(samsung, quantity=100, average_price=70000)},
    )
    insight = alpha.generate(
        SnapshotContext.from_indicator_snapshot(
            _snapshot(
                now,
                {
                    "KRX:005930": _values(
                        close=74500,
                        fast=75500,
                        slow=76000,
                        sma60=73000,
                        sma120=70000,
                        momentum=0.01,
                        momentum_5=0.012,
                        momentum_60=0.04,
                        vol=0.035,
                        rolling_high=81000,
                        rolling_low=74000,
                        zscore=-1.8,
                        drawdown=-0.08,
                        bar_return=0.012,
                        clv=0.45,
                    )
                },
            )
        ).with_input_symbols(("KRX:005930",))
    )[0]
    model = portfolio_module.create_portfolio_model({"max_target_percent": 1.0, "min_cash_to_add_pct": 0.01})

    targets = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=data,
            portfolio=portfolio,
            active_insights=(insight,),
            managed_symbols=(samsung,),
        )
    )

    expected_current_percent = (100 * 74500) / ((100 * 74500) + 2_980_000)
    assert round(targets[0].target_percent, 6) == round(expected_current_percent + 0.15, 6)
    assert targets[0].tag == "samsung_steward:semiconduct-kor-samsung-steward:accumulate_standard_dip:up"


def test_semiconduct_kor_samsung_steward_portfolio_defends_then_adds_on_capitulation_trigger():
    alpha = _load("sleeves/semiconduct-kor/alphas/samsung_steward.py")
    portfolio_module = _load("sleeves/semiconduct-kor/portfolios/samsung_steward.py")
    now = datetime(2026, 5, 15)
    samsung = Symbol("005930", "KRX")
    insight = alpha.generate(
        SnapshotContext.from_indicator_snapshot(
            _snapshot(
                now,
                {
                    "KRX:005930": _values(
                        close=70000,
                        fast=70500,
                        slow=73000,
                        sma60=76000,
                        sma120=75000,
                        momentum=-0.04,
                        momentum_5=-0.01,
                        momentum_60=-0.02,
                        vol=0.04,
                        rolling_high=80000,
                        rolling_low=69000,
                        zscore=-1.5,
                        drawdown=-0.125,
                        bar_return=-0.01,
                        clv=-0.2,
                    )
                },
            )
        ).with_input_symbols(("KRX:005930",))
    )[0]
    model = portfolio_module.create_portfolio_model({"max_target_percent": 1.0, "min_cash_to_add_pct": 0.01})

    defense_data = DataSlice(
        time=now,
        bars={samsung.key: Bar(symbol=samsung, time=now, open=70000, high=70500, low=69000, close=70000, volume=1_000_000)},
    )
    defense_target = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=defense_data,
            portfolio=Portfolio(
                cash=0,
                cash_by_currency={"KRW": 0},
                holdings={samsung.key: Holding(samsung, quantity=100, average_price=70000)},
            ),
            active_insights=(insight,),
            managed_symbols=(samsung,),
        )
    )[0]

    add_data = DataSlice(
        time=now,
        bars={samsung.key: Bar(symbol=samsung, time=now, open=65500, high=65600, low=64800, close=65000, volume=1_000_000)},
    )
    add_target = model.create_targets(
        PortfolioConstructionContext(
            sleeve_id="semiconduct-kor",
            data=add_data,
            portfolio=Portfolio(
                cash=4_225_000,
                cash_by_currency={"KRW": 4_225_000},
                holdings={samsung.key: Holding(samsung, quantity=35, average_price=70000)},
            ),
            active_insights=(insight,),
            managed_symbols=(samsung,),
        )
    )[0]

    assert defense_target.target_percent == 0.35
    assert defense_target.tag == "samsung_steward:semiconduct-kor-samsung-steward:risk_capitulation_accumulate:flat"
    assert round(add_target.target_percent, 6) == 0.4


def test_semiconduct_kor_runtime_config_and_workspace_models_load():
    snapshot = load_runtime_config_snapshot(ROOT / "configs" / "runtime" / "semiconduct_kor_sleeve.json")
    sleeve = snapshot.config.sleeve("semiconduct-kor")

    portfolio = PythonPortfolioConstructionModelLoader().load(
        SLEEVE / "portfolios" / "samsung_buy_only.py",
        parameters={"max_target_percent": 1.0},
    )
    risk = PythonRiskManagementModelLoader().load(
        SLEEVE / "risks" / "basic.py",
        parameters={"max_position_pct": 0.18},
    )
    execution = PythonExecutionModelLoader().load(
        SLEEVE / "executions" / "immediate.py",
        parameters={"tag_prefix": "semiconduct-kor"},
    )

    assert snapshot.config.mode == "paper"
    assert sleeve.workspace_path == Path("sleeves/semiconduct-kor")
    assert sleeve.universe.coarse_path == Path("configs/universes/semiconduct_kor_core.json")
    assert [module.ref for module in sleeve.alpha.modules] == [
        "alphas/samsung_strike_risk_reentry.py",
    ]
    assert dict(sleeve.alpha.input_selections) == {
        "semiconduct-kor-samsung-strike-reentry": "semiconduct-kor-samsung-core",
    }
    assert dict(sleeve.cash_by_currency) == {}
    assert sleeve.portfolio.model.ref == "portfolios/samsung_buy_only.py"
    assert portfolio.model_name == "SamsungBuyOnlyPortfolioConstructionModel"
    assert risk.model_name == "BasicRiskManagementModel"
    assert execution.model_name == "SemiconductKorExecutionModel"


def _snapshot(
    now: datetime,
    values: dict[str, dict[str, float]],
    *,
    metadata: dict[str, dict[str, object]] | None = None,
) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id="indicator-test",
        sleeve_id="semiconduct-kor",
        universe_id="semiconductor-test",
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
        symbol_metadata=metadata or {},
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
    sma60: float | None = None,
    sma120: float | None = None,
    momentum_60: float | None = None,
    zscore: float = 0.0,
    drawdown: float | None = None,
    bar_return: float = 0.0,
    clv: float = 0.0,
) -> dict[str, float]:
    return {
        "close": close,
        "identity_close": close,
        "ema_8_close": fast,
        "sma_10_close": fast,
        "sma_20_close": slow,
        "sma_50_close": sma60 if sma60 is not None else slow,
        "sma_60_close": sma60 if sma60 is not None else slow,
        "sma_120_close": sma120 if sma120 is not None else sma60 if sma60 is not None else slow,
        "roc_20_close": momentum,
        "roc_60_close": momentum_60 if momentum_60 is not None else momentum,
        "momentum_5_close": momentum_5,
        "rolling_max_20_close": rolling_high if rolling_high is not None else close * 1.05,
        "rolling_min_20_close": rolling_low if rolling_low is not None else close * 0.95,
        "zscore_20_close": zscore,
        "drawdown_20_close": drawdown if drawdown is not None else -0.05,
        "return_1_close": bar_return,
        "close_location_value": clv,
        "stddev_20_close": close * vol,
        "atr_14": close * vol,
        "rolling_dollar_volume_20": 5_000_000_000,
        "volume": 1000,
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

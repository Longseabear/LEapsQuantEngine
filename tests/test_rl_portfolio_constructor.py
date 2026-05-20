from datetime import datetime, timedelta
import json

import numpy as np
import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import PortfolioConstructionContext
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.rl import ReinforcementLearningPortfolioConstructionModel
from leaps_quant_engine.rl.portfolio_constructor import (
    _asset_token_observation,
    _integer_lot_asset_weights,
    _make_training_env,
    _observation_from_insights,
    _ranked_asset_indices,
)
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, RuntimeModelStateView, StatePatch
from leaps_quant_engine.universe.definition import UniverseDefinition


def test_rl_portfolio_constructor_falls_back_to_deterministic_exposure():
    symbol_a = Symbol("AAPL", "US")
    symbol_b = Symbol("MSFT", "US")
    now = datetime(2026, 1, 2)
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        exposure_levels=(0.0, 0.25, 0.5, 0.75),
        fallback_action=2,
        max_position_pct=0.35,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                symbol_a.key: Bar(symbol_a, now, 100, 100, 100, 100, 1000),
                symbol_b.key: Bar(symbol_b, now, 100, 100, 100, 100, 1000),
            },
        ),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=(
            _up_insight(symbol_a, now, momentum=0.1),
            _up_insight(symbol_b, now, momentum=0.2),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert {target.symbol.key for target in targets} == {"US:AAPL", "US:MSFT"}
    assert [target.target_percent for target in targets] == [0.25, 0.25]
    assert all(target.tag.startswith("rl:ppo") for target in targets)


def test_rl_portfolio_constructor_risk_softmax_respects_top_k():
    symbol_a = Symbol("AAPL", "US")
    symbol_b = Symbol("MSFT", "US")
    symbol_c = Symbol("TSLA", "US")
    now = datetime(2026, 1, 2)
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="risk_softmax",
        exposure_levels=(0.0, 1.0),
        fallback_action=1,
        max_position_pct=1.0,
        top_k=2,
    )
    context = PortfolioConstructionContext(
        sleeve_id="us_etf_rotation",
        data=DataSlice(
            time=now,
            bars={
                symbol_a.key: Bar(symbol_a, now, 100, 100, 100, 100, 1000),
                symbol_b.key: Bar(symbol_b, now, 100, 100, 100, 100, 1000),
                symbol_c.key: Bar(symbol_c, now, 100, 100, 100, 100, 1000),
            },
        ),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=(
            _up_insight(symbol_a, now, momentum=0.2),
            _up_insight(symbol_b, now, momentum=0.3),
            _up_insight(symbol_c, now, momentum=0.1),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert {target.symbol.key for target in targets} == {"US:AAPL", "US:MSFT"}


def test_rl_portfolio_constructor_keeps_managed_holding_without_active_insight():
    symbol = Symbol("AAPL", "US")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=0, cash_by_currency={"USD": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 3, "average_price": 100})()
    model = ReinforcementLearningPortfolioConstructionModel(fallback_action=0)
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(),
        managed_symbols=(symbol,),
    )

    targets = model.create_targets(context)

    assert targets == ()


def test_rl_portfolio_constructor_can_zero_held_symbol_missing_from_complete_target_set():
    selected = Symbol("005930", "KRX")
    held_missing = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held_missing.key] = type(
        "HoldingLike",
        (),
        {"symbol": held_missing, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
        max_position_pct=0.9,
        emit_zero_for_missing_held_targets=True,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                selected.key: Bar(selected, now, 280_000, 280_000, 280_000, 280_000, 1000),
                held_missing.key: Bar(held_missing, now, 119_300, 119_300, 119_300, 119_300, 1000),
            },
        ),
        portfolio=portfolio,
        active_insights=(_up_insight(selected, now, momentum=0.2),),
        managed_symbols=(held_missing,),
    )

    targets = model.create_targets(context)

    by_symbol = {target.symbol.key: target for target in targets}
    assert by_symbol[selected.key].target_percent == pytest.approx(0.8)
    assert by_symbol[held_missing.key].target_percent == 0.0
    assert by_symbol[held_missing.key].tag == "rl:ppo:no_longer_in_target_portfolio"


def test_rl_portfolio_constructor_holds_missing_symbol_until_exit_confirmation():
    selected = Symbol("005930", "KRX")
    held_missing = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held_missing.key] = type(
        "HoldingLike",
        (),
        {"symbol": held_missing, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
        max_position_pct=0.9,
        emit_zero_for_missing_held_targets=True,
        missing_target_exit_confirmation_cycles=3,
    )
    state_view = RuntimeModelStateView(store=InMemoryRuntimeStateStore(), default_sleeve_id="LEaps")
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                selected.key: Bar(selected, now, 280_000, 280_000, 280_000, 280_000, 1000),
                held_missing.key: Bar(held_missing, now, 119_300, 119_300, 119_300, 119_300, 1000),
            },
        ),
        portfolio=portfolio,
        active_insights=(_up_insight(selected, now, momentum=0.2),),
        managed_symbols=(held_missing,),
        model_state=state_view,
    )

    targets = model.create_targets(context)
    patches = model.state_patches(context=context, targets=targets)

    by_symbol = {target.symbol.key: target for target in targets}
    assert by_symbol[held_missing.key].target_percent > 0.0
    assert by_symbol[held_missing.key].tag == "rl:ppo:missing_target_hold:1/3"
    membership = [patch for patch in patches if patch.key.namespace == "target_membership"]
    assert {patch.key.symbol_key: patch.value["missing_count"] for patch in membership} == {
        selected.key: 0,
        held_missing.key: 1,
    }


def test_rl_portfolio_constructor_exits_after_missing_confirmation_count():
    selected = Symbol("005930", "KRX")
    held_missing = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="rl-portfolio-constructor",
                    namespace="target_membership",
                    symbol_key=held_missing.key,
                ),
                value={"missing_count": 2},
                generated_at=now,
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held_missing.key] = type(
        "HoldingLike",
        (),
        {"symbol": held_missing, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
        max_position_pct=0.9,
        emit_zero_for_missing_held_targets=True,
        missing_target_exit_confirmation_cycles=3,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                selected.key: Bar(selected, now, 280_000, 280_000, 280_000, 280_000, 1000),
                held_missing.key: Bar(held_missing, now, 119_300, 119_300, 119_300, 119_300, 1000),
            },
        ),
        portfolio=portfolio,
        active_insights=(_up_insight(selected, now, momentum=0.2),),
        managed_symbols=(held_missing,),
        model_state=state_view,
    )

    targets = model.create_targets(context)

    by_symbol = {target.symbol.key: target for target in targets}
    assert by_symbol[held_missing.key].target_percent == 0.0
    assert by_symbol[held_missing.key].tag == "rl:ppo:no_longer_in_target_portfolio"


def test_rl_portfolio_constructor_ignores_implausible_active_momentum_insight():
    valid = Symbol("009150", "KRX")
    invalid = Symbol("011930", "KRX")
    now = datetime(2026, 5, 15, 12, 15)
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        exposure_levels=(0.0, 0.5),
        fallback_action=1,
        max_position_pct=1.0,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                valid.key: Bar(valid, now, 100, 100, 100, 100, 1000),
                invalid.key: Bar(invalid, now, 100, 100, 100, 100, 1000),
            },
        ),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(
            _up_insight(invalid, now, momentum=9.0),
            _up_insight(valid, now, momentum=0.2),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert {target.symbol.key for target in targets} == {valid.key}
    assert targets[0].target_percent == pytest.approx(0.5)


def test_rl_portfolio_constructor_complete_target_mode_does_not_flatten_without_actionable_insights():
    held = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held.key] = type(
        "HoldingLike",
        (),
        {"symbol": held, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        emit_zero_for_missing_held_targets=True,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={held.key: Bar(held, now, 119_300, 119_300, 119_300, 119_300, 1000)}),
        portfolio=portfolio,
        active_insights=(),
        managed_symbols=(held,),
    )

    targets = model.create_targets(context)

    assert targets == ()


def test_rl_portfolio_constructor_emits_exit_for_explicit_flat_insight():
    symbol = Symbol("AAPL", "US")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=0, cash_by_currency={"USD": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 3, "average_price": 100})()
    model = ReinforcementLearningPortfolioConstructionModel(fallback_action=0)
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(_flat_insight(symbol, now),),
        managed_symbols=(symbol,),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == symbol
    assert targets[0].target_percent == 0.0
    assert targets[0].tag == "rl:flat-alpha:flat"


def test_rl_portfolio_constructor_flat_insight_overrides_same_cycle_up_for_held_symbol():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=0, cash_by_currency={"KRW": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 3, "average_price": 100})()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(
            _up_insight(symbol, now, momentum=0.2),
            Insight(
                sleeve_id="LEaps",
                symbol=symbol,
                direction=InsightDirection.FLAT,
                generated_at=now,
                expires_at=now + timedelta(days=1),
                source_snapshot_id="test",
                alpha_id="stop-alpha",
                alpha_version="1",
                confidence=0.9,
                score=0.4,
            ),
        ),
        managed_symbols=(symbol,),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == symbol
    assert targets[0].target_percent == 0.0
    assert targets[0].tag == "rl:stop-alpha:flat"


def test_rl_portfolio_constructor_flat_priority_is_order_independent_for_same_timestamp():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=0, cash_by_currency={"KRW": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 3, "average_price": 100})()
    flat = Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.FLAT,
        generated_at=now,
        expires_at=now + timedelta(days=1),
        source_snapshot_id="test",
        alpha_id="stop-alpha",
        alpha_version="1",
        confidence=0.9,
        score=0.4,
    )
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(
            flat,
            _up_insight(symbol, now, momentum=0.2),
        ),
        managed_symbols=(symbol,),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == symbol
    assert targets[0].target_percent == 0.0
    assert targets[0].tag == "rl:stop-alpha:flat"


def test_rl_portfolio_constructor_partial_trim_reduces_without_full_exit():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=0, cash_by_currency={"KRW": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 10, "average_price": 100})()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
        max_position_pct=0.9,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(
            _up_insight(symbol, now, momentum=0.2),
            _partial_trim_insight(symbol, now, target_multiplier=0.5),
        ),
        managed_symbols=(symbol,),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].symbol == symbol
    assert targets[0].target_percent == pytest.approx(0.5)
    assert targets[0].tag == "rl:swing-alpha:partial_trim=0.50"


def test_rl_portfolio_constructor_uses_median_ensemble_action(monkeypatch, tmp_path):
    symbol = Symbol("AAPL", "US")
    now = datetime(2026, 1, 2)
    policy_paths = []
    for index in range(3):
        path = tmp_path / f"policy-{index}.zip"
        path.write_text("stub", encoding="utf-8")
        policy_paths.append(path)

    class FakePolicy:
        def __init__(self, action):
            self.action = action

        def predict(self, observation, deterministic=True):
            return self.action, None

    actions = iter((0, 3, 1))

    monkeypatch.setattr(
        "leaps_quant_engine.rl.portfolio_constructor._load_ppo_model",
        lambda path, device="cpu": FakePolicy(next(actions)),
    )
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=tuple(policy_paths),
        exposure_levels=(0.0, 0.25, 0.5, 0.75),
        fallback_action=0,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=(_up_insight(symbol, now, momentum=0.1),),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == 0.25


def test_rl_portfolio_constructor_passes_policy_device_to_loader(monkeypatch, tmp_path):
    symbol = Symbol("AAPL", "US")
    now = datetime(2026, 1, 2)
    path = tmp_path / "policy.zip"
    path.write_text("stub", encoding="utf-8")
    captured = {}

    class FakePolicy:
        def predict(self, observation, deterministic=True):
            return 1, None

    def fake_loader(path, *, device="cpu"):
        captured["device"] = device
        return FakePolicy()

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", fake_loader)
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        exposure_levels=(0.0, 0.25),
        policy_device="cuda",
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=(_up_insight(symbol, now, momentum=0.1),),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert captured["device"] == "cuda"
    assert len(targets) == 1


def test_rl_portfolio_constructor_sends_top_k_token_observation(monkeypatch, tmp_path):
    symbols = [Symbol(f"S{index}", "US") for index in range(3)]
    now = datetime(2026, 1, 2)
    path = tmp_path / "policy.zip"
    path.write_text("stub", encoding="utf-8")
    captured = {}

    class FakePolicy:
        def predict(self, observation, deterministic=True):
            captured["shape"] = observation.shape
            captured["first_token"] = observation[0].copy()
            return 1, None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": FakePolicy())
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        exposure_levels=(0.0, 0.25),
        top_k=2,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000) for symbol in symbols},
        ),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=tuple(
            _up_insight(symbol, now, momentum=0.1 + index)
            for index, symbol in enumerate(symbols)
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 2
    assert {target.symbol.key for target in targets} == {"US:S1", "US:S2"}
    assert captured["shape"] == (2, 8)
    assert captured["first_token"][0] == 1.0


def test_rl_portfolio_constructor_applies_signal_action_floor(monkeypatch, tmp_path):
    symbol = Symbol("AAPL", "US")
    now = datetime(2026, 1, 2)
    path = tmp_path / "policy.zip"
    path.write_text("stub", encoding="utf-8")

    class CashPolicy:
        def predict(self, observation, deterministic=True):
            return 0, None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": CashPolicy())
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        exposure_levels=(0.0, 0.1, 0.2),
        min_signal_action=1,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1000, cash_by_currency={"USD": 1000}),
        active_insights=(_up_insight(symbol, now, momentum=0.1),),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == 0.1


def test_rl_portfolio_allocator_uses_policy_weight_vector(monkeypatch, tmp_path):
    symbol_a = Symbol("005930", "KRX")
    symbol_b = Symbol("000660", "KRX")
    now = datetime(2026, 1, 2)
    path = tmp_path / "allocator.zip"
    path.write_text("stub", encoding="utf-8")

    class WeightPolicy:
        def predict(self, observation, deterministic=True):
            # Two asset scores plus cash. Normalized weights become 60%, 30%, 10% cash.
            return [0.6, 0.3, 0.1], None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": WeightPolicy())
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        allocation_mode="rl_weights",
        model_name="attention_allocator",
        top_k=2,
        max_position_pct=0.9,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                symbol_a.key: Bar(symbol_a, now, 100, 100, 100, 100, 1000),
                symbol_b.key: Bar(symbol_b, now, 100, 100, 100, 100, 1000),
            },
        ),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(
            _up_insight(symbol_a, now, momentum=0.2),
            _up_insight(symbol_b, now, momentum=0.1),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert [target.symbol.key for target in targets] == ["KRX:005930", "KRX:000660"]
    assert [target.target_percent for target in targets] == pytest.approx([0.6, 0.3])


def test_rl_portfolio_allocator_falls_back_to_score_weighted_targets():
    symbol_a = Symbol("005930", "KRX")
    symbol_b = Symbol("000660", "KRX")
    now = datetime(2026, 1, 2)
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        top_k=2,
        fallback_gross_exposure=0.8,
        max_position_pct=0.9,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                symbol_a.key: Bar(symbol_a, now, 100, 100, 100, 100, 1000),
                symbol_b.key: Bar(symbol_b, now, 100, 100, 100, 100, 1000),
            },
        ),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(
            _up_insight(symbol_a, now, momentum=0.2),
            _up_insight(symbol_b, now, momentum=0.1),
        ),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 2
    assert sum(target.target_percent for target in targets) == pytest.approx(0.8)
    assert targets[0].target_percent > targets[1].target_percent


def test_rl_portfolio_allocator_loads_policy_paths_from_metadata(monkeypatch, tmp_path):
    symbol = Symbol("SMH", "US")
    now = datetime(2026, 1, 2)
    policy_path = tmp_path / "allocator.zip"
    policy_path.write_text("stub", encoding="utf-8")
    metadata_path = tmp_path / "allocator.json"
    metadata_path.write_text(
        json.dumps({"policy_paths": [str(policy_path)]}),
        encoding="utf-8",
    )
    captured = {}

    class WeightPolicy:
        def predict(self, observation, deterministic=True):
            captured["called"] = True
            return [0.8, 0.2], None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": WeightPolicy())
    model = ReinforcementLearningPortfolioConstructionModel(
        metadata_path=metadata_path,
        allocation_mode="rl_weights",
        top_k=1,
        max_position_pct=1.0,
    )
    context = PortfolioConstructionContext(
        sleeve_id="us_etf_rotation",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 570, 570, 570, 570, 1000)}),
        portfolio=Portfolio(cash=2500, cash_by_currency={"USD": 2500}),
        active_insights=(_up_insight(symbol, now, momentum=0.2),),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert captured["called"] is True
    assert len(targets) == 1
    assert targets[0].target_percent == pytest.approx(0.8)


def test_rl_portfolio_allocator_smooths_targets_from_runtime_anchor():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="rl-portfolio-constructor",
                    namespace="target_anchor",
                    symbol_key=symbol.key,
                ),
                value={"target_percent": 0.2},
                generated_at=now,
            ),
        ),
        applied_at=now,
    )
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        fallback_gross_exposure=0.8,
        top_k=1,
        max_position_pct=1.0,
        target_smoothing_alpha=0.5,
        target_drift_threshold_pct=0.03,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(_up_insight(symbol, now, momentum=0.2),),
        managed_symbols=(),
        model_state=state_view,
    )

    targets = model.create_targets(context)
    patches = model.state_patches(context=context, targets=targets)

    assert len(targets) == 1
    assert targets[0].target_percent == pytest.approx(0.5)
    assert ":smoothed=0.500" in targets[0].tag
    assert len(patches) == 1
    assert patches[0].key.symbol_key == symbol.key
    assert patches[0].value["target_percent"] == pytest.approx(0.5)
    assert patches[0].reason == "portfolio_target_anchor"


def test_rl_portfolio_allocator_does_not_smooth_explicit_flat_exit():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    store = InMemoryRuntimeStateStore()
    state_view = RuntimeModelStateView(store=store, default_sleeve_id="LEaps")
    store.apply_patches(
        (
            StatePatch(
                key=state_view.key(
                    model_id="rl-portfolio-constructor",
                    namespace="target_anchor",
                    symbol_key=symbol.key,
                ),
                value={"target_percent": 0.3},
                generated_at=now,
            ),
        ),
        applied_at=now,
    )
    portfolio = Portfolio(cash=0, cash_by_currency={"KRW": 0})
    portfolio.holdings[symbol.key] = type("HoldingLike", (), {"symbol": symbol, "quantity": 3, "average_price": 100})()
    model = ReinforcementLearningPortfolioConstructionModel(
        target_smoothing_alpha=0.5,
        target_drift_threshold_pct=0.03,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=portfolio,
        active_insights=(_flat_insight(symbol, now),),
        managed_symbols=(symbol,),
        model_state=state_view,
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == 0.0
    assert targets[0].tag == "rl:flat-alpha:flat"


def test_rl_portfolio_allocator_uses_signal_floor_when_policy_selects_cash(monkeypatch, tmp_path):
    symbol = Symbol("SMH", "US")
    now = datetime(2026, 1, 2)
    path = tmp_path / "allocator.zip"
    path.write_text("stub", encoding="utf-8")

    class CashPolicy:
        def predict(self, observation, deterministic=True):
            return [0.0, 1.0], None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": CashPolicy())
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        allocation_mode="rl_weights",
        top_k=1,
        fallback_gross_exposure=0.7,
        min_signal_action=1,
        max_position_pct=1.0,
    )
    context = PortfolioConstructionContext(
        sleeve_id="us_etf_rotation",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 570, 570, 570, 570, 1000)}),
        portfolio=Portfolio(cash=2500, cash_by_currency={"USD": 2500}),
        active_insights=(_up_insight(symbol, now, momentum=0.2),),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == pytest.approx(0.7)


def test_rl_training_lot_sizing_reflects_small_account_integer_shares():
    weights = _integer_lot_asset_weights(
        desired_asset_weights=[0.30, 0.30],
        prices=[1_600_000.0, 900_000.0],
        equity=5_000_000.0,
        min_lot_fraction=0.25,
    )

    assert weights.tolist() == pytest.approx([0.32, 0.18])


def test_rl_training_ignores_short_history_symbols_when_aligning_expanded_universe():
    long_a = Symbol("005930", "KRX")
    long_b = Symbol("000660", "KRX")
    recent = Symbol("491000", "KRX")
    universe = UniverseDefinition(
        id="test-expanded",
        market="KRX",
        symbols=(long_a, long_b, recent),
        indicators=(),
    )
    provider = _HistoryProvider(
        {
            long_a.key: 920,
            long_b.key: 900,
            recent.key: 63,
        }
    )

    env = _make_training_env(
        universe,
        provider,
        start=None,
        end=None,
        exposure_levels=(0.0, 0.5, 0.95),
        turnover_penalty=0.12,
        downside_penalty=1.1,
        volatility_penalty=0.45,
        drawdown_penalty=0.95,
        underwater_penalty=0.35,
        missed_upside_penalty=0.08,
        top_k=2,
        concentration_penalty=0.4,
        allocation_mode="rl_weights",
        initial_cash=17_329_806,
        lot_optimizer_min_lot_fraction=0.25,
    )

    assert env.training_symbol_count == 2
    assert env.dropped_history_symbol_count == 1
    assert env.episode_length > 800


def test_rl_temporal_observation_uses_real_lookback_axis():
    prices = np.column_stack(
        [
            np.linspace(100.0, 220.0, 130),
            np.linspace(100.0, 160.0, 130),
        ]
    )

    observation = _asset_token_observation(
        prices,
        100,
        0.63,
        2,
        feature_schema="v2_temporal",
        lookback_window=64,
        current_weights=np.asarray([0.20, 0.10]),
        previous_target_weights=np.asarray([0.30, 0.05]),
    )

    assert observation.shape == (64, 2, 10)
    assert observation[-1, :, 0].tolist() == [1.0, 1.0]
    assert np.all(observation[:-1, :, 7:10] == 0.0)
    assert observation[-1, 0, 7] == pytest.approx(0.20)
    assert observation[-1, 0, 8] == pytest.approx(0.30)
    assert observation[-1, 0, 9] == pytest.approx(0.63)


def test_rl_temporal_training_env_exposes_lookback_topk_feature_shape():
    symbol_a = Symbol("005930", "KRX")
    symbol_b = Symbol("000660", "KRX")
    universe = UniverseDefinition(
        id="test-temporal",
        market="KRX",
        symbols=(symbol_a, symbol_b),
        indicators=(),
    )
    env = _make_training_env(
        universe,
        _HistoryProvider({symbol_a.key: 260, symbol_b.key: 260}),
        start=None,
        end=None,
        exposure_levels=(0.0, 0.5, 0.95),
        turnover_penalty=0.12,
        downside_penalty=1.1,
        volatility_penalty=0.45,
        drawdown_penalty=0.95,
        underwater_penalty=0.35,
        missed_upside_penalty=0.08,
        top_k=2,
        concentration_penalty=0.4,
        allocation_mode="rl_weights",
        initial_cash=5_000_000,
        lot_optimizer_min_lot_fraction=0.25,
        feature_schema="v2_temporal",
        lookback_window=64,
    )

    observation, _ = env.reset()

    assert env.observation_space.shape == (64, 2, 10)
    assert observation.shape == (64, 2, 10)
    assert env.minimum_index >= 84


def test_rl_temporal_runtime_observation_requires_alpha_feature_window():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(_up_insight(symbol, now, momentum=0.2),),
        managed_symbols=(),
    )

    with pytest.raises(RuntimeError, match="temporal feature window"):
        _observation_from_insights(
            context,
            list(context.active_insights),
            currency="KRW",
            top_k=1,
            feature_schema="v2_temporal",
            lookback_window=64,
        )


def test_rl_temporal_portfolio_fails_closed_without_alpha_feature_window():
    symbol = Symbol("005930", "KRX")
    held = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held.key] = type(
        "HoldingLike",
        (),
        {"symbol": held, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        feature_schema="v2_temporal",
        lookback_window=64,
        emit_zero_for_missing_held_targets=True,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000),
                held.key: Bar(held, now, 119_300, 119_300, 119_300, 119_300, 1000),
            },
        ),
        portfolio=portfolio,
        active_insights=(_up_insight(symbol, now, momentum=0.2),),
        managed_symbols=(held,),
    )

    targets = model.create_targets(context)

    assert targets == ()


def test_rl_temporal_portfolio_does_not_exit_when_temporal_feature_window_is_missing():
    symbol = Symbol("005930", "KRX")
    held = Symbol("034020", "KRX")
    now = datetime(2026, 1, 2)
    portfolio = Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000})
    portfolio.holdings[held.key] = type(
        "HoldingLike",
        (),
        {"symbol": held, "quantity": 6, "average_price": 133_100},
    )()
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_path="missing.zip",
        allocation_mode="rl_weights",
        feature_schema="v2_temporal",
        lookback_window=64,
        emit_zero_for_missing_held_targets=True,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(
            time=now,
            bars={
                symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000),
                held.key: Bar(held, now, 119_300, 119_300, 119_300, 119_300, 1000),
            },
        ),
        portfolio=portfolio,
        active_insights=(
            _up_insight(symbol, now, momentum=0.2),
            _flat_insight(held, now),
        ),
        managed_symbols=(held,),
    )

    targets = model.create_targets(context)

    assert targets == ()


def test_rl_temporal_portfolio_uses_alpha_feature_window(monkeypatch, tmp_path):
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 1, 2)
    path = tmp_path / "temporal.zip"
    path.write_text("stub", encoding="utf-8")
    captured = {}

    class WeightPolicy:
        def predict(self, observation, deterministic=True):
            captured["shape"] = observation.shape
            captured["last_token"] = observation[-1, 0].copy()
            return [0.8, 0.2], None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path, device="cpu": WeightPolicy())
    feature_rows = [
        {
            "selected_flag": 1.0,
            "momentum_20": 0.02 + (index * 0.001),
            "volatility_20": 0.04,
            "return_5": 0.01,
            "return_1": 0.002,
            "drawdown_20": 0.03,
            "rank_score": 0.12,
        }
        for index in range(64)
    ]
    insight = Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=5),
        source_snapshot_id="test",
        alpha_id="test-alpha",
        alpha_version="1",
        confidence=0.7,
        score=0.2,
        metadata={"momentum": 0.2, "rl_temporal_features": feature_rows},
    )
    model = ReinforcementLearningPortfolioConstructionModel(
        policy_paths=(path,),
        allocation_mode="rl_weights",
        feature_schema="v2_temporal",
        lookback_window=64,
        top_k=1,
        max_position_pct=1.0,
    )
    context = PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=DataSlice(time=now, bars={symbol.key: Bar(symbol, now, 100, 100, 100, 100, 1000)}),
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        active_insights=(insight,),
        managed_symbols=(),
    )

    targets = model.create_targets(context)

    assert len(targets) == 1
    assert targets[0].target_percent == pytest.approx(0.8)
    assert captured["shape"] == (64, 1, 10)
    assert captured["last_token"][0] == 1.0
    assert captured["last_token"][9] == 0.0


def test_rl_temporal_residual_observation_adds_residual_features():
    prices = np.column_stack(
        [
            np.linspace(100.0, 170.0, 180),
            np.linspace(100.0, 230.0, 180),
            np.linspace(100.0, 210.0, 180),
        ]
    )

    observation = _asset_token_observation(
        prices,
        140,
        0.55,
        2,
        feature_schema="v2_temporal_residual",
        lookback_window=84,
        current_weights=np.asarray([0.15, 0.20, 0.0]),
        previous_target_weights=np.asarray([0.10, 0.18, 0.0]),
        core_asset_indices=(0,),
    )

    assert observation.shape == (84, 2, 13)
    assert observation[-1, :, 0].tolist() == [1.0, 1.0]
    assert np.any(np.abs(observation[-1, :, 2]) > 0.0)
    assert np.all(observation[:-1, :, 10:13] == 0.0)
    assert observation[-1, :, 12].max() == pytest.approx(0.55)


def test_rl_temporal_residual_ranking_reserves_core_bucket():
    days = np.arange(180, dtype=np.float64)
    prices = np.column_stack(
        [
            100.0 + (days * 0.10),
            100.0 + (days * 1.40),
            100.0 + (days * 1.20),
        ]
    )

    ranked_without_core = _ranked_asset_indices(
        prices,
        140,
        2,
        feature_schema="v2_temporal_residual",
    )
    ranked_with_core = _ranked_asset_indices(
        prices,
        140,
        2,
        feature_schema="v2_temporal_residual",
        core_asset_indices=(0,),
    )

    assert 0 not in ranked_without_core
    assert 0 in ranked_with_core
    assert len(ranked_with_core) == 2


def test_rl_temporal_residual_training_env_exposes_expanded_features():
    symbol_a = Symbol("005930", "KRX")
    symbol_b = Symbol("000660", "KRX")
    universe = UniverseDefinition(
        id="test-temporal-residual",
        market="KRX",
        symbols=(symbol_a, symbol_b),
        indicators=(),
        symbol_properties={
            symbol_a.key: {"asset_type": "stock", "market_cap_snapshot": 100_000_000_000},
            symbol_b.key: {"asset_type": "stock", "market_cap_snapshot": 50_000_000_000},
        },
    )
    env = _make_training_env(
        universe,
        _HistoryProvider({symbol_a.key: 300, symbol_b.key: 300}),
        start=None,
        end=None,
        exposure_levels=(0.0, 0.5, 0.95),
        turnover_penalty=0.12,
        downside_penalty=1.1,
        volatility_penalty=0.45,
        drawdown_penalty=0.95,
        underwater_penalty=0.35,
        missed_upside_penalty=0.08,
        top_k=2,
        concentration_penalty=0.4,
        allocation_mode="rl_weights",
        initial_cash=5_000_000,
        lot_optimizer_min_lot_fraction=0.25,
        feature_schema="v2_temporal_residual",
        lookback_window=84,
    )

    observation, _ = env.reset()

    assert env.observation_space.shape == (84, 2, 13)
    assert observation.shape == (84, 2, 13)
    assert env.minimum_index >= 144


class _HistoryProvider:
    def __init__(self, lengths: dict[str, int]) -> None:
        self.lengths = lengths

    def get_history(self, symbol: Symbol, *, start=None, end=None):
        length = self.lengths[symbol.key]
        base = datetime(2021, 1, 1)
        return tuple(
            Bar(
                symbol,
                base + timedelta(days=index),
                100 + index,
                101 + index,
                99 + index,
                100 + index,
                1000,
            )
            for index in range(length)
        )


def _up_insight(symbol: Symbol, now: datetime, *, momentum: float) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        expires_at=now + timedelta(days=5),
        source_snapshot_id="test",
        alpha_id="test-alpha",
        alpha_version="1",
        confidence=0.7,
        score=momentum,
        metadata={"momentum": momentum},
    )


def _flat_insight(symbol: Symbol, now: datetime) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.FLAT,
        generated_at=now,
        expires_at=now + timedelta(days=1),
        source_snapshot_id="test",
        alpha_id="flat-alpha",
        alpha_version="1",
        confidence=0.8,
    )


def _partial_trim_insight(symbol: Symbol, now: datetime, *, target_multiplier: float) -> Insight:
    return Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.FLAT,
        generated_at=now,
        expires_at=now + timedelta(days=1),
        source_snapshot_id="test",
        alpha_id="swing-alpha",
        alpha_version="1",
        confidence=0.8,
        metadata={
            "portfolio_action": "partial_trim",
            "target_multiplier": target_multiplier,
        },
    )

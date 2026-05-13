from datetime import datetime, timedelta
import json

import pytest

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework import PortfolioConstructionContext
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.rl import ReinforcementLearningPortfolioConstructionModel
from leaps_quant_engine.rl.portfolio_constructor import _integer_lot_asset_weights


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
        lambda path: FakePolicy(next(actions)),
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

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path: FakePolicy())
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

    assert len(targets) == 3
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

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path: CashPolicy())
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

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path: WeightPolicy())
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

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path: WeightPolicy())
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


def test_rl_portfolio_allocator_uses_signal_floor_when_policy_selects_cash(monkeypatch, tmp_path):
    symbol = Symbol("SMH", "US")
    now = datetime(2026, 1, 2)
    path = tmp_path / "allocator.zip"
    path.write_text("stub", encoding="utf-8")

    class CashPolicy:
        def predict(self, observation, deterministic=True):
            return [0.0, 1.0], None

    monkeypatch.setattr("leaps_quant_engine.rl.portfolio_constructor._load_ppo_model", lambda path: CashPolicy())
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

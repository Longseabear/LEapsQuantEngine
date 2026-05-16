from datetime import datetime, timedelta

import pytest

from leaps_quant_engine.alpha import AlphaRuntime, Insight, InsightDirection
from leaps_quant_engine.framework import (
    FrameworkRunner,
    PassThroughRiskManagementModel,
    PortfolioAllocationTarget,
    PortfolioBlendEngine,
    PortfolioBlendPolicy,
    PortfolioConstructionContext,
    PortfolioConstructionEngine,
    PortfolioTargetBatch,
    PortfolioTargetResolutionPolicy,
    PortfolioTargetResolver,
    RebalancePolicy,
)
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime_config import ModuleReference, parse_runtime_config
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, RuntimeModelStateView
from leaps_quant_engine.snapshots import IndicatorSnapshot, IndicatorValue


class MutableTargetPortfolioModel:
    def __init__(self, symbol: Symbol, target_percent: float, tag: str = "model") -> None:
        self.symbol = symbol
        self.target_percent = target_percent
        self.tag = tag
        self.calls = 0

    def create_targets(self, context):
        self.calls += 1
        return (
            PortfolioAllocationTarget(
                symbol=self.symbol,
                target_percent=self.target_percent,
                tag=self.tag,
            ),
        )


class OneShotAlpha:
    alpha_id = "blend-alpha"
    version = "1.0"

    def __init__(self, symbol: Symbol) -> None:
        self.symbol = symbol

    def generate(self, context):
        return [
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=self.symbol,
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + timedelta(days=1),
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=self.alpha_id,
                alpha_version=self.version,
            )
        ]


def _bar(symbol: Symbol, as_of: datetime, close: float = 100.0) -> Bar:
    return Bar(symbol, as_of, close, close, close, close, 1000)


def _slice(symbol: Symbol, as_of: datetime, close: float = 100.0) -> DataSlice:
    return DataSlice(time=as_of, bars={symbol.key: _bar(symbol, as_of, close)})


def _snapshot(symbol: Symbol, as_of: datetime) -> IndicatorSnapshot:
    return IndicatorSnapshot(
        snapshot_id=f"indicator-{as_of:%H%M}",
        sleeve_id="blend-sleeve",
        universe_id="blend-universe",
        as_of=as_of,
        created_at=as_of,
        source_snapshot_id=f"market-{as_of:%H%M}",
        symbols=(symbol.key,),
        values={symbol.key: {"close": IndicatorValue("close", 100.0, True, 1, as_of)}},
    )


def _context(symbol: Symbol, as_of: datetime, store: InMemoryRuntimeStateStore) -> PortfolioConstructionContext:
    return PortfolioConstructionContext(
        sleeve_id="blend-sleeve",
        data=_slice(symbol, as_of),
        portfolio=Portfolio(cash=10_000),
        active_insights=(),
        managed_symbols=(symbol,),
        model_state=RuntimeModelStateView(store=store, default_sleeve_id="blend-sleeve"),
    )


def _batch(symbol: Symbol, as_of: datetime, target_percent: float, tag: str = "model") -> PortfolioTargetBatch:
    return PortfolioTargetBatch(
        sleeve_id="blend-sleeve",
        generated_at=as_of,
        targets=(PortfolioAllocationTarget(symbol=symbol, target_percent=target_percent, tag=tag),),
        model_name="Static",
        reason="portfolio_construction",
    )


def _batch_many(as_of: datetime, targets: tuple[PortfolioAllocationTarget, ...]) -> PortfolioTargetBatch:
    return PortfolioTargetBatch(
        sleeve_id="blend-sleeve",
        generated_at=as_of,
        targets=targets,
        model_name="Static",
        reason="portfolio_construction",
    )


def test_portfolio_blend_seeds_then_advances_transition_from_runtime_state():
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    engine = PortfolioBlendEngine(
        PortfolioBlendPolicy(
            enabled=True,
            duration_minutes=300,
            target_drift_threshold_pct=0.05,
            clock="wall_time",
        )
    )
    first_time = datetime(2026, 5, 15, 9, 0)
    first = engine.apply(_context(symbol, first_time, store), _batch(symbol, first_time, 0.20))
    store.apply_patches(first.state_patches, applied_at=first_time)

    second_time = datetime(2026, 5, 15, 9, 5)
    second = engine.apply(
        _context(symbol, second_time, store),
        _batch(symbol, second_time, 0.80),
        previous_batch=_batch(symbol, first_time, 0.20),
    )
    store.apply_patches(second.state_patches, applied_at=second_time)

    halfway_time = datetime(2026, 5, 15, 11, 35)
    halfway = engine.advance(
        _context(symbol, halfway_time, store),
        _batch(symbol, second_time, 0.20),
        previous_batch=_batch(symbol, second_time, 0.20),
    )

    assert first.targets[0].target_percent == pytest.approx(0.20)
    assert first.metadata["portfolio_blend"]["status"] == "seeded"
    assert second.targets[0].target_percent == pytest.approx(0.20)
    assert second.metadata["portfolio_blend"]["status"] == "started"
    assert halfway.targets[0].target_percent == pytest.approx(0.50)
    assert halfway.metadata["portfolio_blend"]["status"] == "advancing"
    assert halfway.metadata["portfolio_blend"]["progress"] == pytest.approx(0.5)


def test_target_resolver_zero_missing_targets_bypass_blend_as_exits():
    old_symbol = Symbol("005930", "KRX")
    new_symbol = Symbol("000660", "KRX")
    store = InMemoryRuntimeStateStore()
    resolver = PortfolioTargetResolver(PortfolioTargetResolutionPolicy(mode="complete"))
    blend = PortfolioBlendEngine(
        PortfolioBlendPolicy(
            enabled=True,
            duration_minutes=60,
            target_drift_threshold_pct=0.01,
            clock="wall_time",
        )
    )
    first_time = datetime(2026, 5, 15, 9, 0)
    old_batch = _batch(old_symbol, first_time, 0.40)
    first = blend.apply(_context(old_symbol, first_time, store), old_batch)
    store.apply_patches(first.state_patches, applied_at=first_time)

    second_time = datetime(2026, 5, 15, 9, 5)
    raw_new = _batch(new_symbol, second_time, 0.60)
    resolved = resolver.resolve(
        _context(new_symbol, second_time, store),
        raw_new,
        previous_batch=old_batch,
    )
    assert {target.symbol.key: target.target_percent for target in resolved.targets} == {
        new_symbol.key: pytest.approx(0.60),
        old_symbol.key: pytest.approx(0.0),
    }
    assert resolved.metadata["portfolio_target_resolution"]["zeroed_symbols"] == [old_symbol.key]

    blended = blend.apply(
        _context(new_symbol, second_time, store),
        _batch_many(second_time, resolved.targets),
        previous_batch=old_batch,
    )
    assert {target.symbol.key: target.target_percent for target in blended.targets} == {
        old_symbol.key: pytest.approx(0.0),
        new_symbol.key: pytest.approx(0.0),
    }
    assert blended.metadata["portfolio_blend"]["bypassed_symbols"] == [old_symbol.key]


def test_target_resolver_patch_mode_carries_forward_missing_old_targets():
    old_symbol = Symbol("005930", "KRX")
    new_symbol = Symbol("000660", "KRX")
    as_of = datetime(2026, 5, 15, 9, 0)
    resolver = PortfolioTargetResolver(PortfolioTargetResolutionPolicy(mode="patch"))
    resolved = resolver.resolve(
        _context(new_symbol, as_of, InMemoryRuntimeStateStore()),
        _batch(new_symbol, as_of, 0.60),
        previous_batch=_batch(old_symbol, as_of, 0.40),
    )

    assert {target.symbol.key: target.target_percent for target in resolved.targets} == {
        new_symbol.key: pytest.approx(0.60),
        old_symbol.key: pytest.approx(0.40),
    }
    assert resolved.metadata["portfolio_target_resolution"]["carried_symbols"] == [old_symbol.key]


def test_target_resolver_complete_mode_does_not_flatten_empty_raw_batch_by_default():
    old_symbol = Symbol("005930", "KRX")
    as_of = datetime(2026, 5, 15, 9, 0)
    resolver = PortfolioTargetResolver(PortfolioTargetResolutionPolicy(mode="complete"))
    resolved = resolver.resolve(
        _context(old_symbol, as_of, InMemoryRuntimeStateStore()),
        _batch_many(as_of, ()),
        previous_batch=_batch(old_symbol, as_of, 0.40),
    )

    assert resolved.targets == ()
    assert resolved.metadata["portfolio_target_resolution"]["status"] == "empty_no_action"


def test_portfolio_blend_bypasses_explicit_flat_exit_targets():
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    engine = PortfolioBlendEngine(
        PortfolioBlendPolicy(
            enabled=True,
            duration_minutes=300,
            target_drift_threshold_pct=0.01,
            clock="wall_time",
        )
    )
    first_time = datetime(2026, 5, 15, 9, 0)
    first = engine.apply(_context(symbol, first_time, store), _batch(symbol, first_time, 0.50))
    store.apply_patches(first.state_patches, applied_at=first_time)

    exit_time = datetime(2026, 5, 15, 9, 5)
    decision = engine.apply(
        _context(symbol, exit_time, store),
        _batch(symbol, exit_time, 0.0, tag="rl:trailing-stop:flat"),
        previous_batch=_batch(symbol, first_time, 0.50),
    )

    assert decision.targets[0].target_percent == 0.0
    assert decision.metadata["portfolio_blend"]["bypassed_symbols"] == [symbol.key]


@pytest.mark.parametrize(
    "tag",
    [
        "rl:ppo:no_longer_in_target_portfolio",
        "portfolio_target_resolver:missing_target_zero",
    ],
)
def test_portfolio_blend_bypasses_engine_exit_target_tags(tag: str):
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    engine = PortfolioBlendEngine(
        PortfolioBlendPolicy(
            enabled=True,
            duration_minutes=60,
            target_drift_threshold_pct=0.01,
            clock="wall_time",
        )
    )
    first_time = datetime(2026, 5, 15, 9, 0)
    first = engine.apply(_context(symbol, first_time, store), _batch(symbol, first_time, 0.50))
    store.apply_patches(first.state_patches, applied_at=first_time)

    exit_time = datetime(2026, 5, 15, 9, 5)
    decision = engine.apply(
        _context(symbol, exit_time, store),
        _batch(symbol, exit_time, 0.0, tag=tag),
        previous_batch=_batch(symbol, first_time, 0.50),
    )

    assert decision.targets[0].target_percent == 0.0
    assert decision.metadata["portfolio_blend"]["bypassed_symbols"] == [symbol.key]


def test_portfolio_blend_retarget_keeps_original_deadline():
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    engine = PortfolioBlendEngine(
        PortfolioBlendPolicy(
            enabled=True,
            duration_minutes=60,
            target_drift_threshold_pct=0.01,
            clock="wall_time",
        )
    )
    seed_time = datetime(2026, 5, 15, 9, 0)
    seed = engine.apply(_context(symbol, seed_time, store), _batch(symbol, seed_time, 0.20))
    store.apply_patches(seed.state_patches, applied_at=seed_time)

    start_time = datetime(2026, 5, 15, 9, 5)
    started = engine.apply(
        _context(symbol, start_time, store),
        _batch(symbol, start_time, 0.80),
        previous_batch=_batch(symbol, seed_time, 0.20),
    )
    store.apply_patches(started.state_patches, applied_at=start_time)

    retarget_time = datetime(2026, 5, 15, 9, 35)
    retargeted = engine.apply(
        _context(symbol, retarget_time, store),
        _batch(symbol, retarget_time, 0.10),
        previous_batch=_batch(symbol, start_time, 0.80),
    )

    blend = retargeted.metadata["portfolio_blend"]
    transition = blend["active_transition"]
    assert blend["status"] == "retargeted"
    assert blend["started_at"] == start_time.isoformat()
    assert blend["elapsed_minutes"] == pytest.approx(30.0)
    assert blend["progress"] == pytest.approx(0.5)
    assert transition["started_at"] == start_time.isoformat()
    assert transition["duration_minutes"] == 60
    assert transition["reason"] == "retarget_during_active_blend"
    assert retargeted.targets[0].target_percent == pytest.approx(0.30)


def test_framework_runner_advances_active_blend_between_portfolio_rebalance_runs():
    symbol = Symbol("005930", "KRX")
    store = InMemoryRuntimeStateStore()
    model = MutableTargetPortfolioModel(symbol, 0.20, tag="rl")
    runner = FrameworkRunner(
        sleeve_id="blend-sleeve",
        alpha_runtime=AlphaRuntime(active_models=(OneShotAlpha(symbol),)),
        portfolio_engine=PortfolioConstructionEngine(
            model=model,
            rebalance_policy=RebalancePolicy(cadence="every_5_minutes"),
        ),
        portfolio_blend_engine=PortfolioBlendEngine(
            PortfolioBlendPolicy(
                enabled=True,
                duration_minutes=60,
                target_drift_threshold_pct=0.01,
                clock="wall_time",
            )
        ),
        risk_model=PassThroughRiskManagementModel(),
        runtime_state_store=store,
    )
    portfolio = Portfolio(cash=10_000)
    first_time = datetime(2026, 5, 15, 9, 0)
    second_time = datetime(2026, 5, 15, 9, 5)
    third_time = datetime(2026, 5, 15, 9, 6)

    first = runner.run_once(
        indicator_snapshot=_snapshot(symbol, first_time),
        data=_slice(symbol, first_time),
        portfolio=portfolio,
    )
    model.target_percent = 0.80
    second = runner.run_once(
        indicator_snapshot=_snapshot(symbol, second_time),
        data=_slice(symbol, second_time),
        portfolio=portfolio,
    )
    third = runner.run_once(
        indicator_snapshot=_snapshot(symbol, third_time),
        data=_slice(symbol, third_time),
        portfolio=portfolio,
    )

    assert first.portfolio_target_batch.targets[0].target_percent == pytest.approx(0.20)
    assert second.stage_decisions["portfolio"]["ran"] is True
    assert second.portfolio_target_batch.targets[0].target_percent == pytest.approx(0.20)
    assert third.stage_decisions["portfolio"]["ran"] is False
    assert third.portfolio_target_batch.targets[0].target_percent == pytest.approx(0.21)
    assert third.portfolio_target_batch.metadata["portfolio_blend"]["status"] == "advancing"
    assert model.calls == 2


def test_runtime_config_parses_portfolio_blend_policy():
    payload = {
        "runtime_id": "blend-runtime",
        "mode": "live",
        "timezone": "Asia/Seoul",
        "sleeves": [
            {
                "sleeve_id": "blend-sleeve",
                "cash": 1_000_000,
                "universe": {"coarse_path": "configs/universes/leaps_kr_research_core.json"},
                "portfolio": {
                    "model": "examples/portfolio_models/equal_weight.py",
                    "target_resolution": {
                        "mode": "patch",
                        "zero_missing_tag": "resolver:zero",
                        "zero_missing_when_raw_empty": True,
                    },
                    "blend": {
                        "enabled": True,
                        "duration_minutes": 300,
                        "target_drift_threshold_pct": 0.05,
                        "clock": "regular_session",
                        "bypass_tag_tokens": ["stop", "urgent"],
                    },
                },
            }
        ],
    }

    config = parse_runtime_config(payload)
    blend = config.sleeve("blend-sleeve").portfolio.blend

    assert config.sleeve("blend-sleeve").portfolio.model == ModuleReference("examples/portfolio_models/equal_weight.py")
    assert config.sleeve("blend-sleeve").portfolio.target_resolution.mode == "patch"
    assert config.sleeve("blend-sleeve").portfolio.target_resolution.zero_missing_tag == "resolver:zero"
    assert config.sleeve("blend-sleeve").portfolio.target_resolution.zero_missing_when_raw_empty is True
    assert blend.enabled is True
    assert blend.duration_minutes == 300
    assert blend.target_drift_threshold_pct == 0.05
    assert blend.clock == "regular_session"
    assert blend.bypass_target_tag_tokens == ("stop", "urgent")
    assert config.to_dict()["sleeves"][0]["portfolio"]["blend"]["enabled"] is True

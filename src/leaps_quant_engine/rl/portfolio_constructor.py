from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.portfolio import currency_for_symbol
from leaps_quant_engine.runtime_state import StatePatch
from leaps_quant_engine.universe.definition import UniverseDefinition


DEFAULT_EXPOSURE_LEVELS = (0.0, 0.25, 0.50, 0.75, 0.95)
DEFAULT_TOP_K = 32
ASSET_FEATURE_COUNT = 8
ALLOCATOR_ACTION_DIM_EXTRA = 1
RL_MAX_NORMALIZED_VOLATILITY = 0.18
RL_EXTREME_NORMALIZED_VOLATILITY = 0.24
RL_HIGH_VOL_MOMENTUM_EXCEPTION = 0.45
RL_VOLATILITY_SCORE_PENALTY = 0.75
RL_MAX_PLAUSIBLE_FEATURE_ABS = 3.0
RL_MAX_PLAUSIBLE_SCORE_ABS = 3.0
RL_MIN_TRAINING_HISTORY_BARS = 252
RL_HISTORY_KEEP_RATIO = 0.80

try:
    import torch
    from torch import nn
    from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
except ImportError:
    torch = None
    nn = None
    BaseFeaturesExtractor = object


class AttentionPortfolioFeaturesExtractor(BaseFeaturesExtractor):
    """Cross-asset attention encoder for top-k portfolio candidate tokens."""

    def __init__(
        self,
        observation_space,
        features_dim: int = 64,
        embed_dim: int = 32,
        num_heads: int = 4,
        num_layers: int = 1,
    ) -> None:
        if torch is None or nn is None:
            raise RuntimeError("torch and stable-baselines3 are required for the attention feature extractor.")
        super().__init__(observation_space, features_dim)
        input_dim = int(observation_space.shape[-1])
        self.input_projection = nn.Linear(input_dim, embed_dim)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        layer = nn.TransformerEncoderLayer(
            d_model=embed_dim,
            nhead=num_heads,
            dim_feedforward=embed_dim * 4,
            batch_first=True,
            activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(layer, num_layers=num_layers)
        self.output = nn.Sequential(
            nn.LayerNorm(embed_dim),
            nn.Linear(embed_dim, features_dim),
            nn.Tanh(),
        )

    def forward(self, observations):
        tokens = observations.float()
        if tokens.ndim == 2:
            tokens = tokens.unsqueeze(0)
        padding_mask = tokens.abs().sum(dim=-1) == 0
        all_padding = padding_mask.all(dim=1)
        if bool(all_padding.any()):
            padding_mask = padding_mask.clone()
            padding_mask[all_padding] = False
        encoded_tokens = self.input_projection(tokens)
        cls = self.cls_token.expand(encoded_tokens.shape[0], -1, -1)
        encoded_tokens = torch.cat((cls, encoded_tokens), dim=1)
        cls_mask = torch.zeros((padding_mask.shape[0], 1), dtype=torch.bool, device=padding_mask.device)
        encoded = self.encoder(encoded_tokens, src_key_padding_mask=torch.cat((cls_mask, padding_mask), dim=1))
        return self.output(encoded[:, 0, :])


@dataclass(frozen=True, slots=True)
class RLPortfolioConstructorTrainingResult:
    model_path: Path
    metadata_path: Path
    timesteps: int
    algorithm: str
    universe_id: str
    start: datetime | None
    end: datetime | None
    symbol_count: int
    episode_length: int
    model_paths: tuple[Path, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "model_path": str(self.model_path),
            "metadata_path": str(self.metadata_path),
            "timesteps": self.timesteps,
            "algorithm": self.algorithm,
            "universe_id": self.universe_id,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "symbol_count": self.symbol_count,
            "episode_length": self.episode_length,
            "model_paths": [str(path) for path in self.model_paths or (self.model_path,)],
        }


@dataclass(frozen=True, slots=True)
class ReinforcementLearningPortfolioConstructionModel:
    policy_path: str | Path | None = None
    policy_paths: tuple[str | Path, ...] = ()
    metadata_path: str | Path | None = None
    exposure_levels: tuple[float, ...] = DEFAULT_EXPOSURE_LEVELS
    fallback_action: int = 3
    max_position_pct: float = 0.35
    long_only: bool = True
    model_name: str = "ppo"
    top_k: int = DEFAULT_TOP_K
    weight_temperature: float = 0.35
    min_position_pct: float = 0.0
    min_signal_action: int = 0
    allocation_mode: str = "equal"
    fallback_gross_exposure: float = 0.75
    emit_zero_for_missing_held_targets: bool = False
    target_smoothing_alpha: float = 1.0
    target_drift_threshold_pct: float = 0.0
    target_anchor_model_id: str = "rl-portfolio-constructor"
    target_anchor_namespace: str = "target_anchor"
    target_membership_namespace: str = "target_membership"
    missing_target_exit_confirmation_cycles: int = 1

    def __post_init__(self) -> None:
        if not 0.0 < self.target_smoothing_alpha <= 1.0:
            raise ValueError("target_smoothing_alpha must be greater than 0 and at most 1.")
        if self.target_drift_threshold_pct < 0:
            raise ValueError("target_drift_threshold_pct cannot be negative.")
        if self.missing_target_exit_confirmation_cycles < 1:
            raise ValueError("missing_target_exit_confirmation_cycles must be at least 1.")

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        actionable_by_currency: dict[str, list] = {}
        blocked_symbol_keys = _latest_non_up_symbol_keys(context.active_insights)
        for insight in _latest_up_insights(context):
            if insight.symbol_key in blocked_symbol_keys:
                continue
            if insight.direction.value != "up":
                continue
            actionable_by_currency.setdefault(currency_for_symbol(insight.symbol), []).append(insight)

        targets: dict[str, PortfolioAllocationTarget] = {}
        for currency, insights in actionable_by_currency.items():
            if not insights:
                continue
            ranked_insights = _rank_insights(insights)[: self.top_k]
            observation = _observation_from_insights(context, ranked_insights, currency=currency, top_k=self.top_k)
            if self.allocation_mode == "rl_weights":
                weighted_targets = self._weights_for_observation(observation, ranked_insights)
                for insight, target_percent in weighted_targets:
                    target_percent = min(target_percent, self.max_position_pct)
                    if target_percent <= self.min_position_pct:
                        continue
                    targets[insight.symbol_key] = PortfolioAllocationTarget(
                        symbol=insight.symbol,
                        target_percent=target_percent,
                        tag=f"rl:{self.model_name}:{insight.alpha_id}:weight={target_percent:.3f}",
                    )
                continue
            exposure = self._exposure_for_observation(observation)
            if exposure <= 0:
                continue
            if self.allocation_mode == "risk_softmax":
                weighted_insights = _risk_aware_insight_weights(
                    insights,
                    temperature=self.weight_temperature,
                )
            else:
                equal = 1.0 / len(insights)
                weighted_insights = tuple((insight, equal) for insight in insights)
            for insight, weight in weighted_insights:
                target_percent = min(exposure * weight, self.max_position_pct)
                if target_percent <= self.min_position_pct:
                    continue
                targets[insight.symbol_key] = PortfolioAllocationTarget(
                    symbol=insight.symbol,
                    target_percent=target_percent,
                    tag=f"rl:{self.model_name}:{insight.alpha_id}:gross={exposure:.2f}:w={weight:.3f}",
                )

        for insight in _latest_exit_insights(context):
            if insight.symbol_key in targets:
                continue
            if context.portfolio.quantity(insight.symbol) == 0:
                continue
            targets[insight.symbol_key] = PortfolioAllocationTarget(
                symbol=insight.symbol,
                target_percent=0.0,
                tag=f"rl:{insight.alpha_id}:{insight.direction.value}",
            )
        if self.emit_zero_for_missing_held_targets:
            self._add_missing_held_zero_targets(
                context,
                targets,
                actionable_currencies=set(actionable_by_currency),
            )
        return self._smooth_targets(context, tuple(targets.values()))

    def state_patches(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[StatePatch, ...]:
        patches: list[StatePatch] = []
        if self._target_smoothing_enabled():
            patches.extend(
                StatePatch(
                    key=context.model_state.key(
                        model_id=self.target_anchor_model_id,
                        namespace=self.target_anchor_namespace,
                        symbol_key=target.symbol.key,
                    ),
                    value={
                        "target_percent": target.target_percent,
                        "tag": target.tag,
                        "updated_at": context.data.time.isoformat(),
                    },
                    reason="portfolio_target_anchor",
                    generated_at=context.data.time,
                )
                for target in targets
            )
        if self.missing_target_exit_confirmation_cycles > 1:
            patches.extend(self._target_membership_patches(context, targets))
        return tuple(patches)

    def _add_missing_held_zero_targets(
        self,
        context: PortfolioConstructionContext,
        targets: dict[str, PortfolioAllocationTarget],
        *,
        actionable_currencies: set[str],
    ) -> None:
        if not actionable_currencies:
            return
        for symbol in context.portfolio.held_symbols:
            if symbol.key in targets:
                continue
            if currency_for_symbol(symbol) not in actionable_currencies:
                continue
            if context.portfolio.quantity(symbol) == 0:
                continue
            missing_count = self._missing_target_count(context, symbol.key) + 1
            if missing_count < self.missing_target_exit_confirmation_cycles:
                hold_percent = self._current_position_percent(context, symbol)
                if hold_percent <= 0:
                    continue
                targets[symbol.key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=hold_percent,
                    tag=(
                        f"rl:{self.model_name}:missing_target_hold:"
                        f"{missing_count}/{self.missing_target_exit_confirmation_cycles}"
                    ),
                )
                continue
            targets[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=0.0,
                tag=f"rl:{self.model_name}:no_longer_in_target_portfolio",
            )

    def _target_membership_patches(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[StatePatch, ...]:
        return tuple(
            StatePatch(
                key=context.model_state.key(
                    model_id=self.target_anchor_model_id,
                    namespace=self.target_membership_namespace,
                    symbol_key=target.symbol.key,
                ),
                value={
                    "missing_count": self._target_missing_count_from_tag(target),
                    "tag": target.tag,
                    "updated_at": context.data.time.isoformat(),
                },
                reason="portfolio_target_membership",
                generated_at=context.data.time,
            )
            for target in targets
        )

    def _target_missing_count_from_tag(self, target: PortfolioAllocationTarget) -> int:
        tag = target.tag or ""
        if "no_longer_in_target_portfolio" in tag:
            return self.missing_target_exit_confirmation_cycles
        marker = ":missing_target_hold:"
        if marker not in tag:
            return 0
        suffix = tag.rsplit(marker, 1)[-1]
        count_text = suffix.split("/", 1)[0]
        try:
            return max(0, int(count_text))
        except ValueError:
            return 1

    def _missing_target_count(self, context: PortfolioConstructionContext, symbol_key: str) -> int:
        record = context.model_state.get(
            model_id=self.target_anchor_model_id,
            namespace=self.target_membership_namespace,
            symbol_key=symbol_key,
        )
        if record is None:
            return 0
        try:
            return max(0, int(record.value.get("missing_count", 0)))
        except (TypeError, ValueError):
            return 0

    def _current_position_percent(self, context: PortfolioConstructionContext, symbol: Symbol) -> float:
        target_value = context.target_value_for_symbol(symbol)
        if target_value <= 0:
            return 0.0
        return _clamp_target_percent(
            context.portfolio.position_value(symbol, context.data) / target_value,
            long_only=self.long_only,
        )

    def _smooth_targets(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[PortfolioAllocationTarget, ...]:
        if not self._target_smoothing_enabled():
            return targets
        smoothed_targets: list[PortfolioAllocationTarget] = []
        for target in targets:
            if _is_explicit_exit_target(target):
                smoothed_targets.append(target)
                continue
            previous_target = self._previous_anchor_target(context, target.symbol.key)
            if previous_target is None:
                smoothed_targets.append(target)
                continue
            smoothed_percent = self._smoothed_target_percent(
                raw_target=target.target_percent,
                previous_target=previous_target,
            )
            if abs(smoothed_percent - target.target_percent) <= 1e-9:
                smoothed_targets.append(target)
                continue
            smoothed_targets.append(
                replace(
                    target,
                    target_percent=smoothed_percent,
                    tag=f"{target.tag}:smoothed={smoothed_percent:.3f}",
                )
            )
        return tuple(smoothed_targets)

    def _smoothed_target_percent(self, *, raw_target: float, previous_target: float) -> float:
        raw = _clamp_target_percent(raw_target, long_only=self.long_only)
        previous = _clamp_target_percent(previous_target, long_only=self.long_only)
        threshold = float(self.target_drift_threshold_pct)
        if abs(raw) <= 1e-12:
            if abs(previous) <= threshold:
                return 0.0
            return _clamp_target_percent(previous * (1.0 - self.target_smoothing_alpha), long_only=self.long_only)
        if abs(raw - previous) < threshold:
            return previous
        return _clamp_target_percent(
            previous + (self.target_smoothing_alpha * (raw - previous)),
            long_only=self.long_only,
        )

    def _previous_anchor_target(self, context: PortfolioConstructionContext, symbol_key: str) -> float | None:
        record = context.model_state.get(
            model_id=self.target_anchor_model_id,
            namespace=self.target_anchor_namespace,
            symbol_key=symbol_key,
        )
        if record is None:
            return None
        return _safe_float(record.value.get("target_percent"))

    def _target_smoothing_enabled(self) -> bool:
        return self.target_smoothing_alpha < 1.0 or self.target_drift_threshold_pct > 0.0

    def _weights_for_observation(
        self,
        observation: np.ndarray,
        ranked_insights: list[Any],
    ) -> tuple[tuple[Any, float], ...]:
        action = self._predict_weight_action(observation)
        if action is None:
            return _score_weighted_targets(
                ranked_insights,
                gross_exposure=max(0.0, min(float(self.fallback_gross_exposure), 1.0)),
                temperature=self.weight_temperature,
            )
        weights = _action_to_insight_weights(action, ranked_insights, top_k=self.top_k)
        if not weights and ranked_insights and self.min_signal_action > 0:
            return _score_weighted_targets(
                ranked_insights,
                gross_exposure=max(0.0, min(float(self.fallback_gross_exposure), 1.0)),
                temperature=self.weight_temperature,
            )
        return weights

    def _exposure_for_observation(self, observation: np.ndarray) -> float:
        action = self._predict_action(observation)
        if self.min_signal_action > 0 and _has_candidate_tokens(observation):
            action = max(action, self.min_signal_action)
        action = max(0, min(action, len(self.exposure_levels) - 1))
        return float(self.exposure_levels[action])

    def _predict_action(self, observation: np.ndarray) -> int:
        model_paths = self._resolved_policy_paths()
        actions: list[int] = []
        for model_path in model_paths:
            if not model_path.exists():
                continue
            try:
                model = _load_ppo_model(str(model_path.resolve()))
                action, _ = model.predict(observation, deterministic=True)
                actions.append(int(np.asarray(action).item()))
            except Exception:
                continue
        if actions:
            return int(round(float(np.median(actions))))
        return int(self.fallback_action)

    def _predict_weight_action(self, observation: np.ndarray) -> np.ndarray | None:
        model_paths = self._resolved_policy_paths()
        actions: list[np.ndarray] = []
        for model_path in model_paths:
            if not model_path.exists():
                continue
            try:
                model = _load_ppo_model(str(model_path.resolve()))
                action, _ = model.predict(observation, deterministic=True)
                vector = np.asarray(action, dtype=np.float64).reshape(-1)
                if vector.size >= 2:
                    actions.append(vector)
            except Exception:
                continue
        if not actions:
            return None
        min_size = min(action.size for action in actions)
        stacked = np.vstack([action[:min_size] for action in actions])
        return np.median(stacked, axis=0)

    def _resolved_policy_paths(self) -> tuple[Path, ...]:
        model_paths = tuple(Path(path) for path in self.policy_paths if path)
        if model_paths:
            return model_paths
        if self.policy_path:
            return (Path(self.policy_path),)
        return _policy_paths_from_metadata(self.metadata_path)


def train_ppo_portfolio_constructor(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    output_dir: str | Path,
    start: datetime | None = None,
    end: datetime | None = None,
    timesteps: int = 10_000,
    seed: int = 7,
    seeds: tuple[int, ...] | None = None,
    exposure_levels: tuple[float, ...] = DEFAULT_EXPOSURE_LEVELS,
    turnover_penalty: float = 0.002,
    downside_penalty: float = 0.25,
    volatility_penalty: float = 0.05,
    drawdown_penalty: float = 0.05,
    underwater_penalty: float = 0.01,
    missed_upside_penalty: float = 0.05,
    top_k: int = DEFAULT_TOP_K,
    concentration_penalty: float = 0.0,
    allocation_mode: str = "exposure",
    initial_cash: float = 5_000_000.0,
    lot_optimizer_min_lot_fraction: float = 0.25,
) -> RLPortfolioConstructorTrainingResult:
    try:
        from stable_baselines3 import PPO
    except ImportError as exc:
        raise RuntimeError("stable-baselines3 is required to train the PPO portfolio constructor.") from exc

    env = _make_training_env(
        universe,
        provider,
        start=start,
        end=end,
        exposure_levels=exposure_levels,
        turnover_penalty=turnover_penalty,
        downside_penalty=downside_penalty,
        volatility_penalty=volatility_penalty,
        drawdown_penalty=drawdown_penalty,
        underwater_penalty=underwater_penalty,
        missed_upside_penalty=missed_upside_penalty,
        top_k=top_k,
        concentration_penalty=concentration_penalty,
        allocation_mode=allocation_mode,
        initial_cash=initial_cash,
        lot_optimizer_min_lot_fraction=lot_optimizer_min_lot_fraction,
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    training_seeds = tuple(seeds or (seed,))
    file_stem = "leaps_ppo_portfolio_allocator" if allocation_mode == "rl_weights" else "leaps_ppo_portfolio_constructor"
    model_path = output / f"{file_stem}.zip"
    metadata_path = output / f"{file_stem}.json"
    model_paths: list[Path] = []
    for model_seed in training_seeds:
        model = PPO(
            "MlpPolicy",
            env,
            verbose=0,
            seed=model_seed,
            n_steps=min(256, max(32, env.episode_length - 1)),
            batch_size=64,
            gamma=0.99,
            learning_rate=0.0003,
            policy_kwargs={
                "features_extractor_class": AttentionPortfolioFeaturesExtractor,
                "features_extractor_kwargs": {
                    "features_dim": 64,
                    "embed_dim": 32,
                    "num_heads": 4,
                    "num_layers": 1,
                },
            },
        )
        model.learn(total_timesteps=timesteps)
        current_model_path = model_path if len(training_seeds) == 1 else output / f"{file_stem}_seed{model_seed}.zip"
        model.save(str(current_model_path))
        model_paths.append(current_model_path)
    metadata = {
        "algorithm": "PPO",
        "library": "stable-baselines3",
        "policy": "MlpPolicy",
        "feature_extractor": "AttentionPortfolioFeaturesExtractor",
        "universe_id": universe.id,
        "market": universe.market,
        "symbols": [symbol.key for symbol in universe.symbols],
        "training_symbol_count": int(getattr(env, "training_symbol_count", len(universe.symbols))),
        "dropped_history_symbol_count": int(getattr(env, "dropped_history_symbol_count", 0)),
        "training_history_min_bars": int(getattr(env, "training_history_min_bars", 0)),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "timesteps": timesteps,
        "seed": seed,
        "seeds": list(training_seeds),
        "ensemble_method": "median_action" if len(training_seeds) > 1 else "single_policy",
        "allocation_mode": allocation_mode,
        "action_space": "Box(top_k+1)" if allocation_mode == "rl_weights" else "Discrete(exposure_levels)",
        "top_k": top_k,
        "exposure_levels": list(exposure_levels),
        "observation_fields": [
            "selected_flag",
            "momentum_20",
            "volatility_20",
            "return_5",
            "return_1",
            "drawdown_20",
            "rank_score",
            "current_exposure",
        ],
        "turnover_penalty": turnover_penalty,
        "downside_penalty": downside_penalty,
        "volatility_penalty": volatility_penalty,
        "drawdown_penalty": drawdown_penalty,
        "underwater_penalty": underwater_penalty,
        "missed_upside_penalty": missed_upside_penalty,
        "concentration_penalty": concentration_penalty,
        "initial_cash": initial_cash,
        "integer_lot_sizing": True,
        "lot_optimizer_min_lot_fraction": lot_optimizer_min_lot_fraction,
        "reward_profile": "finrl_contest_shape_aware",
        "reward_formula": (
            "portfolio_return - turnover_penalty*turnover "
            "- downside_penalty*negative_return - volatility_penalty*rolling_volatility "
            "- drawdown_penalty*drawdown_increase - underwater_penalty*current_drawdown "
            "- missed_upside_penalty*positive_basket_return*(1-exposure) "
            "- concentration_penalty*sum(weights^2)"
        ),
        "policy_paths": [str(path) for path in model_paths],
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return RLPortfolioConstructorTrainingResult(
        model_path=model_path,
        metadata_path=metadata_path,
        timesteps=timesteps,
        algorithm="PPO",
        universe_id=universe.id,
        start=start,
        end=end,
        symbol_count=len(universe.symbols),
        episode_length=env.episode_length,
        model_paths=tuple(model_paths),
    )


def _make_training_env(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    start: datetime | None,
    end: datetime | None,
    exposure_levels: tuple[float, ...],
    turnover_penalty: float,
    downside_penalty: float,
    volatility_penalty: float,
    drawdown_penalty: float,
    underwater_penalty: float,
    missed_upside_penalty: float,
    top_k: int,
    concentration_penalty: float,
    allocation_mode: str,
    initial_cash: float,
    lot_optimizer_min_lot_fraction: float,
):
    try:
        import gymnasium as gym
        from gymnasium import spaces
    except ImportError as exc:
        raise RuntimeError("gymnasium is required to train the PPO portfolio constructor.") from exc

    price_matrix, training_symbol_count, dropped_history_symbol_count = _price_matrix(
        universe,
        provider,
        start=start,
        end=end,
    )

    class PortfolioConstructorEnv(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self) -> None:
            super().__init__()
            if allocation_mode == "rl_weights":
                self.action_space = spaces.Box(
                    low=0.0,
                    high=1.0,
                    shape=(top_k + ALLOCATOR_ACTION_DIM_EXTRA,),
                    dtype=np.float32,
                )
            else:
                self.action_space = spaces.Discrete(len(exposure_levels))
            self.observation_space = spaces.Box(low=-5.0, high=5.0, shape=(top_k, ASSET_FEATURE_COUNT), dtype=np.float32)
            self.episode_length = price_matrix.shape[0] - 21
            self.index = 20
            self.equity = max(float(initial_cash), 1.0)
            self.peak = self.equity
            self.exposure = 0.0
            self.weights = np.zeros(top_k, dtype=np.float64)
            self.asset_weights = np.zeros(price_matrix.shape[1], dtype=np.float64)
            self.returns: list[float] = []

        def reset(self, *, seed: int | None = None, options: dict | None = None):
            super().reset(seed=seed)
            self.index = 20
            self.equity = max(float(initial_cash), 1.0)
            self.peak = self.equity
            self.exposure = 0.0
            self.weights = np.zeros(top_k, dtype=np.float64)
            self.asset_weights = np.zeros(price_matrix.shape[1], dtype=np.float64)
            self.returns = []
            return self._observation(), {}

        def step(self, action):
            daily_returns = (price_matrix[self.index + 1] / price_matrix[self.index]) - 1.0
            selected = self._selected_indices()
            if allocation_mode == "rl_weights":
                token_weights = _allocator_action_to_token_weights(action, len(selected), top_k)
                desired_asset_weights = np.zeros(price_matrix.shape[1], dtype=np.float64)
                weights = token_weights[: len(selected)]
                if len(selected) > 0:
                    desired_asset_weights[selected] = weights
                asset_weights = _integer_lot_asset_weights(
                    desired_asset_weights,
                    prices=price_matrix[self.index],
                    equity=self.equity,
                    min_lot_fraction=lot_optimizer_min_lot_fraction,
                )
                next_exposure = float(np.sum(asset_weights))
                turnover = float(np.sum(np.abs(asset_weights - self.asset_weights)))
            else:
                next_exposure = float(exposure_levels[int(action)])
                weights = _risk_aware_price_weights(price_matrix, self.index, selected)
                desired_asset_weights = np.zeros(price_matrix.shape[1], dtype=np.float64)
                if len(selected) > 0:
                    desired_asset_weights[selected] = weights * next_exposure
                asset_weights = _integer_lot_asset_weights(
                    desired_asset_weights,
                    prices=price_matrix[self.index],
                    equity=self.equity,
                    min_lot_fraction=lot_optimizer_min_lot_fraction,
                )
                next_exposure = float(np.sum(asset_weights))
                turnover = float(np.sum(np.abs(asset_weights - self.asset_weights)))
            basket_return = float(np.sum(daily_returns * asset_weights)) if next_exposure > 0 else 0.0
            concentration = float(np.sum(asset_weights * asset_weights)) if next_exposure > 0 else 0.0
            turnover_cost = turnover * turnover_penalty
            previous_drawdown = 0.0 if self.peak <= 0 else (self.peak - self.equity) / self.peak
            portfolio_return = basket_return - turnover_cost
            self.equity *= 1.0 + portfolio_return
            self.peak = max(self.peak, self.equity)
            current_drawdown = 0.0 if self.peak <= 0 else (self.peak - self.equity) / self.peak
            drawdown_increase = max(0.0, current_drawdown - previous_drawdown)
            self.returns.append(portfolio_return)
            rolling_returns = np.asarray(self.returns[-20:], dtype=np.float64)
            rolling_volatility = float(np.std(rolling_returns)) if len(rolling_returns) > 1 else 0.0
            negative_return = max(0.0, -portfolio_return)
            missed_upside = max(0.0, basket_return) * (1.0 - next_exposure)
            reward = (
                portfolio_return
                - (downside_penalty * negative_return)
                - (volatility_penalty * rolling_volatility)
                - (drawdown_penalty * drawdown_increase)
                - (underwater_penalty * current_drawdown)
                - (missed_upside_penalty * missed_upside)
                - (concentration_penalty * concentration * next_exposure)
            )
            self.exposure = next_exposure
            if allocation_mode == "rl_weights":
                self.weights = token_weights
            self.asset_weights = asset_weights
            self.index += 1
            terminated = self.index >= price_matrix.shape[0] - 2
            return self._observation(), float(reward), terminated, False, {}

        def _observation(self) -> np.ndarray:
            return _asset_token_observation(price_matrix, self.index, self.exposure, top_k)

        def _selected_indices(self) -> np.ndarray:
            momentum = (price_matrix[self.index] / price_matrix[self.index - 20]) - 1.0
            recent = (price_matrix[self.index - 20 : self.index + 1] / price_matrix[self.index - 20 : self.index + 1][0]) - 1.0
            volatility = np.std(np.diff(recent, axis=0), axis=0)
            scores = _volatility_adjusted_scores(momentum, volatility)
            eligible = _volatility_filtered_indices(momentum, volatility)
            if len(eligible) == 0:
                return np.asarray([], dtype=np.int64)
            ranked = eligible[np.argsort(scores[eligible])[::-1]]
            return ranked[:top_k]

    env = PortfolioConstructorEnv()
    env.training_symbol_count = training_symbol_count
    env.dropped_history_symbol_count = dropped_history_symbol_count
    env.training_history_min_bars = int(price_matrix.shape[0])
    if env.episode_length <= 10:
        raise RuntimeError("Not enough historical bars to train RL portfolio constructor.")
    return env


def _price_matrix(
    universe: UniverseDefinition,
    provider: MarketDataProvider,
    *,
    start: datetime | None,
    end: datetime | None,
) -> tuple[np.ndarray, int, int]:
    raw_histories: list[list[float]] = []
    for symbol in universe.symbols:
        bars = provider.get_history(symbol, start=start, end=end)
        closes = [bar.close for bar in bars if bar.close > 0]
        if len(closes) < 30:
            continue
        raw_histories.append(closes)
    if not raw_histories:
        raise RuntimeError("No sufficient price histories available for RL portfolio constructor training.")
    max_len = max(len(history) for history in raw_histories)
    min_required = min(max_len, max(RL_MIN_TRAINING_HISTORY_BARS, int(max_len * RL_HISTORY_KEEP_RATIO)))
    histories = [history for history in raw_histories if len(history) >= min_required]
    dropped_history_symbol_count = len(raw_histories) - len(histories)
    min_len = min((len(history) for history in histories), default=None)
    if not histories or min_len is None or min_len < 30:
        raise RuntimeError("No sufficient price histories available for RL portfolio constructor training.")
    aligned = [history[-min_len:] for history in histories]
    return np.asarray(aligned, dtype=np.float64).T, len(histories), dropped_history_symbol_count


def _latest_up_insights(context: PortfolioConstructionContext) -> tuple[Any, ...]:
    latest = {}
    for insight in context.active_insights:
        if insight.direction.value != "up":
            continue
        if not _is_plausible_actionable_insight(insight):
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at > previous.generated_at:
            latest[insight.symbol_key] = insight
    return tuple(latest.values())


def _is_plausible_actionable_insight(insight: Any) -> bool:
    metadata = getattr(insight, "metadata", {}) or {}
    for key in ("momentum", "momentum_5", "momentum_60", "trend_strength"):
        value = _safe_float(metadata.get(key))
        if value is None:
            continue
        if not math.isfinite(value) or abs(value) > RL_MAX_PLAUSIBLE_FEATURE_ABS:
            return False
    score = _safe_float(getattr(insight, "score", None))
    if score is not None and (not math.isfinite(score) or abs(score) > RL_MAX_PLAUSIBLE_SCORE_ABS):
        return False
    return True


def _latest_non_up_symbol_keys(insights: tuple[Any, ...]) -> set[str]:
    latest = {}
    for insight in insights:
        previous = latest.get(insight.symbol_key)
        if previous is None or _is_newer_or_equal_priority(insight, previous):
            latest[insight.symbol_key] = insight
    return {
        symbol_key
        for symbol_key, insight in latest.items()
        if insight.direction.value != "up"
    }


def _latest_exit_insights(context: PortfolioConstructionContext) -> tuple[Any, ...]:
    latest = {}
    for insight in context.active_insights:
        if insight.direction.value not in {"flat", "down"}:
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or _is_newer_or_equal_priority(insight, previous):
            latest[insight.symbol_key] = insight
    return tuple(latest.values())


def _is_newer_or_equal_priority(candidate: Any, previous: Any) -> bool:
    if candidate.generated_at > previous.generated_at:
        return True
    if candidate.generated_at < previous.generated_at:
        return False
    return _direction_priority(candidate) >= _direction_priority(previous)


def _direction_priority(insight: Any) -> int:
    if insight.direction.value in {"flat", "down"}:
        return 2
    return 1


def _rank_insights(insights: list[Any]) -> list[Any]:
    return sorted(
        insights,
        key=lambda insight: (
            _safe_float(insight.score) if _safe_float(insight.score) is not None else _safe_float(insight.metadata.get("momentum")) or 0.0,
            insight.symbol_key,
        ),
        reverse=True,
    )


def _observation_from_insights(
    context: PortfolioConstructionContext,
    insights: list[Any],
    *,
    currency: str,
    top_k: int,
) -> np.ndarray:
    equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
    exposure = 0.0 if equity <= 0 else context.portfolio.position_value_for_currency(currency, context.data) / equity
    ranked = _rank_insights(insights)[:top_k]
    tokens = np.zeros((top_k, ASSET_FEATURE_COUNT), dtype=np.float32)
    for row, insight in enumerate(ranked):
        momentum = _safe_float(insight.metadata.get("momentum"))
        score = _safe_float(insight.score)
        volatility = _safe_float(insight.metadata.get("volatility"))
        confidence = _safe_float(insight.confidence)
        weight = _safe_float(insight.weight)
        tokens[row] = np.asarray(
            [
                1.0,
                _clip_feature(momentum),
                _clip_feature(volatility),
                _clip_feature(score),
                _clip_feature(confidence),
                _clip_feature(weight),
                _clip_feature((score if score is not None else momentum) or 0.0),
                max(0.0, min(exposure, 1.0)),
            ],
            dtype=np.float32,
        )
    return tokens


def _allocator_action_to_token_weights(action: Any, selected_count: int, top_k: int) -> np.ndarray:
    weights = np.zeros(top_k, dtype=np.float64)
    if selected_count <= 0:
        return weights
    vector = np.asarray(action, dtype=np.float64).reshape(-1)
    if vector.size < top_k + ALLOCATOR_ACTION_DIM_EXTRA:
        vector = np.pad(vector, (0, top_k + ALLOCATOR_ACTION_DIM_EXTRA - vector.size))
    scores = np.clip(vector[:top_k], 0.0, 1.0)
    cash_score = float(np.clip(vector[top_k], 0.0, 1.0))
    scores[selected_count:] = 0.0
    total = float(np.sum(scores) + cash_score)
    if total <= 1e-12:
        return weights
    weights[:selected_count] = scores[:selected_count] / total
    return weights


def _integer_lot_asset_weights(
    desired_asset_weights: np.ndarray,
    *,
    prices: np.ndarray,
    equity: float,
    min_lot_fraction: float,
) -> np.ndarray:
    desired = np.asarray(desired_asset_weights, dtype=np.float64)
    current_prices = np.asarray(prices, dtype=np.float64)
    if equity <= 0 or desired.size == 0:
        return np.zeros_like(desired)

    valid = (desired > 0) & (current_prices > 0)
    if not np.any(valid):
        return np.zeros_like(desired)

    desired_values = desired * equity
    quantities = np.zeros_like(desired, dtype=np.int64)
    quantities[valid] = np.floor(desired_values[valid] / current_prices[valid]).astype(np.int64)

    intended_budget = min(float(np.sum(desired_values[valid])), float(equity))
    spent = float(np.sum(quantities[valid] * current_prices[valid]))
    available = max(0.0, intended_budget - spent)

    while True:
        best_index: int | None = None
        best_score = 0.0
        for index in np.where(valid)[0]:
            price = float(current_prices[index])
            if price <= 0 or price > available:
                continue
            desired_lots = float(desired_values[index] / price)
            lot_gap = desired_lots - float(quantities[index])
            if lot_gap > 0:
                score = min(lot_gap, 1.0)
            elif quantities[index] == 0 and desired_lots >= min_lot_fraction:
                score = desired_lots
            else:
                continue
            if score < min_lot_fraction:
                continue
            score *= max(float(desired[index]), 1e-9)
            if score > best_score:
                best_score = score
                best_index = int(index)
        if best_index is None:
            break
        quantities[best_index] += 1
        available -= float(current_prices[best_index])

    return (quantities * current_prices) / equity


def _action_to_insight_weights(
    action: np.ndarray,
    ranked_insights: list[Any],
    *,
    top_k: int,
) -> tuple[tuple[Any, float], ...]:
    token_weights = _allocator_action_to_token_weights(action, len(ranked_insights), top_k)
    return tuple(
        (insight, float(weight))
        for insight, weight in zip(ranked_insights, token_weights)
        if weight > 0.0
    )


def _score_weighted_targets(
    ranked_insights: list[Any],
    *,
    gross_exposure: float,
    temperature: float,
) -> tuple[tuple[Any, float], ...]:
    weighted = _risk_aware_insight_weights(ranked_insights, temperature=temperature)
    return tuple((insight, gross_exposure * weight) for insight, weight in weighted)


def _asset_token_observation(price_matrix: np.ndarray, index: int, exposure: float, top_k: int) -> np.ndarray:
    momentum = (price_matrix[index] / price_matrix[index - 20]) - 1.0
    recent = (price_matrix[index - 20 : index + 1] / price_matrix[index - 20 : index + 1][0]) - 1.0
    volatility = np.std(np.diff(recent, axis=0), axis=0)
    return_5 = (price_matrix[index] / price_matrix[index - 5]) - 1.0
    return_1 = (price_matrix[index] / price_matrix[index - 1]) - 1.0
    rolling_high = np.max(price_matrix[index - 20 : index + 1], axis=0)
    drawdown = (rolling_high - price_matrix[index]) / rolling_high
    scores = _volatility_adjusted_scores(momentum, volatility)
    eligible = _volatility_filtered_indices(momentum, volatility)
    if len(eligible) == 0:
        return np.zeros((top_k, ASSET_FEATURE_COUNT), dtype=np.float32)
    ranked = eligible[np.argsort(scores[eligible])[::-1]][:top_k]
    tokens = np.zeros((top_k, ASSET_FEATURE_COUNT), dtype=np.float32)
    for row, column in enumerate(ranked):
        tokens[row] = np.asarray(
            [
                1.0,
                _clip_feature(momentum[column]),
                _clip_feature(volatility[column]),
                _clip_feature(return_5[column]),
                _clip_feature(return_1[column]),
                _clip_feature(drawdown[column]),
                _clip_feature(scores[column]),
                max(0.0, min(exposure, 1.0)),
            ],
            dtype=np.float32,
        )
    return tokens


def _risk_aware_price_weights(price_matrix: np.ndarray, index: int, selected: np.ndarray) -> np.ndarray:
    if len(selected) == 0:
        return np.asarray([], dtype=np.float64)
    momentum = (price_matrix[index] / price_matrix[index - 20]) - 1.0
    recent = (price_matrix[index - 20 : index + 1] / price_matrix[index - 20 : index + 1][0]) - 1.0
    volatility = np.std(np.diff(recent, axis=0), axis=0)
    score = momentum[selected] / (volatility[selected] + 1e-6)
    score = np.maximum(score, 0.0)
    if not np.any(score):
        return np.full(len(selected), 1.0 / len(selected), dtype=np.float64)
    scaled = score / max(float(np.std(score)), 1.0)
    scaled = scaled - np.max(scaled)
    weights = np.exp(scaled)
    weights = weights / np.sum(weights)
    return weights


def _volatility_adjusted_scores(momentum: np.ndarray, volatility: np.ndarray) -> np.ndarray:
    return momentum - (volatility * RL_VOLATILITY_SCORE_PENALTY)


def _volatility_filtered_indices(momentum: np.ndarray, volatility: np.ndarray) -> np.ndarray:
    positive = momentum > 0
    normal_volatility = volatility <= RL_MAX_NORMALIZED_VOLATILITY
    high_volatility_exception = (volatility < RL_EXTREME_NORMALIZED_VOLATILITY) & (
        momentum >= RL_HIGH_VOL_MOMENTUM_EXCEPTION
    )
    return np.where(positive & (normal_volatility | high_volatility_exception))[0]


def _risk_aware_insight_weights(
    insights: list[Any],
    *,
    temperature: float,
) -> tuple[tuple[Any, float], ...]:
    if not insights:
        return ()
    scored: list[tuple[Any, float]] = []
    for insight in insights:
        momentum = _safe_float(insight.metadata.get("momentum"))
        score = _safe_float(insight.score)
        volatility = _safe_float(insight.metadata.get("volatility"))
        confidence = _safe_float(insight.confidence)
        weight_hint = _safe_float(insight.weight)
        base = score if score is not None else momentum
        if base is None:
            base = 0.0
        if weight_hint is not None and weight_hint > 0:
            base = max(base, weight_hint)
        risk = 1.0 + max(volatility or 0.0, 0.0)
        quality = max(base, 0.0) * max(confidence or 0.5, 0.0) / risk
        scored.append((insight, quality))
    qualities = np.asarray([quality for _, quality in scored], dtype=np.float64)
    if not np.any(qualities > 0):
        equal = 1.0 / len(scored)
        return tuple((insight, equal) for insight, _ in scored)
    temp = max(float(temperature), 1e-6)
    logits = qualities / temp
    logits = logits - np.max(logits)
    weights = np.exp(logits)
    weights = weights / np.sum(weights)
    return tuple((insight, float(weight)) for (insight, _), weight in zip(scored, weights))


def _has_candidate_tokens(observation: np.ndarray) -> bool:
    array = np.asarray(observation)
    if array.ndim == 1:
        return bool(np.any(np.abs(array) > 1e-12))
    return bool(np.any(array[..., 0] > 0.0))


def _is_explicit_exit_target(target: PortfolioAllocationTarget) -> bool:
    if abs(target.target_percent) > 1e-12:
        return False
    tag = target.tag.lower()
    return ":flat" in tag or ":down" in tag or "stop" in tag


def _clamp_target_percent(value: float, *, long_only: bool) -> float:
    lower = 0.0 if long_only else -1.0
    return max(lower, min(float(value), 1.0))


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _policy_paths_from_metadata(metadata_path: str | Path | None) -> tuple[Path, ...]:
    if not metadata_path:
        return ()
    path = Path(metadata_path)
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    raw_paths = payload.get("policy_paths") or ()
    if not isinstance(raw_paths, list):
        return ()
    base = path.parent
    resolved: list[Path] = []
    for raw_path in raw_paths:
        candidate = Path(str(raw_path))
        if not candidate.is_absolute() and not candidate.exists():
            candidate = base / candidate.name
        resolved.append(candidate)
    return tuple(resolved)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _clip_feature(value: float | None) -> float:
    if value is None or not math.isfinite(value):
        return 0.0
    return max(-5.0, min(float(value), 5.0))


@lru_cache(maxsize=8)
def _load_ppo_model(path: str):
    from stable_baselines3 import PPO

    return PPO.load(path)

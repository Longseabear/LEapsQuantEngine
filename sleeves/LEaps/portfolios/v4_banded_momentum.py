from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Mapping

import numpy as np

from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext
from leaps_quant_engine.portfolio import currency_for_symbol
from leaps_quant_engine.runtime_state import StatePatch


ALPHA_WEIGHTS = {
    "leaps-kospi-conviction": 1.00,
    "leaps-kospi-pullback-reversion": 0.70,
    "leaps-kospi-swing-rebalance": 0.85,
}
REGIME_ALPHA_MULTIPLIERS = {
    "risk_off": {
        "leaps-kospi-conviction": 0.65,
        "leaps-kospi-pullback-reversion": 0.45,
        "leaps-kospi-swing-rebalance": 0.85,
    },
    "neutral": {
        "leaps-kospi-conviction": 1.00,
        "leaps-kospi-pullback-reversion": 1.00,
        "leaps-kospi-swing-rebalance": 1.00,
    },
    "risk_on": {
        "leaps-kospi-conviction": 1.05,
        "leaps-kospi-pullback-reversion": 1.05,
        "leaps-kospi-swing-rebalance": 1.00,
    },
    "strong_risk_on": {
        "leaps-kospi-conviction": 1.15,
        "leaps-kospi-pullback-reversion": 1.10,
        "leaps-kospi-swing-rebalance": 1.00,
    },
}
ETF_SAFETY_ALPHA_ID = "leaps-krx-etf-safety"
DEFAULT_HARD_EXIT_ALPHA_IDS = ("leaps-volatility-trailing-stop",)
DEFAULT_HARD_EXIT_REASON_TOKENS = ("exit", "stop", "20dma_break", "trailing_stop")
PARTIAL_TRIM_ACTION = "partial_trim"
EXIT_ACTION = "exit"
PORTFOLIO_STATE_SYMBOL = "__portfolio__"


class V4BandedMomentumPortfolioConstructionModel:
    """LEAN-style momentum construction with entry/hold/trim/exit bands.

    The model keeps alpha generation stateless: alphas still emit insights only.
    Portfolio construction owns target continuity, turnover control, and held
    symbol hysteresis.
    """

    def __init__(
        self,
        *,
        model_name: str = "v4_banded_momentum",
        model_id: str = "leaps-v4-banded-momentum",
        state_namespace: str = "position_state",
        entry_top_n: int = 12,
        hold_top_n: int = 60,
        trim_top_n: int = 85,
        max_positions: int = 8,
        min_holding_days: int = 3,
        gross_exposure: float = 0.92,
        neutral_gross_exposure: float = 0.68,
        weak_gross_exposure: float = 0.35,
        max_position_pct: float = 0.24,
        min_position_pct: float = 0.015,
        min_entry_lot_count: float = 1.0,
        target_drift_threshold_pct: float = 0.0,
        reentry_cooldown_days: int = 1,
        score_temperature: float = 0.32,
        score_normalization_enabled: bool = True,
        regime_alpha_weighting_enabled: bool = True,
        retention_bonus: float = 0.10,
        multi_alpha_bonus: float = 0.06,
        recent_momentum_weight: float = 0.12,
        trend_weight: float = 0.06,
        volatility_penalty: float = 0.55,
        drawdown_penalty: float = 0.18,
        trim_multiplier: float = 0.50,
        missing_target_exit_confirmation_cycles: int = 2,
        hard_exit_cooldown_days: int | None = None,
        reduce_half_cooldown_days: int = 2,
        add_requires_entry_band: bool = True,
        add_requires_unrealized_profit: bool = True,
        add_min_unrealized_return_pct: float = 0.0,
        max_target_turnover_pct: float | None = 0.18,
        daily_turnover_budget_pct: float | None = 0.35,
        emit_zero_for_missing_held_targets: bool = True,
        long_only: bool = True,
        hard_exit_alpha_ids: tuple[str, ...] = DEFAULT_HARD_EXIT_ALPHA_IDS,
        hard_exit_reason_tokens: tuple[str, ...] = DEFAULT_HARD_EXIT_REASON_TOKENS,
        etf_safety_alpha_id: str = ETF_SAFETY_ALPHA_ID,
    ) -> None:
        self.model_name = model_name
        self.model_id = model_id
        self.state_namespace = state_namespace
        self.entry_top_n = max(1, int(entry_top_n))
        self.hold_top_n = max(self.entry_top_n, int(hold_top_n))
        self.trim_top_n = max(self.hold_top_n, int(trim_top_n))
        self.max_positions = max(1, int(max_positions))
        self.min_holding_days = max(0, int(min_holding_days))
        self.gross_exposure = _clamp_pct(gross_exposure)
        self.neutral_gross_exposure = _clamp_pct(neutral_gross_exposure)
        self.weak_gross_exposure = _clamp_pct(weak_gross_exposure)
        self.max_position_pct = _clamp_pct(max_position_pct)
        self.min_position_pct = _clamp_pct(min_position_pct)
        self.min_entry_lot_count = max(float(min_entry_lot_count), 0.0)
        self.target_drift_threshold_pct = max(float(target_drift_threshold_pct), 0.0)
        self.reentry_cooldown_days = max(int(reentry_cooldown_days), 0)
        self.score_temperature = max(float(score_temperature), 1e-6)
        self.score_normalization_enabled = bool(score_normalization_enabled)
        self.regime_alpha_weighting_enabled = bool(regime_alpha_weighting_enabled)
        self.retention_bonus = max(float(retention_bonus), 0.0)
        self.multi_alpha_bonus = max(float(multi_alpha_bonus), 0.0)
        self.recent_momentum_weight = float(recent_momentum_weight)
        self.trend_weight = float(trend_weight)
        self.volatility_penalty = max(float(volatility_penalty), 0.0)
        self.drawdown_penalty = max(float(drawdown_penalty), 0.0)
        self.trim_multiplier = _clamp_pct(trim_multiplier)
        self.missing_target_exit_confirmation_cycles = max(1, int(missing_target_exit_confirmation_cycles))
        self.hard_exit_cooldown_days = max(
            int(reentry_cooldown_days if hard_exit_cooldown_days is None else hard_exit_cooldown_days),
            0,
        )
        self.reduce_half_cooldown_days = max(int(reduce_half_cooldown_days), 0)
        self.add_requires_entry_band = bool(add_requires_entry_band)
        self.add_requires_unrealized_profit = bool(add_requires_unrealized_profit)
        self.add_min_unrealized_return_pct = float(add_min_unrealized_return_pct)
        self.max_target_turnover_pct = (
            None if max_target_turnover_pct is None else max(float(max_target_turnover_pct), 0.0)
        )
        self.daily_turnover_budget_pct = (
            None if daily_turnover_budget_pct is None else max(float(daily_turnover_budget_pct), 0.0)
        )
        self.emit_zero_for_missing_held_targets = bool(emit_zero_for_missing_held_targets)
        self.long_only = bool(long_only)
        self.hard_exit_alpha_ids = tuple(str(value) for value in hard_exit_alpha_ids)
        self.hard_exit_reason_tokens = tuple(str(value).lower() for value in hard_exit_reason_tokens)
        self.etf_safety_alpha_id = str(etf_safety_alpha_id)
        self._last_state_values: dict[str, dict[str, Any]] = {}
        self._last_portfolio_state: dict[str, Any] | None = None

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        self._last_state_values = {}
        self._last_portfolio_state = None
        stock_insights = tuple(
            insight
            for insight in context.active_insights
            if str(getattr(insight, "alpha_id", "")) != self.etf_safety_alpha_id
        )
        hard_exits = _latest_hard_exit_insights(stock_insights, self)
        trim_multipliers = _partial_trim_multipliers(stock_insights)
        grouped = _group_latest_up_insights(stock_insights, hard_exits)
        normalized_scores = _normalized_alpha_scores(grouped) if self.score_normalization_enabled else {}
        alpha_regime = _alpha_regime_from_insights(stock_insights)
        candidates = [
            candidate
            for candidate in (
                _candidate_from_insights(
                    context,
                    symbol_key,
                    insights,
                    self,
                    normalized_scores=normalized_scores,
                    alpha_regime=alpha_regime,
                )
                for symbol_key, insights in grouped.items()
            )
            if candidate is not None
        ]
        candidates.sort(key=lambda item: (float(item["quality"]), str(item["symbol_key"])), reverse=True)
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank

        target_map: dict[str, PortfolioAllocationTarget] = {}
        candidate_map = {str(candidate["symbol_key"]): candidate for candidate in candidates}
        selected = self._selected_positive_candidates(context, candidates, trim_multipliers=trim_multipliers)
        gross = self._gross_exposure(candidates)
        for symbol_key, target in self._target_map(context, selected, gross).items():
            target_map[symbol_key] = target
            self._last_state_values[symbol_key] = _state_value(
                context,
                self,
                symbol_key,
                status=_target_status_from_tag(target.tag),
                rank=_safe_int(candidate_map.get(symbol_key, {}).get("rank")),
                target_percent=target.target_percent,
                attribution=_candidate_attribution(candidate_map.get(symbol_key)),
                blocked_reason=_safe_str(candidate_map.get(symbol_key, {}).get("blocked_reason")),
            )

        target_map = self._apply_held_band_actions(context, target_map, candidate_map, hard_exits, trim_multipliers)
        if self.emit_zero_for_missing_held_targets:
            target_map = self._zero_missing_held_targets(context, target_map, hard_exits, candidate_map)

        targets = tuple(target_map.values())
        targets = self._cap_target_turnover(context, targets)
        self._sync_state_targets(targets)
        return targets

    def state_patches(
        self,
        *,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[StatePatch, ...]:
        patches: list[StatePatch] = []
        target_percents = {target.symbol.key: target.target_percent for target in targets}
        for symbol_key, value in self._last_state_values.items():
            payload = dict(value)
            if symbol_key in target_percents:
                payload["target_percent"] = target_percents[symbol_key]
            patches.append(
                StatePatch(
                    key=context.model_state.key(
                        model_id=self.model_id,
                        namespace=self.state_namespace,
                        symbol_key=symbol_key,
                    ),
                    value=payload,
                    reason="v4_banded_momentum_mark",
                )
            )
        if self._last_portfolio_state:
            patches.append(
                StatePatch(
                    key=context.model_state.key(
                        model_id=self.model_id,
                        namespace=self.state_namespace,
                        symbol_key=PORTFOLIO_STATE_SYMBOL,
                    ),
                    value=dict(self._last_portfolio_state),
                    reason="v4_banded_momentum_turnover_budget",
                )
            )
        return tuple(patches)

    def _selected_positive_candidates(
        self,
        context: PortfolioConstructionContext,
        candidates: list[dict[str, Any]],
        *,
        trim_multipliers: Mapping[str, float],
    ) -> list[dict[str, Any]]:
        held_keys = {symbol.key for symbol in context.portfolio.held_symbols}
        selected: list[dict[str, Any]] = []
        for candidate in candidates:
            symbol_key = str(candidate["symbol_key"])
            rank = int(candidate["rank"])
            if symbol_key not in held_keys and symbol_key in trim_multipliers:
                candidate["blocked_reason"] = "partial_trim_blocks_new_entry"
                continue
            if symbol_key not in held_keys and not self._reentry_allowed(context, symbol_key):
                candidate["blocked_reason"] = "cooldown_blocks_reentry"
                continue
            if rank <= self.entry_top_n:
                candidate["v4_status"] = "entry" if symbol_key not in held_keys else "hold"
                selected.append(candidate)
                continue
            if symbol_key in held_keys and self._is_hold_allowed(context, symbol_key, rank):
                candidate["v4_status"] = "hold"
                candidate["quality"] = float(candidate["quality"]) + self.retention_bonus
                selected.append(candidate)

        selected.sort(
            key=lambda item: (
                1 if str(item["symbol_key"]) in held_keys else 0,
                float(item["quality"]),
                str(item["symbol_key"]),
            ),
            reverse=True,
        )
        return selected[: self.max_positions]

    def _is_hold_allowed(self, context: PortfolioConstructionContext, symbol_key: str, rank: int) -> bool:
        if rank <= self.hold_top_n:
            return True
        age_days = self._holding_age_days(context, symbol_key)
        return age_days is not None and age_days < self.min_holding_days

    def _gross_exposure(self, candidates: list[dict[str, Any]]) -> float:
        if not candidates:
            return 0.0
        breadth_values = [float(item["market_breadth"]) for item in candidates if item.get("market_breadth") is not None]
        momentum_values = [max(float(item["momentum"]), 0.0) for item in candidates if item.get("momentum") is not None]
        volatility_values = [float(item["volatility"]) for item in candidates if item.get("volatility") is not None]
        breadth = _average(breadth_values)
        momentum = _average(momentum_values)
        volatility = _average(volatility_values)
        if volatility >= 0.18 or breadth < 0.25:
            return self.weak_gross_exposure
        if breadth >= 0.55 and momentum >= 0.18 and volatility <= 0.16:
            return self.gross_exposure
        if breadth >= 0.38 and momentum >= 0.10 and volatility <= 0.17:
            return _clamp_pct((self.gross_exposure + self.neutral_gross_exposure) / 2.0)
        return self.neutral_gross_exposure

    def _target_map(
        self,
        context: PortfolioConstructionContext,
        candidates: list[dict[str, Any]],
        gross: float,
    ) -> dict[str, PortfolioAllocationTarget]:
        if not candidates or gross <= 0:
            return {}
        weights = _softmax_weights([float(item["quality"]) for item in candidates], temperature=self.score_temperature)
        weights = _cap_and_redistribute(weights * gross, cap=self.max_position_pct)
        result: dict[str, PortfolioAllocationTarget] = {}
        for candidate, target_percent in zip(candidates, weights):
            target_percent = _clamp_pct(float(target_percent))
            if target_percent < self.min_position_pct:
                continue
            symbol = candidate["symbol"]
            status = str(candidate.get("v4_status") or "entry")
            if status == "hold":
                current_pct = _current_position_pct(context, symbol.key)
                if target_percent > current_pct:
                    allowed, reason = self._add_allowed(context, candidate, current_pct=current_pct)
                    if not allowed:
                        candidate["blocked_reason"] = reason
                        target_percent = current_pct
                        status = "hold_no_add"
                if target_percent <= 0:
                    continue
            result[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=target_percent,
                tag=(
                    f"v4:{self.model_name}:{status}:rank={int(candidate['rank'])}:"
                    f"q={float(candidate['quality']):.3f}"
                ),
            )
        return result

    def _add_allowed(
        self,
        context: PortfolioConstructionContext,
        candidate: dict[str, Any],
        *,
        current_pct: float,
    ) -> tuple[bool, str | None]:
        if current_pct <= 0:
            return True, None
        if self.add_requires_entry_band and int(candidate["rank"]) > self.entry_top_n:
            return False, "add_blocked_outside_entry_band"
        if not self.add_requires_unrealized_profit:
            return True, None
        unrealized = _unrealized_return_pct(context, str(candidate["symbol_key"]))
        if unrealized is None:
            return False, "add_blocked_missing_unrealized_return"
        if unrealized < self.add_min_unrealized_return_pct:
            return False, "add_blocked_unprofitable_position"
        return True, None

    def _apply_held_band_actions(
        self,
        context: PortfolioConstructionContext,
        targets: dict[str, PortfolioAllocationTarget],
        candidate_map: dict[str, dict[str, Any]],
        hard_exits: dict[str, Any],
        trim_multipliers: dict[str, float],
    ) -> dict[str, PortfolioAllocationTarget]:
        result = dict(targets)
        held_keys = {symbol.key for symbol in context.portfolio.held_symbols}
        for symbol in context.portfolio.held_symbols:
            symbol_key = symbol.key
            if symbol_key in hard_exits:
                result[symbol_key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=0.0,
                    tag=f"v4:{self.model_name}:hard_exit:{_hard_exit_reason(hard_exits[symbol_key])}",
                )
                self._last_state_values[symbol_key] = _state_value(
                    context,
                    self,
                    symbol_key,
                    status="hard_exit",
                    rank=_candidate_rank(candidate_map.get(symbol_key)),
                    target_percent=0.0,
                    attribution=_candidate_attribution(candidate_map.get(symbol_key)),
                    blocked_reason="hard_exit_overrides_up",
                )
                continue

            candidate = candidate_map.get(symbol_key)
            rank = _candidate_rank(candidate)
            if symbol_key in result:
                if symbol_key in trim_multipliers:
                    result[symbol_key] = self._trim_target(context, result[symbol_key], trim_multipliers[symbol_key])
                continue

            if rank is not None and rank <= self.trim_top_n:
                result[symbol_key] = self._trim_target_for_symbol(
                    context,
                    symbol,
                    multiplier=trim_multipliers.get(symbol_key, self.trim_multiplier),
                    reason=f"rank_trim:{rank}",
                )
                self._last_state_values[symbol_key] = _state_value(
                    context,
                    self,
                    symbol_key,
                    status="rank_trim",
                    rank=rank,
                    target_percent=result[symbol_key].target_percent,
                    attribution=_candidate_attribution(candidate),
                    blocked_reason="rank_trim_overrides_add",
                )
                continue

            if symbol_key in trim_multipliers:
                result[symbol_key] = self._trim_target_for_symbol(
                    context,
                    symbol,
                    multiplier=trim_multipliers[symbol_key],
                    reason="partial_trim",
                )
                self._last_state_values[symbol_key] = _state_value(
                    context,
                    self,
                    symbol_key,
                    status="partial_trim",
                    rank=rank,
                    target_percent=result[symbol_key].target_percent,
                    attribution=_candidate_attribution(candidate),
                    blocked_reason="partial_trim_overrides_add",
                )
                continue

            if self._missing_exit_confirmed(context, symbol_key):
                result[symbol_key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=0.0,
                    tag=f"v4:{self.model_name}:missing_exit_confirmed",
                )
                self._last_state_values[symbol_key] = _state_value(
                    context,
                    self,
                    symbol_key,
                    status="missing_exit",
                    rank=rank,
                    target_percent=0.0,
                    missing_count=self._previous_missing_count(context, symbol_key) + 1,
                    attribution=_candidate_attribution(candidate),
                    blocked_reason="missing_target_exit_confirmed",
                )
                continue

            hold_pct = _current_position_pct(context, symbol_key)
            if hold_pct > 0:
                result[symbol_key] = PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=hold_pct,
                    tag=f"v4:{self.model_name}:missing_hold:count={self._previous_missing_count(context, symbol_key) + 1}",
                )
                self._last_state_values[symbol_key] = _state_value(
                    context,
                    self,
                    symbol_key,
                    status="missing_hold",
                    rank=rank,
                    target_percent=hold_pct,
                    missing_count=self._previous_missing_count(context, symbol_key) + 1,
                    attribution=_candidate_attribution(candidate),
                    blocked_reason="missing_target_hold_until_confirmed",
                )
        return result

    def _zero_missing_held_targets(
        self,
        context: PortfolioConstructionContext,
        targets: dict[str, PortfolioAllocationTarget],
        hard_exits: dict[str, Any],
        candidate_map: dict[str, dict[str, Any]],
    ) -> dict[str, PortfolioAllocationTarget]:
        result = dict(targets)
        target_currencies = {currency_for_symbol(target.symbol) for target in targets.values()}
        for symbol in context.portfolio.held_symbols:
            if symbol.key in result:
                continue
            if target_currencies and currency_for_symbol(symbol) not in target_currencies:
                continue
            result[symbol.key] = PortfolioAllocationTarget(
                symbol=symbol,
                target_percent=0.0,
                tag=(
                    f"v4:{self.model_name}:hard_exit"
                    if symbol.key in hard_exits
                    else f"v4:{self.model_name}:no_longer_in_target_portfolio"
                ),
            )
            self._last_state_values[symbol.key] = _state_value(
                context,
                self,
                symbol.key,
                status="zero_missing",
                rank=_candidate_rank(candidate_map.get(symbol.key)),
                target_percent=0.0,
                attribution=_candidate_attribution(candidate_map.get(symbol.key)),
                blocked_reason="no_longer_in_target_portfolio",
            )
        return result

    def _trim_target(self, context: PortfolioConstructionContext, target: PortfolioAllocationTarget, multiplier: float) -> PortfolioAllocationTarget:
        current_pct = _current_position_pct(context, target.symbol.key)
        trim_pct = _clamp_pct(current_pct * _clamp_pct(multiplier))
        if current_pct <= 0:
            trim_pct = min(target.target_percent, trim_pct)
        return PortfolioAllocationTarget(
            symbol=target.symbol,
            target_percent=min(target.target_percent, trim_pct) if trim_pct > 0 else 0.0,
            tag=f"{target.tag}:partial_trim={_clamp_pct(multiplier):.2f}",
        )

    def _trim_target_for_symbol(
        self,
        context: PortfolioConstructionContext,
        symbol: Any,
        *,
        multiplier: float,
        reason: str,
    ) -> PortfolioAllocationTarget:
        current_pct = _current_position_pct(context, symbol.key)
        return PortfolioAllocationTarget(
            symbol=symbol,
            target_percent=_clamp_pct(current_pct * _clamp_pct(multiplier)),
            tag=f"v4:{self.model_name}:{reason}:multiplier={_clamp_pct(multiplier):.2f}",
        )

    def _cap_target_turnover(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[PortfolioAllocationTarget, ...]:
        cap = self._target_turnover_cap(context)
        if cap is None:
            return targets
        adjustable: list[tuple[PortfolioAllocationTarget, float, float]] = []
        fixed: list[PortfolioAllocationTarget] = []
        fixed_by_key: dict[str, PortfolioAllocationTarget] = {}
        for target in targets:
            if _bypasses_turnover_cap(target):
                fixed.append(target)
                fixed_by_key[target.symbol.key] = target
                continue
            previous = self._previous_target_pct(context, target.symbol)
            raw = _clamp_pct(target.target_percent)
            delta = abs(raw - previous)
            if delta <= 1e-12:
                fixed.append(target)
                fixed_by_key[target.symbol.key] = target
                continue
            if (
                self.target_drift_threshold_pct > 0
                and previous > 0
                and raw > 0
                and delta < self.target_drift_threshold_pct
            ):
                drift_target = PortfolioAllocationTarget(
                    symbol=target.symbol,
                    target_percent=previous,
                    tag=f"{target.tag}:drift_hold={previous:.3f}",
                )
                fixed.append(drift_target)
                fixed_by_key[target.symbol.key] = drift_target
                continue
            adjustable.append((target, previous, raw))
        turnover = sum(abs(raw - previous) for _target, previous, raw in adjustable)
        if turnover <= cap or turnover <= 1e-12:
            self._mark_daily_turnover_budget(context, turnover)
            if not fixed_by_key:
                return targets
            return tuple(fixed_by_key.get(target.symbol.key, target) for target in targets)
        applied_turnover = 0.0
        remaining = max(float(cap), 0.0)
        capped_by_key: dict[str, PortfolioAllocationTarget] = {target.symbol.key: target for target in fixed}
        for target, previous, raw in adjustable:
            delta = raw - previous
            if abs(delta) <= remaining + 1e-12:
                capped = raw
            else:
                step = math.copysign(remaining, delta) if remaining > 0 else 0.0
                capped = _clamp_pct(previous + step)
            if self._unbuyable_new_target(context, target, previous=previous, target_percent=capped):
                capped = 0.0
            if capped <= self.min_position_pct and target.target_percent > 0:
                capped = 0.0
            consumed = abs(capped - previous)
            applied_turnover += consumed
            remaining = max(remaining - consumed, 0.0)
            capped_by_key[target.symbol.key] = PortfolioAllocationTarget(
                symbol=target.symbol,
                target_percent=capped,
                tag=f"{target.tag}:turnover_cap={capped:.3f}",
            )
        self._mark_daily_turnover_budget(context, applied_turnover)
        return tuple(capped_by_key[target.symbol.key] for target in targets if target.symbol.key in capped_by_key)

    def _unbuyable_new_target(
        self,
        context: PortfolioConstructionContext,
        target: PortfolioAllocationTarget,
        *,
        previous: float,
        target_percent: float,
    ) -> bool:
        if self.min_entry_lot_count <= 0:
            return False
        if previous > 0 or target_percent <= 0:
            return False
        if context.portfolio.quantity(target.symbol) != 0:
            return False
        price = context.portfolio.mark_price(target.symbol, context.data)
        if price is None or price <= 0:
            return False
        target_value = context.target_value_for_symbol(target.symbol)
        if target_value <= 0:
            return False
        return target_value * target_percent < price * self.min_entry_lot_count

    def _target_turnover_cap(self, context: PortfolioConstructionContext) -> float | None:
        cycle_cap = None if self.max_target_turnover_pct is None else max(float(self.max_target_turnover_pct), 0.0)
        daily_remaining = self._daily_turnover_remaining(context)
        if daily_remaining is None:
            return cycle_cap
        if cycle_cap is None:
            return daily_remaining
        return min(cycle_cap, daily_remaining)

    def _daily_turnover_remaining(self, context: PortfolioConstructionContext) -> float | None:
        if self.daily_turnover_budget_pct is None:
            return None
        budget = max(float(self.daily_turnover_budget_pct), 0.0)
        record = context.model_state.get(
            model_id=self.model_id,
            namespace=self.state_namespace,
            symbol_key=PORTFOLIO_STATE_SYMBOL,
        )
        today = context.data.time.date().isoformat()
        used = 0.0
        if record is not None and str(record.value.get("turnover_date") or "") == today:
            used = max(_safe_float(record.value.get("turnover_used_pct")) or 0.0, 0.0)
        return max(budget - used, 0.0)

    def _mark_daily_turnover_budget(self, context: PortfolioConstructionContext, applied_turnover: float) -> None:
        if self.daily_turnover_budget_pct is None:
            return
        budget = max(float(self.daily_turnover_budget_pct), 0.0)
        today = context.data.time.date().isoformat()
        record = context.model_state.get(
            model_id=self.model_id,
            namespace=self.state_namespace,
            symbol_key=PORTFOLIO_STATE_SYMBOL,
        )
        used = 0.0
        if record is not None and str(record.value.get("turnover_date") or "") == today:
            used = max(_safe_float(record.value.get("turnover_used_pct")) or 0.0, 0.0)
        self._last_portfolio_state = {
            "turnover_date": today,
            "turnover_used_pct": min(budget, used + max(float(applied_turnover), 0.0)),
            "turnover_budget_pct": budget,
            "updated_at": context.data.time.isoformat(),
        }

    def _previous_target_pct(self, context: PortfolioConstructionContext, symbol: Any) -> float:
        record = context.model_state.get(model_id=self.model_id, namespace=self.state_namespace, symbol_key=symbol.key)
        if record is not None:
            value = _safe_float(record.value.get("target_percent"))
            if value is not None:
                return _clamp_pct(value)
        return _current_position_pct(context, symbol.key)

    def _reentry_allowed(self, context: PortfolioConstructionContext, symbol_key: str) -> bool:
        record = context.model_state.get(model_id=self.model_id, namespace=self.state_namespace, symbol_key=symbol_key)
        if record is None:
            return True
        status = str(record.value.get("status") or "").strip().lower()
        cooldown_days = self._cooldown_days_for_status(status)
        if cooldown_days is None:
            return True
        if cooldown_days <= 0:
            return True
        updated_at = str(record.value.get("updated_at") or "").strip()
        if not updated_at:
            return True
        try:
            updated = datetime.fromisoformat(updated_at)
        except ValueError:
            return True
        age_days = max((context.data.time.date() - updated.date()).days, 0)
        return age_days >= cooldown_days

    def _cooldown_days_for_status(self, status: str) -> int | None:
        if status == "hard_exit" or "hard_exit" in status or "stop" in status:
            return self.hard_exit_cooldown_days
        if "reduce" in status or "trim" in status:
            return self.reduce_half_cooldown_days
        if status in {"missing_exit", "zero_missing", "exit"} or "exit" in status:
            return self.reentry_cooldown_days
        return None

    def _holding_age_days(self, context: PortfolioConstructionContext, symbol_key: str) -> int | None:
        record = context.model_state.get(model_id=self.model_id, namespace=self.state_namespace, symbol_key=symbol_key)
        if record is None:
            return None
        entered_at = str(record.value.get("entered_at") or "").strip()
        if not entered_at:
            return None
        try:
            entered = datetime.fromisoformat(entered_at)
        except ValueError:
            return None
        return max((context.data.time.date() - entered.date()).days, 0)

    def _previous_missing_count(self, context: PortfolioConstructionContext, symbol_key: str) -> int:
        record = context.model_state.get(model_id=self.model_id, namespace=self.state_namespace, symbol_key=symbol_key)
        if record is None:
            return 0
        value = _safe_int(record.value.get("missing_count"))
        return max(value or 0, 0)

    def _missing_exit_confirmed(self, context: PortfolioConstructionContext, symbol_key: str) -> bool:
        age_days = self._holding_age_days(context, symbol_key)
        if age_days is not None and age_days < self.min_holding_days:
            return False
        return self._previous_missing_count(context, symbol_key) + 1 >= self.missing_target_exit_confirmation_cycles

    def _sync_state_targets(self, targets: tuple[PortfolioAllocationTarget, ...]) -> None:
        for target in targets:
            payload = self._last_state_values.setdefault(
                target.symbol.key,
                {
                    "status": _target_status_from_tag(target.tag),
                    "rank": None,
                    "target_percent": target.target_percent,
                },
            )
            payload["target_percent"] = target.target_percent
            payload["status"] = _target_status_from_tag(target.tag)


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> V4BandedMomentumPortfolioConstructionModel:
    values = dict(params or {})
    return V4BandedMomentumPortfolioConstructionModel(
        model_name=str(values.get("model_name", "v4_banded_momentum")),
        model_id=str(values.get("model_id", "leaps-v4-banded-momentum")),
        state_namespace=str(values.get("state_namespace", "position_state")),
        entry_top_n=int(values.get("entry_top_n", 12)),
        hold_top_n=int(values.get("hold_top_n", 60)),
        trim_top_n=int(values.get("trim_top_n", 85)),
        max_positions=int(values.get("max_positions", 8)),
        min_holding_days=int(values.get("min_holding_days", 3)),
        gross_exposure=float(values.get("gross_exposure", 0.92)),
        neutral_gross_exposure=float(values.get("neutral_gross_exposure", 0.68)),
        weak_gross_exposure=float(values.get("weak_gross_exposure", 0.35)),
        max_position_pct=float(values.get("max_position_pct", 0.24)),
        min_position_pct=float(values.get("min_position_pct", 0.015)),
        min_entry_lot_count=float(values.get("min_entry_lot_count", 1.0)),
        target_drift_threshold_pct=float(values.get("target_drift_threshold_pct", 0.0)),
        reentry_cooldown_days=int(values.get("reentry_cooldown_days", 1)),
        score_temperature=float(values.get("score_temperature", 0.32)),
        score_normalization_enabled=bool(values.get("score_normalization_enabled", True)),
        regime_alpha_weighting_enabled=bool(values.get("regime_alpha_weighting_enabled", True)),
        retention_bonus=float(values.get("retention_bonus", 0.10)),
        multi_alpha_bonus=float(values.get("multi_alpha_bonus", 0.06)),
        recent_momentum_weight=float(values.get("recent_momentum_weight", 0.12)),
        trend_weight=float(values.get("trend_weight", 0.06)),
        volatility_penalty=float(values.get("volatility_penalty", 0.55)),
        drawdown_penalty=float(values.get("drawdown_penalty", 0.18)),
        trim_multiplier=float(values.get("trim_multiplier", 0.50)),
        missing_target_exit_confirmation_cycles=int(values.get("missing_target_exit_confirmation_cycles", 2)),
        hard_exit_cooldown_days=(
            int(values["hard_exit_cooldown_days"]) if values.get("hard_exit_cooldown_days") is not None else None
        ),
        reduce_half_cooldown_days=int(values.get("reduce_half_cooldown_days", 2)),
        add_requires_entry_band=bool(values.get("add_requires_entry_band", True)),
        add_requires_unrealized_profit=bool(values.get("add_requires_unrealized_profit", True)),
        add_min_unrealized_return_pct=float(values.get("add_min_unrealized_return_pct", 0.0)),
        max_target_turnover_pct=(
            float(values["max_target_turnover_pct"])
            if values.get("max_target_turnover_pct") is not None
            else None
        ),
        daily_turnover_budget_pct=(
            float(values["daily_turnover_budget_pct"])
            if values.get("daily_turnover_budget_pct") is not None
            else None
        ),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", True)),
        long_only=bool(values.get("long_only", True)),
        hard_exit_alpha_ids=tuple(values.get("hard_exit_alpha_ids", DEFAULT_HARD_EXIT_ALPHA_IDS)),
        hard_exit_reason_tokens=tuple(values.get("hard_exit_reason_tokens", DEFAULT_HARD_EXIT_REASON_TOKENS)),
        etf_safety_alpha_id=str(values.get("etf_safety_alpha_id", ETF_SAFETY_ALPHA_ID)),
    )


def _group_latest_up_insights(insights: tuple[Any, ...], hard_exits: dict[str, Any]) -> dict[str, list[Any]]:
    latest: dict[tuple[str, str], Any] = {}
    for insight in insights:
        if getattr(getattr(insight, "direction", None), "value", "") != "up":
            continue
        if insight.symbol_key in hard_exits:
            continue
        if not _is_plausible(insight):
            continue
        key = (insight.symbol_key, str(getattr(insight, "alpha_id", "")))
        previous = latest.get(key)
        if previous is None or insight.generated_at >= previous.generated_at:
            latest[key] = insight
    grouped: dict[str, list[Any]] = {}
    for (symbol_key, _alpha_id), insight in latest.items():
        grouped.setdefault(symbol_key, []).append(insight)
    return grouped


def _candidate_from_insights(
    context: PortfolioConstructionContext,
    symbol_key: str,
    insights: list[Any],
    model: V4BandedMomentumPortfolioConstructionModel,
    *,
    normalized_scores: Mapping[tuple[str, str], float],
    alpha_regime: str,
) -> dict[str, Any] | None:
    if not insights:
        return None
    best = max(insights, key=lambda insight: _safe_float(getattr(insight, "score", None)) or 0.0)
    metadata = _merged_metadata(insights)
    momentum = _safe_float(metadata.get("momentum")) or 0.0
    momentum_5 = _safe_float(metadata.get("momentum_5")) or 0.0
    trend_strength = _safe_float(metadata.get("trend_strength")) or 0.0
    volatility = _safe_float(metadata.get("volatility")) or 0.0
    pullback = _safe_float(metadata.get("pullback_from_high")) or 0.0
    source_score = 0.0
    confidence_sum = 0.0
    alpha_sources: dict[str, dict[str, float]] = {}
    for insight in insights:
        alpha_id = str(getattr(insight, "alpha_id", ""))
        alpha_weight = ALPHA_WEIGHTS.get(alpha_id, 0.45)
        if model.regime_alpha_weighting_enabled:
            alpha_weight *= REGIME_ALPHA_MULTIPLIERS.get(alpha_regime, {}).get(alpha_id, 1.0)
        score = _safe_float(getattr(insight, "score", None))
        if score is None:
            score = _safe_float(getattr(insight, "magnitude", None)) or momentum
        normalized_score = normalized_scores.get((symbol_key, alpha_id), _score_to_unit(score))
        confidence = _safe_float(getattr(insight, "confidence", None)) or 0.5
        contribution = max(normalized_score, 0.0) * alpha_weight * confidence
        source_score += contribution
        confidence_sum += alpha_weight
        alpha_sources[alpha_id] = {
            "raw_score": float(score or 0.0),
            "normalized_score": float(normalized_score),
            "confidence": float(confidence),
            "alpha_weight": float(alpha_weight),
            "contribution": float(contribution),
        }
    source_score = source_score / confidence_sum if confidence_sum > 0 else 0.0
    held_bonus = model.retention_bonus if context.portfolio.quantity(best.symbol) != 0 else 0.0
    recent_momentum_component = model.recent_momentum_weight * max(momentum_5, 0.0)
    trend_component = model.trend_weight * max(trend_strength, 0.0)
    multi_alpha_component = model.multi_alpha_bonus * max(len(insights) - 1, 0)
    volatility_component = -model.volatility_penalty * max(volatility - 0.10, 0.0)
    drawdown_component = -model.drawdown_penalty * max(pullback, 0.0)
    quality = (
        source_score
        + recent_momentum_component
        + trend_component
        + multi_alpha_component
        + held_bonus
        + volatility_component
        + drawdown_component
    )
    if quality <= 0:
        return None
    return {
        "symbol": best.symbol,
        "symbol_key": symbol_key,
        "quality": quality,
        "momentum": momentum,
        "volatility": volatility,
        "market_breadth": _safe_float(metadata.get("market_breadth")),
        "alpha_regime": alpha_regime,
        "attribution": {
            "symbol": symbol_key,
            "final_quality": float(quality),
            "source_score": float(source_score),
            "alpha_sources": alpha_sources,
            "components": {
                "recent_momentum": float(recent_momentum_component),
                "trend": float(trend_component),
                "multi_alpha": float(multi_alpha_component),
                "held_bonus": float(held_bonus),
                "volatility_penalty": float(volatility_component),
                "drawdown_penalty": float(drawdown_component),
            },
            "regime": alpha_regime,
            "insight_ids": [str(getattr(insight, "insight_id", "")) for insight in insights],
        },
    }


def _latest_hard_exit_insights(insights: tuple[Any, ...], model: V4BandedMomentumPortfolioConstructionModel) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for insight in insights:
        if not _is_hard_exit(insight, model):
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at >= previous.generated_at:
            latest[insight.symbol_key] = insight
    return latest


def _is_hard_exit(insight: Any, model: V4BandedMomentumPortfolioConstructionModel) -> bool:
    direction = getattr(getattr(insight, "direction", None), "value", "")
    if direction not in {"flat", "down"}:
        return False
    alpha_id = str(getattr(insight, "alpha_id", ""))
    if alpha_id in model.hard_exit_alpha_ids:
        return True
    metadata = getattr(insight, "metadata", {}) or {}
    action = str(metadata.get("portfolio_action") or "").strip().lower()
    if action == EXIT_ACTION:
        return True
    reason = str(getattr(insight, "reason", "") or "").lower()
    return any(token in reason for token in model.hard_exit_reason_tokens)


def _partial_trim_multipliers(insights: tuple[Any, ...]) -> dict[str, float]:
    latest: dict[str, Any] = {}
    for insight in insights:
        metadata = getattr(insight, "metadata", {}) or {}
        if str(metadata.get("portfolio_action") or "").strip().lower() != PARTIAL_TRIM_ACTION:
            continue
        previous = latest.get(insight.symbol_key)
        if previous is None or insight.generated_at >= previous.generated_at:
            latest[insight.symbol_key] = insight
    result: dict[str, float] = {}
    for symbol_key, insight in latest.items():
        metadata = getattr(insight, "metadata", {}) or {}
        result[symbol_key] = _clamp_pct(_safe_float(metadata.get("target_multiplier")) or 0.50)
    return result


def _state_value(
    context: PortfolioConstructionContext,
    model: V4BandedMomentumPortfolioConstructionModel,
    symbol_key: str,
    *,
    status: str,
    rank: int | None,
    target_percent: float,
    missing_count: int = 0,
    attribution: Mapping[str, Any] | None = None,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    previous = context.model_state.get(
        model_id=model.model_id,
        namespace=model.state_namespace,
        symbol_key=symbol_key,
    )
    entered_at = ""
    if previous is not None:
        entered_at = str(previous.value.get("entered_at") or "")
    if not entered_at and target_percent > 0:
        entered_at = context.data.time.isoformat()
    if target_percent <= 0:
        entered_at = ""
    payload = {
        "status": status,
        "rank": rank,
        "target_percent": _clamp_pct(target_percent),
        "missing_count": max(int(missing_count), 0),
        "entered_at": entered_at,
        "updated_at": context.data.time.isoformat(),
    }
    if attribution:
        payload["attribution"] = dict(attribution)
    if blocked_reason:
        payload["blocked_reason"] = blocked_reason
    return payload


def _current_position_pct(context: PortfolioConstructionContext, symbol_key: str) -> float:
    holding = context.portfolio.holdings.get(symbol_key)
    if holding is None or holding.quantity == 0:
        return 0.0
    price = context.portfolio.mark_price(holding.symbol, context.data)
    if price is None or price <= 0:
        return 0.0
    target_value = context.target_value_for_symbol(holding.symbol)
    if target_value <= 0:
        return 0.0
    return _clamp_pct(abs(holding.quantity * price) / target_value)


def _softmax_weights(values: list[float], *, temperature: float) -> np.ndarray:
    if not values:
        return np.asarray([], dtype=np.float64)
    logits = np.asarray(values, dtype=np.float64) / max(float(temperature), 1e-6)
    logits = logits - float(np.max(logits))
    raw = np.exp(logits)
    total = float(np.sum(raw))
    if total <= 1e-12:
        return np.full(len(values), 1.0 / len(values), dtype=np.float64)
    return raw / total


def _cap_and_redistribute(weights: np.ndarray, *, cap: float) -> np.ndarray:
    result = np.maximum(np.asarray(weights, dtype=np.float64), 0.0)
    cap = _clamp_pct(cap)
    if cap <= 0 or result.size == 0:
        return np.zeros_like(result)
    for _ in range(result.size + 1):
        over = result > cap
        if not np.any(over):
            break
        excess = float(np.sum(result[over] - cap))
        result[over] = cap
        under = result < cap - 1e-12
        if not np.any(under) or excess <= 1e-12:
            break
        total_under = float(np.sum(result[under]))
        if total_under <= 1e-12:
            result[under] += excess / int(np.sum(under))
        else:
            result[under] += excess * (result[under] / total_under)
    return np.minimum(result, cap)


def _bypasses_turnover_cap(target: PortfolioAllocationTarget) -> bool:
    tag = target.tag.lower()
    return target.target_percent == 0.0 and any(token in tag for token in ("hard_exit", "stop", "exit"))


def _target_status_from_tag(tag: str) -> str:
    if "hard_exit" in tag:
        return "hard_exit"
    if "partial_trim" in tag:
        return "partial_trim"
    if "rank_trim" in tag:
        return "rank_trim"
    if "hold_no_add" in tag:
        return "hold_no_add"
    if ":entry:" in tag:
        return "entry"
    if ":hold:" in tag:
        return "hold"
    if "trim" in tag:
        return "trim"
    if "exit" in tag:
        return "exit"
    return "target"


def _hard_exit_reason(insight: Any) -> str:
    reason = str(getattr(insight, "reason", "") or "").strip()
    if reason:
        return reason
    return str(getattr(insight, "alpha_id", "") or "exit")


def _candidate_rank(candidate: dict[str, Any] | None) -> int | None:
    if not candidate:
        return None
    return _safe_int(candidate.get("rank"))


def _merged_metadata(insights: list[Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for insight in sorted(insights, key=lambda item: item.generated_at):
        merged.update(dict(getattr(insight, "metadata", {}) or {}))
    return merged


def _normalized_alpha_scores(grouped: Mapping[str, list[Any]]) -> dict[tuple[str, str], float]:
    latest_by_alpha: dict[str, list[tuple[str, float]]] = {}
    for symbol_key, insights in grouped.items():
        for insight in insights:
            alpha_id = str(getattr(insight, "alpha_id", ""))
            score = _insight_score(insight)
            latest_by_alpha.setdefault(alpha_id, []).append((symbol_key, score))
    result: dict[tuple[str, str], float] = {}
    for alpha_id, rows in latest_by_alpha.items():
        if len(rows) == 1:
            symbol_key, score = rows[0]
            result[(symbol_key, alpha_id)] = _score_to_unit(score)
            continue
        ordered = sorted(rows, key=lambda item: (item[1], item[0]))
        denominator = max(len(ordered) - 1, 1)
        for rank, (symbol_key, _score) in enumerate(ordered):
            percentile = rank / denominator
            result[(symbol_key, alpha_id)] = 0.25 + 0.75 * percentile
    return result


def _alpha_regime_from_insights(insights: tuple[Any, ...]) -> str:
    breadth_values: list[float] = []
    momentum_values: list[float] = []
    volatility_values: list[float] = []
    for insight in insights:
        if getattr(getattr(insight, "direction", None), "value", "") != "up":
            continue
        metadata = getattr(insight, "metadata", {}) or {}
        breadth = _safe_float(metadata.get("market_breadth"))
        momentum = _safe_float(metadata.get("momentum"))
        volatility = _safe_float(metadata.get("volatility"))
        if breadth is not None:
            breadth_values.append(breadth)
        if momentum is not None:
            momentum_values.append(momentum)
        if volatility is not None:
            volatility_values.append(volatility)
    breadth = _average(breadth_values)
    momentum = _average(momentum_values)
    volatility = _average(volatility_values)
    if breadth < 0.25 or volatility >= 0.18:
        return "risk_off"
    if breadth >= 0.55 and momentum >= 0.18 and volatility <= 0.14:
        return "strong_risk_on"
    if breadth >= 0.45 and momentum >= 0.08 and volatility <= 0.16:
        return "risk_on"
    return "neutral"


def _candidate_attribution(candidate: dict[str, Any] | None) -> dict[str, Any]:
    if not candidate:
        return {}
    value = candidate.get("attribution")
    return dict(value) if isinstance(value, Mapping) else {}


def _insight_score(insight: Any) -> float:
    score = _safe_float(getattr(insight, "score", None))
    if score is not None:
        return score
    magnitude = _safe_float(getattr(insight, "magnitude", None))
    if magnitude is not None:
        return magnitude
    metadata = getattr(insight, "metadata", {}) or {}
    momentum = _safe_float(metadata.get("momentum"))
    return momentum or 0.0


def _score_to_unit(score: float | None) -> float:
    if score is None:
        return 0.5
    return _clamp_pct(0.5 + 0.5 * float(score))


def _unrealized_return_pct(context: PortfolioConstructionContext, symbol_key: str) -> float | None:
    holding = context.portfolio.holdings.get(symbol_key)
    if holding is None or holding.quantity == 0 or holding.average_price <= 0:
        return None
    price = context.portfolio.mark_price(holding.symbol, context.data)
    if price is None or price <= 0:
        return None
    return (float(price) / float(holding.average_price)) - 1.0


def _safe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_plausible(insight: Any) -> bool:
    metadata = getattr(insight, "metadata", {}) or {}
    for key in ("momentum", "momentum_5", "momentum_60", "trend_strength"):
        value = _safe_float(metadata.get(key))
        if value is not None and (not math.isfinite(value) or abs(value) > 3.0):
            return False
    score = _safe_float(getattr(insight, "score", None))
    return score is None or (math.isfinite(score) and abs(score) <= 3.0)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _clamp_pct(value: float) -> float:
    return max(0.0, min(float(value), 1.0))


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _safe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

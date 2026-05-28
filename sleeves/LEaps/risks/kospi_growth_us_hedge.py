from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from math import ceil

from leaps_quant_engine.framework.risk import (
    RiskDecision,
    RiskDecisionBatch,
    RiskDecisionStatus,
    RiskManagementContext,
)
from leaps_quant_engine.models import PortfolioTarget
from leaps_quant_engine.portfolio import currency_for_symbol
from leaps_quant_engine.runtime_state import StatePatch


MODEL_ID = "leaps-kospi-growth-us-hedge-risk"
REGIME_EQUITY_NAMESPACE = "regime_equity"
INTRADAY_GUARD_NAMESPACE = "intraday_guard"
SYMBOL_GUARD_NAMESPACE = "symbol_guard"
NO_OVERLAY = "none"
ENTRY_FREEZE = "entry_freeze"
INTRADAY_RISK_OFF = "intraday_risk_off"
ETF_SAFETY_ALPHA_ID = "leaps-krx-etf-safety"


class LeapsKospiGrowthUsHedgeRiskModel:
    def __init__(
        self,
        *,
        long_only: bool = True,
        max_position_pct_by_currency: dict[str, float] | None = None,
        max_total_exposure_pct_by_currency: dict[str, float] | None = None,
        cash_buffer_pct_by_currency: dict[str, float] | None = None,
        regime_exposure_enabled: bool = False,
        regime_total_exposure_pct_by_currency: dict[str, dict[str, float]] | None = None,
        regime_equity_overlay_enabled: bool = False,
        entry_freeze_drawdown_pct_by_currency: dict[str, float] | None = None,
        risk_off_drawdown_pct_by_currency: dict[str, float] | None = None,
        entry_freeze_cycle_loss_pct_by_currency: dict[str, float] | None = None,
        risk_off_cycle_loss_pct_by_currency: dict[str, float] | None = None,
        entry_freeze_cap_pct_by_currency: dict[str, float] | None = None,
        risk_off_cap_pct_by_currency: dict[str, float] | None = None,
        recovery_from_trough_pct_by_currency: dict[str, float] | None = None,
        recovery_confirmation_cycles: int = 2,
        intraday_market_guard_enabled: bool = False,
        intraday_guard_symbol: str = "KRX:069500",
        intraday_guard_reference_alpha_id: str = ETF_SAFETY_ALPHA_ID,
        intraday_entry_freeze_return_pct: float = -0.02,
        intraday_risk_off_return_pct: float = -0.04,
        intraday_entry_freeze_cap_pct_by_currency: dict[str, float] | None = None,
        intraday_risk_off_cap_pct_by_currency: dict[str, float] | None = None,
        intraday_guard_exempt_symbols: tuple[str, ...] = (),
        intraday_open_entry_freeze_until: str | None = None,
        intraday_guard_high_drawdown_enabled: bool = False,
        intraday_guard_high_entry_freeze_return_pct: float = -0.006,
        intraday_guard_high_risk_off_return_pct: float = -0.012,
        intraday_guard_smoothing_enabled: bool = False,
        intraday_guard_cap_curve: str = "smoothstep",
        intraday_guard_hard_entry_freeze: bool = True,
        intraday_guard_recovery_enabled: bool = False,
        intraday_guard_recovery_from_low_pct: float = 0.006,
        intraday_guard_recovery_confirmation_cycles: int = 2,
        intraday_guard_recovery_cap_pct_by_currency: dict[str, float] | None = None,
        symbol_guard_enabled: bool = False,
        symbol_guard_exempt_symbols: tuple[str, ...] = (),
        symbol_entry_block_intraday_return_pct: float = -0.025,
        symbol_entry_block_high_drawdown_pct: float = -0.040,
        symbol_entry_block_unrealized_loss_pct: float = -0.015,
        symbol_entry_block_sma10_buffer_pct: float | None = None,
        symbol_entry_block_sma20_buffer_pct: float | None = None,
        symbol_reduce_half_unrealized_loss_pct: float = -0.035,
        symbol_exit_unrealized_loss_pct: float = -0.060,
        symbol_reduce_half_high_drawdown_pct: float = -0.065,
        symbol_exit_high_drawdown_pct: float = -0.100,
        symbol_reduce_half_sma10_buffer_pct: float = -0.005,
        symbol_exit_sma20_buffer_pct: float = -0.005,
        symbol_reduce_fraction: float = 0.50,
        symbol_guard_volatility_adjusted_enabled: bool = False,
        symbol_guard_reference_volatility_pct: float = 0.04,
        symbol_guard_min_volatility_multiplier: float = 0.75,
        symbol_guard_max_volatility_multiplier: float = 1.75,
        symbol_guard_entry_max_volatility_multiplier: float = 1.25,
        symbol_guard_recovery_confirmation_cycles: int = 3,
        symbol_pullback_add_enabled: bool = False,
        symbol_pullback_add_fraction: float = 0.50,
        symbol_pullback_add_min_intraday_return_pct: float = 0.0,
        symbol_pullback_add_min_unrealized_pnl_pct: float = 0.0,
        symbol_pullback_add_min_sma10_gap_pct: float = 0.0,
        symbol_pullback_add_min_sma20_gap_pct: float = 0.0,
        symbol_pullback_add_min_alpha_count: int = 2,
        reject_invalid_snapshot: bool = True,
        require_fresh_for_entries: bool = True,
    ) -> None:
        self.long_only = long_only
        self.max_position_pct_by_currency = max_position_pct_by_currency or {"KRW": 0.40, "USD": 0.30}
        self.max_total_exposure_pct_by_currency = max_total_exposure_pct_by_currency or {"KRW": 0.95, "USD": 0.65}
        self.cash_buffer_pct_by_currency = cash_buffer_pct_by_currency or {"KRW": 0.02, "USD": 0.08}
        self.regime_exposure_enabled = regime_exposure_enabled
        self.regime_total_exposure_pct_by_currency = regime_total_exposure_pct_by_currency or {
            "KRW": {
                "risk_off": 0.35,
                "neutral": 0.60,
                "risk_on": 0.78,
                "strong_risk_on": 0.85,
            }
        }
        self.regime_equity_overlay_enabled = regime_equity_overlay_enabled
        self.entry_freeze_drawdown_pct_by_currency = entry_freeze_drawdown_pct_by_currency or {"KRW": 0.035}
        self.risk_off_drawdown_pct_by_currency = risk_off_drawdown_pct_by_currency or {"KRW": 0.055}
        self.entry_freeze_cycle_loss_pct_by_currency = entry_freeze_cycle_loss_pct_by_currency or {"KRW": 0.025}
        self.risk_off_cycle_loss_pct_by_currency = risk_off_cycle_loss_pct_by_currency or {"KRW": 0.050}
        self.entry_freeze_cap_pct_by_currency = entry_freeze_cap_pct_by_currency or {"KRW": 0.85}
        self.risk_off_cap_pct_by_currency = risk_off_cap_pct_by_currency or {"KRW": 0.70}
        self.recovery_from_trough_pct_by_currency = recovery_from_trough_pct_by_currency or {"KRW": 0.006}
        self.recovery_confirmation_cycles = max(1, int(recovery_confirmation_cycles))
        self.intraday_market_guard_enabled = intraday_market_guard_enabled
        self.intraday_guard_symbol = str(intraday_guard_symbol or "KRX:069500").strip().upper()
        self.intraday_guard_reference_alpha_id = str(intraday_guard_reference_alpha_id or ETF_SAFETY_ALPHA_ID)
        self.intraday_entry_freeze_return_pct = float(intraday_entry_freeze_return_pct)
        self.intraday_risk_off_return_pct = float(intraday_risk_off_return_pct)
        self.intraday_entry_freeze_cap_pct_by_currency = intraday_entry_freeze_cap_pct_by_currency or {"KRW": 0.55}
        self.intraday_risk_off_cap_pct_by_currency = intraday_risk_off_cap_pct_by_currency or {"KRW": 0.35}
        self.intraday_guard_exempt_symbols = {
            str(symbol).strip().upper()
            for symbol in intraday_guard_exempt_symbols
            if str(symbol).strip()
        }
        self.intraday_open_entry_freeze_until = _parse_time(intraday_open_entry_freeze_until)
        self.intraday_guard_high_drawdown_enabled = intraday_guard_high_drawdown_enabled
        self.intraday_guard_high_entry_freeze_return_pct = float(intraday_guard_high_entry_freeze_return_pct)
        self.intraday_guard_high_risk_off_return_pct = float(intraday_guard_high_risk_off_return_pct)
        self.intraday_guard_smoothing_enabled = intraday_guard_smoothing_enabled
        self.intraday_guard_cap_curve = str(intraday_guard_cap_curve or "smoothstep").strip().lower()
        self.intraday_guard_hard_entry_freeze = intraday_guard_hard_entry_freeze
        self.intraday_guard_recovery_enabled = intraday_guard_recovery_enabled
        self.intraday_guard_recovery_from_low_pct = max(0.0, float(intraday_guard_recovery_from_low_pct))
        self.intraday_guard_recovery_confirmation_cycles = max(1, int(intraday_guard_recovery_confirmation_cycles))
        self.intraday_guard_recovery_cap_pct_by_currency = (
            intraday_guard_recovery_cap_pct_by_currency
            or {"KRW": self.intraday_entry_freeze_cap_pct_by_currency.get("KRW", 0.45)}
        )
        self.symbol_guard_enabled = symbol_guard_enabled
        self.symbol_guard_exempt_symbols = {
            str(symbol).strip().upper()
            for symbol in symbol_guard_exempt_symbols
            if str(symbol).strip()
        }
        self.symbol_entry_block_intraday_return_pct = float(symbol_entry_block_intraday_return_pct)
        self.symbol_entry_block_high_drawdown_pct = float(symbol_entry_block_high_drawdown_pct)
        self.symbol_entry_block_unrealized_loss_pct = float(symbol_entry_block_unrealized_loss_pct)
        self.symbol_entry_block_sma10_buffer_pct = (
            None if symbol_entry_block_sma10_buffer_pct is None else float(symbol_entry_block_sma10_buffer_pct)
        )
        self.symbol_entry_block_sma20_buffer_pct = (
            None if symbol_entry_block_sma20_buffer_pct is None else float(symbol_entry_block_sma20_buffer_pct)
        )
        self.symbol_reduce_half_unrealized_loss_pct = float(symbol_reduce_half_unrealized_loss_pct)
        self.symbol_exit_unrealized_loss_pct = float(symbol_exit_unrealized_loss_pct)
        self.symbol_reduce_half_high_drawdown_pct = float(symbol_reduce_half_high_drawdown_pct)
        self.symbol_exit_high_drawdown_pct = float(symbol_exit_high_drawdown_pct)
        self.symbol_reduce_half_sma10_buffer_pct = float(symbol_reduce_half_sma10_buffer_pct)
        self.symbol_exit_sma20_buffer_pct = float(symbol_exit_sma20_buffer_pct)
        self.symbol_reduce_fraction = min(1.0, max(0.0, float(symbol_reduce_fraction)))
        self.symbol_guard_volatility_adjusted_enabled = symbol_guard_volatility_adjusted_enabled
        self.symbol_guard_reference_volatility_pct = max(0.0001, float(symbol_guard_reference_volatility_pct))
        self.symbol_guard_min_volatility_multiplier = max(0.1, float(symbol_guard_min_volatility_multiplier))
        self.symbol_guard_max_volatility_multiplier = max(
            self.symbol_guard_min_volatility_multiplier,
            float(symbol_guard_max_volatility_multiplier),
        )
        self.symbol_guard_entry_max_volatility_multiplier = max(
            self.symbol_guard_min_volatility_multiplier,
            float(symbol_guard_entry_max_volatility_multiplier),
        )
        self.symbol_guard_recovery_confirmation_cycles = max(1, int(symbol_guard_recovery_confirmation_cycles))
        self.symbol_pullback_add_enabled = bool(symbol_pullback_add_enabled)
        self.symbol_pullback_add_fraction = min(1.0, max(0.0, float(symbol_pullback_add_fraction)))
        self.symbol_pullback_add_min_intraday_return_pct = float(symbol_pullback_add_min_intraday_return_pct)
        self.symbol_pullback_add_min_unrealized_pnl_pct = float(symbol_pullback_add_min_unrealized_pnl_pct)
        self.symbol_pullback_add_min_sma10_gap_pct = float(symbol_pullback_add_min_sma10_gap_pct)
        self.symbol_pullback_add_min_sma20_gap_pct = float(symbol_pullback_add_min_sma20_gap_pct)
        self.symbol_pullback_add_min_alpha_count = max(0, int(symbol_pullback_add_min_alpha_count))
        self.reject_invalid_snapshot = reject_invalid_snapshot
        self.require_fresh_for_entries = require_fresh_for_entries

    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        decisions: list[RiskDecision] = []
        currencies = sorted({currency_for_symbol(target.symbol) for target in context.targets} | set(context.portfolio.currencies()))
        if not currencies:
            currencies = sorted(context.portfolio.currencies(context.data))
        equity_by_currency = context.portfolio.equity_by_currency(context.data, currencies)
        regime = self._market_regime(context)
        regime = self._apply_equity_overlay(context, regime, currencies, equity_by_currency)
        max_total_exposure_pct_by_currency = self._regime_total_exposure_pct_by_currency(regime)
        approved_quantities = {
            holding.symbol.key: holding.quantity
            for holding in context.portfolio.holdings.values()
            if holding.quantity != 0
        }
        approved_symbols = {
            holding.symbol.key: holding.symbol
            for holding in context.portfolio.holdings.values()
            if holding.quantity != 0
        }
        cash_by_currency = context.portfolio.cash_by_currency_for(currencies)
        available_cash = {
            currency: max(0.0, cash_by_currency.get(currency, 0.0) * (1.0 - self.cash_buffer_pct_by_currency.get(currency, 0.03)))
            for currency in currencies
        }
        for target in context.targets:
            decision, remaining_cash = self._evaluate_target(
                context,
                target,
                approved_quantities,
                approved_symbols,
                available_cash,
                max_total_exposure_pct_by_currency,
                regime,
            )
            available_cash[currency_for_symbol(target.symbol)] = remaining_cash
            if decision.approved_target is not None:
                approved_quantities[decision.approved_target.symbol.key] = decision.approved_target.quantity
                approved_symbols[decision.approved_target.symbol.key] = decision.approved_target.symbol
            decisions.append(decision)
        processed_symbols = {target.symbol.key for target in context.targets}
        for target in self._exposure_cap_deleverage_targets(
            context,
            processed_symbols=processed_symbols,
            approved_quantities=approved_quantities,
            approved_symbols=approved_symbols,
            max_total_exposure_pct_by_currency=max_total_exposure_pct_by_currency,
            regime=regime,
        ):
            currency = currency_for_symbol(target.symbol)
            if not self._currency_exposure_above_cap(
                context,
                currency=currency,
                approved_quantities=approved_quantities,
                approved_symbols=approved_symbols,
                max_total_exposure_pct_by_currency=max_total_exposure_pct_by_currency,
                regime=regime,
            ):
                continue
            decision, remaining_cash = self._evaluate_target(
                context,
                target,
                approved_quantities,
                approved_symbols,
                available_cash,
                max_total_exposure_pct_by_currency,
                regime,
            )
            available_cash[currency] = remaining_cash
            if decision.approved_target is not None:
                approved_quantities[decision.approved_target.symbol.key] = decision.approved_target.quantity
                approved_symbols[decision.approved_target.symbol.key] = decision.approved_target.symbol
            decisions.append(decision)
        return RiskDecisionBatch(
            sleeve_id=context.sleeve_id,
            decisions=tuple(decisions),
            state_patches=self._state_patches(context, regime, decisions=tuple(decisions)),
        )

    def _exposure_cap_deleverage_targets(
        self,
        context: RiskManagementContext,
        *,
        processed_symbols: set[str],
        approved_quantities: dict[str, int],
        approved_symbols: dict[str, object],
        max_total_exposure_pct_by_currency: dict[str, float],
        regime: dict[str, object],
    ) -> tuple[PortfolioTarget, ...]:
        candidates: list[tuple[float, float, str, PortfolioTarget]] = []
        for holding in context.portfolio.holdings.values():
            if holding.quantity <= 0:
                continue
            symbol_key = holding.symbol.key
            if symbol_key in processed_symbols:
                continue
            currency = currency_for_symbol(holding.symbol)
            if self._intraday_cap_exempts_symbol(regime, currency, symbol_key):
                continue
            if not self._currency_exposure_above_cap(
                context,
                currency=currency,
                approved_quantities=approved_quantities,
                approved_symbols=approved_symbols,
                max_total_exposure_pct_by_currency=max_total_exposure_pct_by_currency,
                regime=regime,
            ):
                continue
            price = context.portfolio.mark_price(holding.symbol, context.data)
            if price is None or price <= 0:
                continue
            average_price = _safe_float(getattr(holding, "average_price", None))
            unrealized_pct = (price / average_price) - 1.0 if average_price and average_price > 0 else 0.0
            market_value = abs(float(holding.quantity) * price)
            candidates.append(
                (
                    unrealized_pct,
                    -market_value,
                    symbol_key,
                    PortfolioTarget(
                        symbol=holding.symbol,
                        quantity=holding.quantity,
                        tag="risk:exposure_cap_deleverage",
                    ),
                )
            )
        targets: list[PortfolioTarget] = []
        for _, _, _, target in sorted(candidates):
            currency = currency_for_symbol(target.symbol)
            if not self._currency_exposure_above_cap(
                context,
                currency=currency,
                approved_quantities=approved_quantities,
                approved_symbols=approved_symbols,
                max_total_exposure_pct_by_currency=max_total_exposure_pct_by_currency,
                regime=regime,
            ):
                continue
            targets.append(target)
        return tuple(targets)

    def _currency_exposure_above_cap(
        self,
        context: RiskManagementContext,
        *,
        currency: str,
        approved_quantities: dict[str, int],
        approved_symbols: dict[str, object],
        max_total_exposure_pct_by_currency: dict[str, float],
        regime: dict[str, object],
    ) -> bool:
        equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
        if equity <= 0:
            return False
        cap_pct = max_total_exposure_pct_by_currency.get(currency, 0.80)
        if cap_pct >= 1.0:
            return False
        cap = equity * cap_pct
        exposure = 0.0
        for symbol_key, quantity in approved_quantities.items():
            symbol = approved_symbols.get(symbol_key)
            if symbol is None or currency_for_symbol(symbol) != currency:
                continue
            if self._intraday_cap_exempts_symbol(regime, currency, symbol_key):
                continue
            mark = context.portfolio.mark_price(symbol, context.data)
            if mark is None:
                continue
            exposure += abs(quantity * mark)
        return exposure > cap + max(1.0, equity * 0.0001)

    def _intraday_cap_exempts_symbol(self, regime: dict[str, object], currency: str, symbol_key: str) -> bool:
        currency_regime = _currency_regime(regime, currency)
        if str(currency_regime.get("source") or "") != "intraday_market_guard":
            return False
        exempt_symbols = {
            str(symbol).strip().upper()
            for symbol in currency_regime.get("exempt_symbols", ())
        }
        return str(symbol_key).upper() in exempt_symbols

    def _evaluate_target(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        approved_quantities: dict[str, int],
        approved_symbols: dict[str, object],
        available_cash_by_currency: dict[str, float],
        max_total_exposure_pct_by_currency: dict[str, float],
        regime: dict[str, object],
    ) -> tuple[RiskDecision, float]:
        currency = currency_for_symbol(target.symbol)
        current_quantity = context.portfolio.quantity(target.symbol)
        available_cash = available_cash_by_currency.get(currency, 0.0)
        if self.long_only and target.quantity < 0:
            return _reject(target, "short_target_rejected", {"long_only": True}), available_cash
        currency_regime = _currency_regime(regime, currency)
        hard_entry_freeze = bool(currency_regime.get("hard_entry_freeze", currency_regime.get("entry_freeze", False)))
        if (
            target.quantity > current_quantity
            and hard_entry_freeze
            and target.symbol.key.upper() not in self.intraday_guard_exempt_symbols
        ):
            approved = PortfolioTarget(symbol=target.symbol, quantity=current_quantity, tag=target.tag)
            return (
                RiskDecision(
                    original_target=target,
                    approved_target=approved,
                    status=RiskDecisionStatus.CLAMPED,
                    reason="regime_entry_freeze",
                    metadata={
                        "currency": currency,
                        "current_quantity": current_quantity,
                        "requested_quantity": target.quantity,
                        "market_regime": regime,
                    },
                ),
                available_cash,
            )
        if (
            self.require_fresh_for_entries
            and context.snapshot_quality is not None
            and target.quantity > current_quantity
            and not context.snapshot_quality.allows_new_entries
        ):
            return _reject(target, "snapshot_quality_blocks_entry", {"snapshot_quality": context.snapshot_quality.to_dict()}), available_cash

        price = context.portfolio.mark_price(target.symbol, context.data)
        if price is None or price <= 0:
            return _reject(target, "missing_or_invalid_price", {"currency": currency}), available_cash

        symbol_guard_decision = self._symbol_guard_decision(
            context=context,
            target=target,
            price=price,
            current_quantity=current_quantity,
            regime=regime,
        )
        if symbol_guard_decision is not None:
            return symbol_guard_decision, available_cash

        position_limited_quantity = self._clamp_position(context, target, price)
        exposure_limited_quantity = self._clamp_total_exposure(
            context,
            target,
            position_limited_quantity,
            price,
            approved_quantities,
            approved_symbols,
            max_total_exposure_pct_by_currency,
            regime,
        )
        cash_limited_quantity, remaining_cash = self._clamp_cash(
            current_quantity=current_quantity,
            target_quantity=exposure_limited_quantity,
            price=price,
            available_cash=available_cash,
        )
        if cash_limited_quantity == current_quantity and target.quantity != current_quantity:
            return (
                _reject(
                    target,
                    _no_room_reason(
                        requested_quantity=target.quantity,
                        current_quantity=current_quantity,
                        position_limited_quantity=position_limited_quantity,
                        exposure_limited_quantity=exposure_limited_quantity,
                        cash_limited_quantity=cash_limited_quantity,
                    ),
                    {
                        "currency": currency,
                        "price": price,
                        "current_quantity": current_quantity,
                        "requested_quantity": target.quantity,
                        "position_limited_quantity": position_limited_quantity,
                        "exposure_limited_quantity": exposure_limited_quantity,
                        "cash_limited_quantity": cash_limited_quantity,
                        "available_cash": available_cash,
                        "available_cash_after": remaining_cash,
                        "max_position_pct": self.max_position_pct_by_currency.get(currency),
                        "max_total_exposure_pct": max_total_exposure_pct_by_currency.get(currency),
                        "base_max_total_exposure_pct": self.max_total_exposure_pct_by_currency.get(currency),
                        "cash_buffer_pct": self.cash_buffer_pct_by_currency.get(currency),
                        "market_regime": regime,
                    },
                ),
                remaining_cash,
            )

        approved_tag = target.tag
        if cash_limited_quantity < current_quantity:
            approved_tag = _risk_tag(target.tag, "currency_policy_reduce")
        approved = PortfolioTarget(symbol=target.symbol, quantity=cash_limited_quantity, tag=approved_tag)
        status = RiskDecisionStatus.APPROVED if approved.quantity == target.quantity else RiskDecisionStatus.CLAMPED
        return (
            RiskDecision(
                original_target=target,
                approved_target=approved,
                status=status,
                reason="approved" if status is RiskDecisionStatus.APPROVED else "currency_policy_clamped",
                metadata={
                    "currency": currency,
                    "price": price,
                    "current_quantity": current_quantity,
                    "requested_quantity": target.quantity,
                    "approved_quantity": cash_limited_quantity,
                    "position_limited_quantity": position_limited_quantity,
                    "exposure_limited_quantity": exposure_limited_quantity,
                    "cash_limited_quantity": cash_limited_quantity,
                    "max_position_pct": self.max_position_pct_by_currency.get(currency),
                    "max_total_exposure_pct": max_total_exposure_pct_by_currency.get(currency),
                    "base_max_total_exposure_pct": self.max_total_exposure_pct_by_currency.get(currency),
                    "cash_buffer_pct": self.cash_buffer_pct_by_currency.get(currency),
                    "available_cash_after": remaining_cash,
                    "market_regime": regime,
                },
            ),
            remaining_cash,
        )

    def _symbol_guard_decision(
        self,
        *,
        context: RiskManagementContext,
        target: PortfolioTarget,
        price: float,
        current_quantity: int,
        regime: dict[str, object],
    ) -> RiskDecision | None:
        if not self.symbol_guard_enabled:
            return None
        symbol_key = target.symbol.key.upper()
        if symbol_key in self.symbol_guard_exempt_symbols:
            return None
        metrics = self._symbol_guard_metrics(context, target, price, current_quantity)
        if current_quantity > 0:
            exit_reason = self._symbol_guard_exit_reason(metrics)
            if exit_reason is not None:
                approved = PortfolioTarget(symbol=target.symbol, quantity=0, tag=_risk_tag(target.tag, "symbol_guard_exit"))
                return RiskDecision(
                    original_target=target,
                    approved_target=approved,
                    status=RiskDecisionStatus.CLAMPED if target.quantity != 0 else RiskDecisionStatus.APPROVED,
                    reason="symbol_guard_exit",
                    metadata={
                        **metrics,
                        "trigger": exit_reason,
                        "market_regime": regime,
                    },
                )
            reduce_reason = self._symbol_guard_reduce_reason(metrics)
            if reduce_reason is not None:
                anchor_quantity, already_reduced = self._symbol_guard_reduce_anchor_quantity(
                    context,
                    target.symbol.key,
                    current_quantity=current_quantity,
                    target_quantity=target.quantity,
                )
                if anchor_quantity is None:
                    reduced_quantity = current_quantity
                else:
                    reduced_quantity = max(0, int(anchor_quantity * self.symbol_reduce_fraction))
                reduced_quantity = min(current_quantity, reduced_quantity)
                approved_quantity = min(target.quantity, reduced_quantity)
                if approved_quantity < target.quantity:
                    approved = PortfolioTarget(
                        symbol=target.symbol,
                        quantity=approved_quantity,
                        tag=_risk_tag(target.tag, "symbol_guard_reduce_half"),
                    )
                    return RiskDecision(
                        original_target=target,
                        approved_target=approved,
                        status=RiskDecisionStatus.CLAMPED,
                        reason="symbol_guard_reduce_half",
                        metadata={
                            **metrics,
                            "trigger": reduce_reason,
                            "reduce_fraction": self.symbol_reduce_fraction,
                            "anchor_quantity": anchor_quantity,
                            "reduced_quantity": reduced_quantity,
                            "already_reduced": already_reduced,
                            "market_regime": regime,
                        },
                    )
        if target.quantity > current_quantity:
            hard_block_reason = self._symbol_guard_entry_hard_block_reason(metrics, has_position=current_quantity > 0)
            if hard_block_reason is not None:
                approved = PortfolioTarget(symbol=target.symbol, quantity=current_quantity, tag=target.tag)
                return RiskDecision(
                    original_target=target,
                    approved_target=approved,
                    status=RiskDecisionStatus.CLAMPED,
                    reason="symbol_guard_entry_block",
                    metadata={
                        **metrics,
                        "trigger": hard_block_reason,
                        "market_regime": regime,
                    },
                )
            drawdown_reason = self._symbol_guard_entry_drawdown_reason(metrics)
            if drawdown_reason is not None:
                pullback_quantity = self._symbol_guard_pullback_add_quantity(
                    metrics,
                    current_quantity=current_quantity,
                    target_quantity=target.quantity,
                )
                if pullback_quantity is not None and pullback_quantity > current_quantity:
                    approved = PortfolioTarget(symbol=target.symbol, quantity=pullback_quantity, tag=target.tag)
                    return RiskDecision(
                        original_target=target,
                        approved_target=approved,
                        status=RiskDecisionStatus.CLAMPED if pullback_quantity < target.quantity else RiskDecisionStatus.APPROVED,
                        reason="symbol_guard_pullback_add",
                        metadata={
                            **metrics,
                            "trigger": drawdown_reason,
                            "pullback_add_fraction": self.symbol_pullback_add_fraction,
                            "pullback_add_quantity": pullback_quantity,
                            "market_regime": regime,
                        },
                    )
                approved = PortfolioTarget(symbol=target.symbol, quantity=current_quantity, tag=target.tag)
                return RiskDecision(
                    original_target=target,
                    approved_target=approved,
                    status=RiskDecisionStatus.CLAMPED,
                    reason="symbol_guard_entry_block",
                    metadata={
                        **metrics,
                        "trigger": drawdown_reason,
                        "market_regime": regime,
                    },
                )
        return None

    def _symbol_guard_reduce_anchor_quantity(
        self,
        context: RiskManagementContext,
        symbol_key: str,
        *,
        current_quantity: int,
        target_quantity: int,
    ) -> tuple[int | None, bool]:
        state = self._symbol_guard_state(context, symbol_key)
        if str(state.get("status") or "") == "reduced":
            anchor = _safe_int(state.get("anchor_quantity"))
            if anchor is not None and anchor > 0:
                return max(anchor, current_quantity), True
        target_half = max(0, int(target_quantity * self.symbol_reduce_fraction))
        if target_quantity > current_quantity and current_quantity <= target_half:
            return None, True
        return current_quantity, False

    def _symbol_guard_state(self, context: RiskManagementContext, symbol_key: str) -> dict[str, object]:
        record = context.model_state.get(
            sleeve_id=context.sleeve_id,
            model_id=MODEL_ID,
            namespace=SYMBOL_GUARD_NAMESPACE,
            symbol_key=str(symbol_key).upper(),
        )
        if record is not None and isinstance(record.value, Mapping):
            return dict(record.value)
        return {}

    def _symbol_guard_metrics(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        price: float,
        current_quantity: int,
    ) -> dict[str, object]:
        bar = context.data.get(target.symbol)
        open_price = _safe_float(getattr(bar, "open", None)) if bar is not None else None
        high_price = _safe_float(getattr(bar, "high", None)) if bar is not None else None
        low_price = _safe_float(getattr(bar, "low", None)) if bar is not None else None
        holding = context.portfolio.holdings.get(target.symbol.key)
        average_price = _safe_float(getattr(holding, "average_price", None)) if holding is not None else None
        signal_metadata = self._symbol_signal_metadata(context, target.symbol.key)
        sma10 = _first_metadata_float(signal_metadata, "sma10", "fast_average")
        sma20 = _first_metadata_float(signal_metadata, "sma20", "slow_average")
        base_thresholds, thresholds, volatility_pct, volatility_multiplier, entry_volatility_multiplier = (
            self._symbol_guard_thresholds(signal_metadata, price)
        )
        intraday_return = (price / open_price) - 1.0 if open_price and open_price > 0 else None
        drawdown_from_session_high = (price / high_price) - 1.0 if high_price and high_price > 0 else None
        unrealized_pnl_pct = (
            (price / average_price) - 1.0
            if current_quantity > 0 and average_price and average_price > 0
            else None
        )
        sma10_gap = (price / sma10) - 1.0 if sma10 and sma10 > 0 else None
        sma20_gap = (price / sma20) - 1.0 if sma20 and sma20 > 0 else None
        return {
            "symbol_guard_enabled": True,
            "price": price,
            "current_quantity": current_quantity,
            "requested_quantity": target.quantity,
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "average_price": average_price,
            "intraday_return": intraday_return,
            "drawdown_from_session_high": drawdown_from_session_high,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "sma10": sma10,
            "sma20": sma20,
            "sma10_gap": sma10_gap,
            "sma20_gap": sma20_gap,
            "volatility_pct": volatility_pct,
            "volatility_multiplier": volatility_multiplier,
            "entry_volatility_multiplier": entry_volatility_multiplier,
            "volatility_adjusted": bool(self.symbol_guard_volatility_adjusted_enabled and volatility_pct is not None),
            "base_thresholds": base_thresholds,
            "thresholds": thresholds,
            "signal_alpha_ids": sorted(signal_metadata.get("alpha_ids", ())),
        }

    def _symbol_guard_thresholds(
        self,
        signal_metadata: Mapping[str, object],
        price: float,
    ) -> tuple[dict[str, float], dict[str, float], float | None, float, float]:
        base_thresholds = {
            "entry_block_intraday_return_pct": self.symbol_entry_block_intraday_return_pct,
            "entry_block_high_drawdown_pct": self.symbol_entry_block_high_drawdown_pct,
            "entry_block_unrealized_loss_pct": self.symbol_entry_block_unrealized_loss_pct,
            "reduce_half_unrealized_loss_pct": self.symbol_reduce_half_unrealized_loss_pct,
            "exit_unrealized_loss_pct": self.symbol_exit_unrealized_loss_pct,
            "reduce_half_high_drawdown_pct": self.symbol_reduce_half_high_drawdown_pct,
            "exit_high_drawdown_pct": self.symbol_exit_high_drawdown_pct,
            "reduce_half_sma10_buffer_pct": self.symbol_reduce_half_sma10_buffer_pct,
            "exit_sma20_buffer_pct": self.symbol_exit_sma20_buffer_pct,
        }
        if self.symbol_entry_block_sma10_buffer_pct is not None:
            base_thresholds["entry_block_sma10_buffer_pct"] = self.symbol_entry_block_sma10_buffer_pct
        if self.symbol_entry_block_sma20_buffer_pct is not None:
            base_thresholds["entry_block_sma20_buffer_pct"] = self.symbol_entry_block_sma20_buffer_pct
        thresholds = dict(base_thresholds)
        volatility_pct = self._symbol_guard_volatility_pct(signal_metadata, price)
        volatility_multiplier = self._symbol_guard_volatility_multiplier(volatility_pct)
        entry_volatility_multiplier = min(
            volatility_multiplier,
            self.symbol_guard_entry_max_volatility_multiplier,
        )
        if self.symbol_guard_volatility_adjusted_enabled and volatility_pct is not None:
            for name in (
                "reduce_half_unrealized_loss_pct",
                "exit_unrealized_loss_pct",
                "reduce_half_high_drawdown_pct",
                "exit_high_drawdown_pct",
            ):
                thresholds[name] = _scale_loss_threshold(base_thresholds[name], volatility_multiplier)
            for name in (
                "entry_block_intraday_return_pct",
                "entry_block_high_drawdown_pct",
                "entry_block_unrealized_loss_pct",
            ):
                thresholds[name] = _scale_loss_threshold(base_thresholds[name], entry_volatility_multiplier)
        return (
            base_thresholds,
            thresholds,
            volatility_pct,
            volatility_multiplier,
            entry_volatility_multiplier,
        )

    def _symbol_guard_volatility_pct(
        self,
        signal_metadata: Mapping[str, object],
        price: float,
    ) -> float | None:
        ratio = _first_metadata_float(
            signal_metadata,
            "volatility",
            "normalized_volatility",
            "realized_volatility_20",
            "realized_vol_20d",
            "atr14_pct",
            "atr_pct",
        )
        if ratio is not None and 0.0 < ratio < 1.0:
            return ratio
        absolute = _first_metadata_float(signal_metadata, "atr_14", "atr14", "stddev_20_close")
        if absolute is not None and price > 0:
            normalized = absolute / price
            if 0.0 < normalized < 1.0:
                return normalized
        return None

    def _symbol_guard_volatility_multiplier(self, volatility_pct: float | None) -> float:
        if volatility_pct is None:
            return 1.0
        raw = volatility_pct / self.symbol_guard_reference_volatility_pct
        return min(
            self.symbol_guard_max_volatility_multiplier,
            max(self.symbol_guard_min_volatility_multiplier, raw),
        )

    def _symbol_signal_metadata(self, context: RiskManagementContext, symbol_key: str) -> dict[str, object]:
        result: dict[str, object] = {"alpha_ids": set()}
        matches = [
            insight
            for insight in context.active_insights
            if getattr(getattr(insight, "symbol", None), "key", "") == symbol_key
        ]
        matches.sort(key=lambda insight: getattr(insight, "generated_at", context.data.time))
        for insight in matches:
            metadata = getattr(insight, "metadata", {}) or {}
            if isinstance(metadata, Mapping):
                result.update(dict(metadata))
            alpha_id = str(getattr(insight, "alpha_id", "") or "").strip()
            if alpha_id:
                result["alpha_ids"].add(alpha_id)
        return result

    def _symbol_guard_exit_reason(self, metrics: dict[str, object]) -> str | None:
        unrealized = _safe_float(metrics.get("unrealized_pnl_pct"))
        drawdown = _safe_float(metrics.get("drawdown_from_session_high"))
        sma20_gap = _safe_float(metrics.get("sma20_gap"))
        exit_unrealized = self._symbol_guard_threshold(
            metrics,
            "exit_unrealized_loss_pct",
            self.symbol_exit_unrealized_loss_pct,
        )
        exit_drawdown = self._symbol_guard_threshold(
            metrics,
            "exit_high_drawdown_pct",
            self.symbol_exit_high_drawdown_pct,
        )
        exit_sma20 = self._symbol_guard_threshold(
            metrics,
            "exit_sma20_buffer_pct",
            self.symbol_exit_sma20_buffer_pct,
        )
        if unrealized is not None and unrealized <= exit_unrealized:
            return "unrealized_loss"
        if drawdown is not None and drawdown <= exit_drawdown:
            return "session_high_drawdown"
        if sma20_gap is not None and sma20_gap <= exit_sma20:
            return "sma20_break"
        return None

    def _symbol_guard_reduce_reason(self, metrics: dict[str, object]) -> str | None:
        unrealized = _safe_float(metrics.get("unrealized_pnl_pct"))
        drawdown = _safe_float(metrics.get("drawdown_from_session_high"))
        sma10_gap = _safe_float(metrics.get("sma10_gap"))
        reduce_unrealized = self._symbol_guard_threshold(
            metrics,
            "reduce_half_unrealized_loss_pct",
            self.symbol_reduce_half_unrealized_loss_pct,
        )
        reduce_drawdown = self._symbol_guard_threshold(
            metrics,
            "reduce_half_high_drawdown_pct",
            self.symbol_reduce_half_high_drawdown_pct,
        )
        reduce_sma10 = self._symbol_guard_threshold(
            metrics,
            "reduce_half_sma10_buffer_pct",
            self.symbol_reduce_half_sma10_buffer_pct,
        )
        if unrealized is not None and unrealized <= reduce_unrealized:
            return "unrealized_loss"
        if drawdown is not None and drawdown <= reduce_drawdown:
            return "session_high_drawdown"
        if sma10_gap is not None and sma10_gap <= reduce_sma10:
            return "sma10_break"
        return None

    def _symbol_guard_entry_block_reason(
        self,
        metrics: dict[str, object],
        *,
        has_position: bool,
    ) -> str | None:
        hard_block = self._symbol_guard_entry_hard_block_reason(metrics, has_position=has_position)
        if hard_block is not None:
            return hard_block
        return self._symbol_guard_entry_drawdown_reason(metrics)

    def _symbol_guard_entry_hard_block_reason(
        self,
        metrics: dict[str, object],
        *,
        has_position: bool,
    ) -> str | None:
        intraday_return = _safe_float(metrics.get("intraday_return"))
        unrealized = _safe_float(metrics.get("unrealized_pnl_pct"))
        sma10_gap = _safe_float(metrics.get("sma10_gap"))
        sma20_gap = _safe_float(metrics.get("sma20_gap"))
        entry_intraday = self._symbol_guard_threshold(
            metrics,
            "entry_block_intraday_return_pct",
            self.symbol_entry_block_intraday_return_pct,
        )
        entry_unrealized = self._symbol_guard_threshold(
            metrics,
            "entry_block_unrealized_loss_pct",
            self.symbol_entry_block_unrealized_loss_pct,
        )
        thresholds = metrics.get("thresholds")
        entry_sma10 = (
            _safe_float(thresholds.get("entry_block_sma10_buffer_pct"))
            if isinstance(thresholds, Mapping)
            else self.symbol_entry_block_sma10_buffer_pct
        )
        entry_sma20 = (
            _safe_float(thresholds.get("entry_block_sma20_buffer_pct"))
            if isinstance(thresholds, Mapping)
            else self.symbol_entry_block_sma20_buffer_pct
        )
        if intraday_return is not None and intraday_return <= entry_intraday:
            return "intraday_return"
        if (
            has_position
            and unrealized is not None
            and unrealized <= entry_unrealized
        ):
            return "unrealized_loss"
        if has_position and sma20_gap is not None and entry_sma20 is not None and sma20_gap <= entry_sma20:
            return "sma20_add_block"
        if has_position and sma10_gap is not None and entry_sma10 is not None and sma10_gap <= entry_sma10:
            return "sma10_add_block"
        return None

    def _symbol_guard_entry_drawdown_reason(self, metrics: dict[str, object]) -> str | None:
        drawdown = _safe_float(metrics.get("drawdown_from_session_high"))
        entry_drawdown = self._symbol_guard_threshold(
            metrics,
            "entry_block_high_drawdown_pct",
            self.symbol_entry_block_high_drawdown_pct,
        )
        if drawdown is not None and drawdown <= entry_drawdown:
            return "session_high_drawdown"
        return None

    def _symbol_guard_pullback_add_quantity(
        self,
        metrics: dict[str, object],
        *,
        current_quantity: int,
        target_quantity: int,
    ) -> int | None:
        if not self.symbol_pullback_add_enabled or current_quantity <= 0 or target_quantity <= current_quantity:
            return None
        intraday_return = _safe_float(metrics.get("intraday_return"))
        unrealized = _safe_float(metrics.get("unrealized_pnl_pct"))
        sma10_gap = _safe_float(metrics.get("sma10_gap"))
        sma20_gap = _safe_float(metrics.get("sma20_gap"))
        alpha_ids = metrics.get("signal_alpha_ids")
        alpha_count = len(alpha_ids) if isinstance(alpha_ids, (list, tuple, set, frozenset)) else 0
        if intraday_return is None or intraday_return < self.symbol_pullback_add_min_intraday_return_pct:
            return None
        if unrealized is None or unrealized < self.symbol_pullback_add_min_unrealized_pnl_pct:
            return None
        if sma10_gap is not None and sma10_gap < self.symbol_pullback_add_min_sma10_gap_pct:
            return None
        if sma20_gap is not None and sma20_gap < self.symbol_pullback_add_min_sma20_gap_pct:
            return None
        if alpha_count < self.symbol_pullback_add_min_alpha_count:
            return None
        delta = target_quantity - current_quantity
        add_quantity = max(1, ceil(delta * self.symbol_pullback_add_fraction))
        return min(target_quantity, current_quantity + add_quantity)

    def _symbol_guard_threshold(self, metrics: dict[str, object], name: str, fallback: float) -> float:
        thresholds = metrics.get("thresholds")
        if isinstance(thresholds, Mapping):
            value = _safe_float(thresholds.get(name))
            if value is not None:
                return value
        return fallback

    def _clamp_position(self, context: RiskManagementContext, target: PortfolioTarget, price: float) -> int:
        currency = currency_for_symbol(target.symbol)
        equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
        if equity <= 0:
            return 0
        max_pct = self.max_position_pct_by_currency.get(currency, 0.35)
        max_abs_quantity = int((equity * max_pct) // price)
        if abs(target.quantity) <= max_abs_quantity:
            return target.quantity
        return max_abs_quantity if target.quantity > 0 else -max_abs_quantity

    def _clamp_total_exposure(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        target_quantity: int,
        price: float,
        approved_quantities: dict[str, int],
        approved_symbols: dict[str, object],
        max_total_exposure_pct_by_currency: dict[str, float],
        regime: dict[str, object],
    ) -> int:
        currency = currency_for_symbol(target.symbol)
        equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
        if equity <= 0:
            return 0
        currency_regime = _currency_regime(regime, currency)
        cap_source = str(currency_regime.get("source") or "")
        exempt_symbols = set()
        if cap_source == "intraday_market_guard":
            exempt_symbols = {
                str(symbol).strip().upper()
                for symbol in currency_regime.get("exempt_symbols", ())
            }
            if target.symbol.key.upper() in exempt_symbols:
                return target_quantity
        max_total_exposure = equity * max_total_exposure_pct_by_currency.get(currency, 0.80)
        exposure_without_target = 0.0
        for symbol_key, quantity in approved_quantities.items():
            if symbol_key == target.symbol.key:
                continue
            if str(symbol_key).upper() in exempt_symbols:
                continue
            symbol = approved_symbols.get(symbol_key)
            if symbol is None or currency_for_symbol(symbol) != currency:
                continue
            mark = context.portfolio.mark_price(symbol, context.data)
            if mark is None:
                continue
            exposure_without_target += abs(quantity * mark)
        allowed = max(0.0, max_total_exposure - exposure_without_target)
        max_abs_quantity = int(allowed // price)
        if abs(target_quantity) <= max_abs_quantity:
            return target_quantity
        return max_abs_quantity if target_quantity > 0 else -max_abs_quantity

    def _clamp_cash(
        self,
        *,
        current_quantity: int,
        target_quantity: int,
        price: float,
        available_cash: float,
    ) -> tuple[int, float]:
        delta = target_quantity - current_quantity
        if delta <= 0:
            return target_quantity, available_cash
        affordable_delta = int(available_cash // price)
        if affordable_delta >= delta:
            return target_quantity, available_cash - (delta * price)
        return current_quantity + affordable_delta, available_cash - (affordable_delta * price)

    def _regime_total_exposure_pct_by_currency(self, regime: dict[str, object]) -> dict[str, float]:
        result = dict(self.max_total_exposure_pct_by_currency)
        if not self.regime_exposure_enabled:
            return result
        regime_name = str(regime.get("name", "neutral"))
        for currency, table in self.regime_total_exposure_pct_by_currency.items():
            if regime_name in table:
                result[currency] = float(table[regime_name])
        by_currency = regime.get("by_currency", {})
        if isinstance(by_currency, dict):
            for currency, item in by_currency.items():
                if not isinstance(item, dict):
                    continue
                cap = _safe_float(item.get("max_total_exposure_pct"))
                if cap is not None:
                    code = str(currency).upper()
                    result[code] = min(result.get(code, cap), cap)
        return result

    def _apply_equity_overlay(
        self,
        context: RiskManagementContext,
        regime: dict[str, object],
        currencies: list[str],
        equity_by_currency: dict[str, float],
    ) -> dict[str, object]:
        if not self.regime_equity_overlay_enabled:
            return regime
        by_currency = dict(regime.get("by_currency", {}) if isinstance(regime.get("by_currency"), dict) else {})
        now = context.data.time
        for currency in currencies:
            code = str(currency).upper()
            equity = float(equity_by_currency.get(code, 0.0))
            if equity <= 0:
                continue
            prior = self._prior_equity_state(context, code)
            overlay = self._equity_overlay_for_currency(code, equity, prior, session_date=now.date().isoformat())
            overlay.update(
                {
                    "equity": equity,
                    "as_of": now.isoformat(),
                    "source": "equity_overlay",
                }
            )
            by_currency[code] = overlay
        result = dict(regime)
        result["by_currency"] = by_currency
        active = [item for item in by_currency.values() if isinstance(item, dict) and item.get("overlay") != NO_OVERLAY]
        if active:
            result["equity_overlay_active"] = True
            strongest = max(active, key=lambda item: _overlay_severity(str(item.get("overlay", NO_OVERLAY))))
            result["equity_overlay"] = strongest.get("overlay")
            result["entry_freeze"] = any(bool(item.get("entry_freeze", False)) for item in active)
        return result

    def _prior_equity_state(self, context: RiskManagementContext, currency: str) -> dict[str, object]:
        record = context.model_state.get(
            model_id=MODEL_ID,
            namespace=REGIME_EQUITY_NAMESPACE,
            symbol_key=currency,
        )
        return dict(record.value) if record is not None else {}

    def _equity_overlay_for_currency(
        self,
        currency: str,
        equity: float,
        prior: dict[str, object],
        *,
        session_date: str,
    ) -> dict[str, object]:
        last_equity = _safe_float(prior.get("last_equity"))
        is_new_session = str(prior.get("session_date") or "") != session_date
        if is_new_session:
            anchor_equity = last_equity if last_equity and last_equity > 0 else equity
            peak_equity = max(anchor_equity, equity)
            previous_trough = min(anchor_equity, equity)
            previous_overlay = NO_OVERLAY
            previous_recovery_count = 0
        else:
            peak_equity = max(_safe_float(prior.get("peak_equity")) or equity, equity)
            previous_trough = _safe_float(prior.get("trough_equity")) or equity
            previous_overlay = str(prior.get("overlay") or NO_OVERLAY)
            previous_recovery_count = int(_safe_float(prior.get("recovery_count")) or 0)
        cycle_return = ((equity / last_equity) - 1.0) if last_equity and last_equity > 0 else 0.0
        drawdown = (equity / peak_equity) - 1.0 if peak_equity > 0 else 0.0
        trough_equity = min(previous_trough, equity) if previous_overlay != NO_OVERLAY else equity
        recovery_from_trough = (equity / trough_equity) - 1.0 if trough_equity > 0 else 0.0

        overlay = self._raw_equity_overlay(currency, cycle_return=cycle_return, drawdown=drawdown)
        recovery_count = 0
        if previous_overlay != NO_OVERLAY:
            recovery_threshold = self.recovery_from_trough_pct_by_currency.get(currency, 0.006)
            cycle_overlay = self._raw_equity_overlay(currency, cycle_return=cycle_return, drawdown=0.0)
            if cycle_overlay != NO_OVERLAY:
                overlay = _stronger_overlay(previous_overlay, cycle_overlay)
                recovery_count = 0
            elif recovery_from_trough >= recovery_threshold and cycle_return >= 0.0:
                recovery_count = previous_recovery_count + 1
                if recovery_count < self.recovery_confirmation_cycles:
                    overlay = _stronger_overlay(previous_overlay, overlay)
                elif previous_overlay == INTRADAY_RISK_OFF:
                    overlay = ENTRY_FREEZE
                else:
                    overlay = NO_OVERLAY
            else:
                overlay = _stronger_overlay(previous_overlay, overlay)
                recovery_count = 0
        if overlay == NO_OVERLAY and previous_overlay != NO_OVERLAY:
            peak_equity = equity
            trough_equity = equity

        max_total_exposure_pct = None
        if overlay == ENTRY_FREEZE:
            max_total_exposure_pct = self.entry_freeze_cap_pct_by_currency.get(currency)
        elif overlay == INTRADAY_RISK_OFF:
            max_total_exposure_pct = self.risk_off_cap_pct_by_currency.get(currency)

        return {
            "overlay": overlay,
            "entry_freeze": overlay in {ENTRY_FREEZE, INTRADAY_RISK_OFF},
            "max_total_exposure_pct": max_total_exposure_pct,
            "cycle_return": cycle_return,
            "drawdown_from_peak": drawdown,
            "peak_equity": peak_equity,
            "trough_equity": trough_equity,
            "session_date": session_date,
            "recovery_from_trough": recovery_from_trough,
            "recovery_count": recovery_count,
            "trigger": _overlay_trigger(overlay),
        }

    def _raw_equity_overlay(self, currency: str, *, cycle_return: float, drawdown: float) -> str:
        risk_off_drawdown = self.risk_off_drawdown_pct_by_currency.get(currency, 0.055)
        entry_drawdown = self.entry_freeze_drawdown_pct_by_currency.get(currency, 0.035)
        risk_off_cycle = self.risk_off_cycle_loss_pct_by_currency.get(currency, 0.050)
        entry_cycle = self.entry_freeze_cycle_loss_pct_by_currency.get(currency, 0.025)
        if drawdown <= -risk_off_drawdown or cycle_return <= -risk_off_cycle:
            return INTRADAY_RISK_OFF
        if drawdown <= -entry_drawdown or cycle_return <= -entry_cycle:
            return ENTRY_FREEZE
        return NO_OVERLAY

    def _equity_overlay_state_patches(
        self,
        context: RiskManagementContext,
        regime: dict[str, object],
    ) -> tuple[StatePatch, ...]:
        if not self.regime_equity_overlay_enabled:
            return ()
        by_currency = regime.get("by_currency")
        if not isinstance(by_currency, dict):
            return ()
        patches: list[StatePatch] = []
        for currency, item in by_currency.items():
            if not isinstance(item, dict):
                continue
            code = str(currency).upper()
            patches.append(
                StatePatch(
                    key=context.model_state.key(
                        sleeve_id=context.sleeve_id,
                        model_id=MODEL_ID,
                        namespace=REGIME_EQUITY_NAMESPACE,
                        symbol_key=code,
                    ),
                    value={
                        "last_equity": item.get("equity"),
                        "peak_equity": item.get("peak_equity"),
                        "trough_equity": item.get("trough_equity"),
                        "session_date": item.get("session_date"),
                        "overlay": item.get("overlay", NO_OVERLAY),
                        "recovery_count": item.get("recovery_count", 0),
                        "last_cycle_return": item.get("cycle_return", 0.0),
                        "last_drawdown_from_peak": item.get("drawdown_from_peak", 0.0),
                        "updated_at": context.data.time.isoformat(),
                    },
                    reason="regime_equity_overlay_mark",
                    generated_at=context.data.time,
                )
            )
        return tuple(patches)

    def _state_patches(
        self,
        context: RiskManagementContext,
        regime: dict[str, object],
        *,
        decisions: tuple[RiskDecision, ...] = (),
    ) -> tuple[StatePatch, ...]:
        return (
            *self._equity_overlay_state_patches(context, regime),
            *self._intraday_guard_state_patches(context, regime),
            *self._symbol_guard_state_patches(context, decisions),
        )

    def _symbol_guard_state_patches(
        self,
        context: RiskManagementContext,
        decisions: tuple[RiskDecision, ...],
    ) -> tuple[StatePatch, ...]:
        if not self.symbol_guard_enabled:
            return ()
        patches: list[StatePatch] = []
        for decision in decisions:
            symbol_key = decision.original_target.symbol.key.upper()
            if symbol_key in self.symbol_guard_exempt_symbols:
                continue
            metadata = dict(decision.metadata)
            if decision.reason == "symbol_guard_reduce_half":
                anchor_quantity = _safe_int(metadata.get("anchor_quantity"))
                if anchor_quantity is None:
                    anchor_quantity = _safe_int(self._symbol_guard_state(context, symbol_key).get("anchor_quantity"))
                if anchor_quantity is None:
                    anchor_quantity = _safe_int(metadata.get("current_quantity"))
                patches.append(
                    StatePatch(
                        key=context.model_state.key(
                            sleeve_id=context.sleeve_id,
                            model_id=MODEL_ID,
                            namespace=SYMBOL_GUARD_NAMESPACE,
                            symbol_key=symbol_key,
                        ),
                        value={
                            "status": "reduced",
                            "anchor_quantity": anchor_quantity,
                            "last_approved_quantity": decision.approved_target.quantity if decision.approved_target else None,
                            "last_current_quantity": metadata.get("current_quantity"),
                            "trigger": metadata.get("trigger"),
                            "last_risk_status": "reduced",
                            "last_risk_trigger": metadata.get("trigger"),
                            "last_risk_event_at": context.data.time.isoformat(),
                            "updated_at": context.data.time.isoformat(),
                        },
                        reason="symbol_guard_reduce_mark",
                        generated_at=context.data.time,
                    )
                )
                continue
            if decision.reason == "symbol_guard_exit":
                patches.append(
                    StatePatch(
                        key=context.model_state.key(
                            sleeve_id=context.sleeve_id,
                            model_id=MODEL_ID,
                            namespace=SYMBOL_GUARD_NAMESPACE,
                            symbol_key=symbol_key,
                        ),
                        value={
                            "status": "exited",
                            "last_approved_quantity": 0,
                            "last_current_quantity": metadata.get("current_quantity"),
                            "trigger": metadata.get("trigger"),
                            "last_risk_status": "exited",
                            "last_risk_trigger": metadata.get("trigger"),
                            "last_risk_event_at": context.data.time.isoformat(),
                            "updated_at": context.data.time.isoformat(),
                        },
                        reason="symbol_guard_exit_mark",
                        generated_at=context.data.time,
                    )
                )
                continue
            prior = self._symbol_guard_state(context, symbol_key)
            if prior and str(prior.get("status") or "") in {"reduced", "exited", "recovering"}:
                confirmation_count = (_safe_int(prior.get("recovery_confirmation_count")) or 0) + 1
                status = "clear"
                reason = "symbol_guard_clear"
                if confirmation_count < self.symbol_guard_recovery_confirmation_cycles:
                    status = "recovering"
                    reason = "symbol_guard_recovery_wait"
                patches.append(
                    StatePatch(
                        key=context.model_state.key(
                            sleeve_id=context.sleeve_id,
                            model_id=MODEL_ID,
                            namespace=SYMBOL_GUARD_NAMESPACE,
                            symbol_key=symbol_key,
                        ),
                        value={
                            "status": status,
                            "anchor_quantity": None,
                            "last_approved_quantity": decision.approved_target.quantity if decision.approved_target else None,
                            "last_current_quantity": metadata.get("current_quantity"),
                            "trigger": None,
                            "last_risk_status": prior.get("last_risk_status") or prior.get("status"),
                            "last_risk_trigger": prior.get("last_risk_trigger") or prior.get("trigger"),
                            "last_risk_event_at": prior.get("last_risk_event_at") or prior.get("updated_at"),
                            "recovery_confirmation_count": confirmation_count,
                            "recovery_confirmation_required": self.symbol_guard_recovery_confirmation_cycles,
                            "updated_at": context.data.time.isoformat(),
                        },
                        reason=reason,
                        generated_at=context.data.time,
                    )
                )
        return tuple(patches)

    def _intraday_guard_state_patches(
        self,
        context: RiskManagementContext,
        regime: dict[str, object],
    ) -> tuple[StatePatch, ...]:
        state = regime.get("intraday_guard_state")
        if not isinstance(state, dict):
            return ()
        return (
            StatePatch(
                key=context.model_state.key(
                    sleeve_id=context.sleeve_id,
                    model_id=MODEL_ID,
                    namespace=INTRADAY_GUARD_NAMESPACE,
                    symbol_key=self.intraday_guard_symbol,
                ),
                value={
                    "guard_symbol": self.intraday_guard_symbol,
                    "session_date": state.get("session_date"),
                    "session_high_price": state.get("session_high_price"),
                    "session_low_price": state.get("session_low_price"),
                    "current_price": state.get("current_price"),
                    "reference_price": state.get("reference_price"),
                    "reference_return": state.get("reference_return"),
                    "drawdown_from_session_high": state.get("drawdown_from_session_high"),
                    "recovery_from_session_low": state.get("recovery_from_session_low"),
                    "cycle_return": state.get("cycle_return"),
                    "recovery_count": state.get("recovery_count", 0),
                    "recovery_ready": state.get("recovery_ready", False),
                    "recovery_release_active": state.get("recovery_release_active", False),
                    "recovery_release_cap_pct": state.get("recovery_release_cap_pct"),
                    "underlying_trigger": state.get("underlying_trigger"),
                    "overlay": state.get("overlay", NO_OVERLAY),
                    "trigger": state.get("trigger"),
                    "updated_at": context.data.time.isoformat(),
                },
                reason="intraday_guard_mark",
                generated_at=context.data.time,
            ),
        )

    def _market_regime(self, context: RiskManagementContext) -> dict[str, object]:
        up_insights = [
            insight
            for insight in context.active_insights
            if getattr(insight, "alpha_id", "") == "leaps-kospi-conviction"
            and getattr(getattr(insight, "direction", None), "value", "") == "up"
        ]
        stop_count = sum(
            1
            for insight in context.active_insights
            if getattr(insight, "alpha_id", "") == "leaps-volatility-trailing-stop"
        )
        if not up_insights:
            regime = {
                "name": "risk_off" if stop_count else "neutral",
                "market_breadth": 0.0,
                "average_momentum": 0.0,
                "average_volatility": 0.0,
                "stop_pressure": stop_count,
                "source": "active_insights",
            }
            return self._apply_intraday_market_guard(context, regime)

        breadth_values = []
        momentum_values = []
        volatility_values = []
        for insight in up_insights:
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
        average_momentum = _average(momentum_values)
        average_volatility = _average(volatility_values)

        if stop_count >= 3 or breadth < 0.25 or average_volatility >= 0.18:
            name = "risk_off"
            trigger = "stop_pressure_or_weak_breadth_or_high_volatility"
        elif breadth >= 0.55 and average_momentum >= 0.18 and average_volatility <= 0.14:
            name = "strong_risk_on"
            trigger = "broad_strong_momentum"
        elif breadth >= 0.45 and average_momentum >= 0.08 and average_volatility <= 0.16:
            name = "risk_on"
            trigger = "broad_positive_momentum"
        elif (
            stop_count == 0
            and breadth >= 0.30
            and average_momentum >= 0.25
            and average_volatility <= 0.13
        ):
            name = "risk_on"
            trigger = "narrow_leadership_strong_momentum"
        else:
            name = "neutral"
            trigger = "mixed_or_insufficient_confirmation"
        regime = {
            "name": name,
            "market_breadth": breadth,
            "average_momentum": average_momentum,
            "average_volatility": average_volatility,
            "stop_pressure": stop_count,
            "trigger": trigger,
            "source": "active_insights",
        }
        return self._apply_intraday_market_guard(context, regime)

    def _apply_intraday_market_guard(
        self,
        context: RiskManagementContext,
        regime: dict[str, object],
    ) -> dict[str, object]:
        if not self.intraday_market_guard_enabled:
            return regime
        guard_bar = context.data.bars.get(self.intraday_guard_symbol)
        if guard_bar is None or guard_bar.close <= 0:
            return regime
        reference = self._intraday_guard_reference_price(context)
        guard_state = self._intraday_guard_state(
            context,
            float(guard_bar.close),
            reference,
            high_price=_safe_float(getattr(guard_bar, "high", None)),
            low_price=_safe_float(getattr(guard_bar, "low", None)),
        )
        if reference is None or reference <= 0:
            result = dict(regime)
            result["intraday_guard_state"] = guard_state
            return result
        intraday_return = (float(guard_bar.close) / reference) - 1.0
        guard_state["reference_return"] = intraday_return
        if self.intraday_guard_smoothing_enabled:
            return self._apply_smooth_intraday_market_guard(
                context=context,
                regime=regime,
                guard_state=guard_state,
                intraday_return=intraday_return,
                reference=reference,
                current_price=float(guard_bar.close),
            )
        if intraday_return <= self.intraday_risk_off_return_pct:
            overlay = INTRADAY_RISK_OFF
            cap_table = self.intraday_risk_off_cap_pct_by_currency
            trigger = "reference_return"
        elif (
            self.intraday_guard_high_drawdown_enabled
            and guard_state["drawdown_from_session_high"] <= self.intraday_guard_high_risk_off_return_pct
        ):
            overlay = INTRADAY_RISK_OFF
            cap_table = self.intraday_risk_off_cap_pct_by_currency
            trigger = "session_high_drawdown"
        elif self._intraday_open_entry_gate_active(context):
            overlay = ENTRY_FREEZE
            cap_table = self.intraday_entry_freeze_cap_pct_by_currency
            trigger = "opening_entry_gate"
        elif intraday_return <= self.intraday_entry_freeze_return_pct:
            overlay = ENTRY_FREEZE
            cap_table = self.intraday_entry_freeze_cap_pct_by_currency
            trigger = "reference_return"
        elif (
            self.intraday_guard_high_drawdown_enabled
            and guard_state["drawdown_from_session_high"] <= self.intraday_guard_high_entry_freeze_return_pct
        ):
            overlay = ENTRY_FREEZE
            cap_table = self.intraday_entry_freeze_cap_pct_by_currency
            trigger = "session_high_drawdown"
        else:
            result = dict(regime)
            guard_state["overlay"] = NO_OVERLAY
            result["intraday_guard_state"] = guard_state
            return result

        by_currency = dict(regime.get("by_currency", {}) if isinstance(regime.get("by_currency"), dict) else {})
        guard_state["overlay"] = overlay
        guard_state["trigger"] = trigger
        cap = cap_table.get("KRW")
        cap, trigger, overlay, recovery_metadata = self._intraday_recovery_adjusted_cap(
            context=context,
            currency="KRW",
            guard_state=guard_state,
            base_cap=self._base_regime_cap_pct("KRW", regime),
            current_cap=cap,
            overlay=overlay,
            trigger=trigger,
        )
        guard_state.update(recovery_metadata)
        guard_state["overlay"] = overlay
        guard_state["trigger"] = trigger
        by_currency["KRW"] = {
            "overlay": overlay,
            "entry_freeze": True,
            "max_total_exposure_pct": cap,
            "source": "intraday_market_guard",
            "guard_symbol": self.intraday_guard_symbol,
            "reference_price": reference,
            "current_price": float(guard_bar.close),
            "intraday_return": intraday_return,
            "drawdown_from_session_high": guard_state["drawdown_from_session_high"],
            "session_high_price": guard_state["session_high_price"],
            "session_low_price": guard_state.get("session_low_price"),
            "recovery_from_session_low": guard_state.get("recovery_from_session_low"),
            "recovery_count": guard_state.get("recovery_count", 0),
            "recovery_release_active": guard_state.get("recovery_release_active", False),
            "trigger": trigger,
            "exempt_symbols": sorted(self.intraday_guard_exempt_symbols),
        }
        result = dict(regime)
        result["by_currency"] = by_currency
        result["intraday_guard_state"] = guard_state
        result["intraday_market_guard_active"] = True
        result["equity_overlay_active"] = True
        result["equity_overlay"] = overlay
        result["entry_freeze"] = True
        return result

    def _apply_smooth_intraday_market_guard(
        self,
        *,
        context: RiskManagementContext,
        regime: dict[str, object],
        guard_state: dict[str, object],
        intraday_return: float,
        reference: float,
        current_price: float,
    ) -> dict[str, object]:
        currency = "KRW"
        base_cap = self._base_regime_cap_pct(currency, regime)
        entry_cap = min(base_cap, self.intraday_entry_freeze_cap_pct_by_currency.get(currency, base_cap))
        risk_cap = min(entry_cap, self.intraday_risk_off_cap_pct_by_currency.get(currency, entry_cap))
        candidates: list[tuple[float, str, str, float]] = []
        reference_cap, reference_severity = _smooth_intraday_cap(
            value=intraday_return,
            entry_threshold=self.intraday_entry_freeze_return_pct,
            risk_threshold=self.intraday_risk_off_return_pct,
            base_cap=base_cap,
            entry_cap=entry_cap,
            risk_cap=risk_cap,
            curve=self.intraday_guard_cap_curve,
        )
        if reference_cap < base_cap:
            reference_overlay = INTRADAY_RISK_OFF if intraday_return <= self.intraday_risk_off_return_pct else ENTRY_FREEZE
            candidates.append((reference_cap, "reference_return", reference_overlay, reference_severity))
        if self.intraday_guard_high_drawdown_enabled:
            drawdown_cap, drawdown_severity = _smooth_intraday_cap(
                value=float(guard_state["drawdown_from_session_high"]),
                entry_threshold=self.intraday_guard_high_entry_freeze_return_pct,
                risk_threshold=self.intraday_guard_high_risk_off_return_pct,
                base_cap=base_cap,
                entry_cap=entry_cap,
                risk_cap=risk_cap,
                curve=self.intraday_guard_cap_curve,
            )
            if drawdown_cap < base_cap:
                drawdown_overlay = (
                    INTRADAY_RISK_OFF
                    if float(guard_state["drawdown_from_session_high"]) <= self.intraday_guard_high_risk_off_return_pct
                    else ENTRY_FREEZE
                )
                candidates.append((drawdown_cap, "session_high_drawdown", drawdown_overlay, drawdown_severity))
        if self._intraday_open_entry_gate_active(context):
            candidates.append((entry_cap, "opening_entry_gate", ENTRY_FREEZE, 0.5))
        if not candidates:
            result = dict(regime)
            guard_state["overlay"] = NO_OVERLAY
            guard_state["trigger"] = None
            guard_state["smoothed_cap_pct"] = base_cap
            guard_state["smoothing_severity"] = 0.0
            result["intraday_guard_state"] = guard_state
            return result

        cap, trigger, overlay, severity = min(candidates, key=lambda item: item[0])
        cap, trigger, overlay, recovery_metadata = self._intraday_recovery_adjusted_cap(
            context=context,
            currency=currency,
            guard_state=guard_state,
            base_cap=base_cap,
            current_cap=cap,
            overlay=overlay,
            trigger=trigger,
        )
        guard_state.update(recovery_metadata)
        by_currency = dict(regime.get("by_currency", {}) if isinstance(regime.get("by_currency"), dict) else {})
        guard_state["overlay"] = overlay
        guard_state["trigger"] = trigger
        guard_state["smoothed_cap_pct"] = cap
        guard_state["smoothing_severity"] = severity
        hard_entry_freeze = bool(self.intraday_guard_hard_entry_freeze)
        by_currency[currency] = {
            "overlay": overlay,
            "entry_freeze": hard_entry_freeze,
            "hard_entry_freeze": hard_entry_freeze,
            "max_total_exposure_pct": cap,
            "base_max_total_exposure_pct": base_cap,
            "entry_cap_pct": entry_cap,
            "risk_off_cap_pct": risk_cap,
            "smoothing_enabled": True,
            "cap_curve": self.intraday_guard_cap_curve,
            "smoothing_severity": severity,
            "source": "intraday_market_guard",
            "guard_symbol": self.intraday_guard_symbol,
            "reference_price": reference,
            "current_price": current_price,
            "intraday_return": intraday_return,
            "drawdown_from_session_high": guard_state["drawdown_from_session_high"],
            "session_high_price": guard_state["session_high_price"],
            "session_low_price": guard_state.get("session_low_price"),
            "recovery_from_session_low": guard_state.get("recovery_from_session_low"),
            "recovery_count": guard_state.get("recovery_count", 0),
            "recovery_release_active": guard_state.get("recovery_release_active", False),
            "recovery_confirmation_cycles": guard_state.get("recovery_confirmation_cycles"),
            "underlying_trigger": guard_state.get("underlying_trigger"),
            "trigger": trigger,
            "exempt_symbols": sorted(self.intraday_guard_exempt_symbols),
        }
        result = dict(regime)
        result["by_currency"] = by_currency
        result["intraday_guard_state"] = guard_state
        result["intraday_market_guard_active"] = True
        result["equity_overlay_active"] = True
        result["equity_overlay"] = overlay
        result["entry_freeze"] = hard_entry_freeze
        return result

    def _base_regime_cap_pct(self, currency: str, regime: dict[str, object]) -> float:
        code = str(currency).upper()
        base = self.max_total_exposure_pct_by_currency.get(code, 0.80)
        if self.regime_exposure_enabled:
            table = self.regime_total_exposure_pct_by_currency.get(code, {})
            if isinstance(table, dict):
                base = float(table.get(str(regime.get("name", "neutral")), base))
        return min(base, self.max_total_exposure_pct_by_currency.get(code, base))

    def _intraday_recovery_adjusted_cap(
        self,
        *,
        context: RiskManagementContext,
        currency: str,
        guard_state: dict[str, object],
        base_cap: float,
        current_cap: float | None,
        overlay: str,
        trigger: str,
    ) -> tuple[float | None, str, str, dict[str, object]]:
        metadata = {
            "recovery_enabled": bool(self.intraday_guard_recovery_enabled),
            "recovery_from_session_low": guard_state.get("recovery_from_session_low"),
            "recovery_count": guard_state.get("recovery_count", 0),
            "recovery_confirmation_cycles": self.intraday_guard_recovery_confirmation_cycles,
            "recovery_release_active": False,
        }
        if (
            not self.intraday_guard_recovery_enabled
            or current_cap is None
            or current_cap >= base_cap
            or self._intraday_open_entry_gate_active(context)
        ):
            return current_cap, trigger, overlay, metadata

        recovery_count = int(_safe_float(guard_state.get("recovery_count")) or 0)
        if recovery_count < self.intraday_guard_recovery_confirmation_cycles:
            return current_cap, trigger, overlay, metadata

        code = str(currency).upper()
        configured_cap = self.intraday_guard_recovery_cap_pct_by_currency.get(code)
        if configured_cap is None:
            configured_cap = self.intraday_entry_freeze_cap_pct_by_currency.get(code, current_cap)
        recovery_cap = min(base_cap, max(current_cap, float(configured_cap)))
        if recovery_cap <= current_cap:
            return current_cap, trigger, overlay, metadata

        metadata.update(
            {
                "recovery_release_active": True,
                "recovery_release_cap_pct": recovery_cap,
                "underlying_trigger": trigger,
            }
        )
        return recovery_cap, "recovery_from_session_low", ENTRY_FREEZE, metadata

    def _intraday_guard_state(
        self,
        context: RiskManagementContext,
        current_price: float,
        reference_price: float | None,
        high_price: float | None = None,
        low_price: float | None = None,
    ) -> dict[str, object]:
        session_date = context.data.time.date().isoformat()
        previous_high = None
        previous_low = None
        previous_current = None
        previous_recovery_count = 0
        record = context.model_state.get(
            sleeve_id=context.sleeve_id,
            model_id=MODEL_ID,
            namespace=INTRADAY_GUARD_NAMESPACE,
            symbol_key=self.intraday_guard_symbol,
        )
        if record is not None and isinstance(record.value, Mapping):
            if str(record.value.get("session_date") or "") == session_date:
                previous_high = _safe_float(record.value.get("session_high_price"))
                previous_low = _safe_float(record.value.get("session_low_price"))
                previous_current = _safe_float(record.value.get("current_price"))
                previous_recovery_count = int(_safe_float(record.value.get("recovery_count")) or 0)
        observed_high = high_price if high_price is not None and high_price > 0 else current_price
        observed_low = low_price if low_price is not None and low_price > 0 else current_price
        session_high = max(previous_high or observed_high, observed_high, current_price)
        session_low = min(previous_low or observed_low, observed_low, current_price)
        drawdown = (current_price / session_high) - 1.0 if session_high > 0 else 0.0
        recovery_from_low = (current_price / session_low) - 1.0 if session_low > 0 else 0.0
        cycle_return = (current_price / previous_current) - 1.0 if previous_current and previous_current > 0 else 0.0
        recovery_ready = (
            self.intraday_guard_recovery_enabled
            and recovery_from_low >= self.intraday_guard_recovery_from_low_pct
            and cycle_return >= 0.0
        )
        recovery_count = previous_recovery_count + 1 if recovery_ready else 0
        return {
            "guard_symbol": self.intraday_guard_symbol,
            "session_date": session_date,
            "session_high_price": session_high,
            "session_low_price": session_low,
            "current_price": current_price,
            "reference_price": reference_price,
            "reference_return": None,
            "drawdown_from_session_high": drawdown,
            "recovery_from_session_low": recovery_from_low,
            "cycle_return": cycle_return,
            "recovery_count": recovery_count,
            "recovery_ready": recovery_ready,
            "recovery_confirmation_cycles": self.intraday_guard_recovery_confirmation_cycles,
            "overlay": NO_OVERLAY,
            "trigger": None,
        }

    def _intraday_open_entry_gate_active(self, context: RiskManagementContext) -> bool:
        if self.intraday_open_entry_freeze_until is None:
            return False
        current = context.data.time.time()
        return time(hour=9, minute=0) <= current < self.intraday_open_entry_freeze_until

    def _intraday_guard_reference_price(self, context: RiskManagementContext) -> float | None:
        latest = None
        for insight in context.active_insights:
            if getattr(insight, "alpha_id", "") != self.intraday_guard_reference_alpha_id:
                continue
            metadata = getattr(insight, "metadata", {}) or {}
            if str(metadata.get("benchmark_symbol") or "").upper() != self.intraday_guard_symbol:
                continue
            value = _safe_float(metadata.get("benchmark_close"))
            if value is None:
                continue
            if latest is None or insight.generated_at >= latest[0]:
                latest = (insight.generated_at, value)
        return latest[1] if latest is not None else None


def create_risk_model(params):
    return LeapsKospiGrowthUsHedgeRiskModel(
        long_only=bool(params.get("long_only", True)),
        max_position_pct_by_currency=_float_map(
            params.get("max_position_pct_by_currency"),
            {"KRW": 0.40, "USD": 0.30},
        ),
        max_total_exposure_pct_by_currency=_float_map(
            params.get("max_total_exposure_pct_by_currency"),
            {"KRW": 0.95, "USD": 0.65},
        ),
        cash_buffer_pct_by_currency=_float_map(
            params.get("cash_buffer_pct_by_currency"),
            {"KRW": 0.02, "USD": 0.08},
        ),
        regime_exposure_enabled=bool(params.get("regime_exposure_enabled", False)),
        regime_total_exposure_pct_by_currency=_nested_float_map(
            params.get("regime_total_exposure_pct_by_currency")
        ),
        regime_equity_overlay_enabled=bool(params.get("regime_equity_overlay_enabled", False)),
        entry_freeze_drawdown_pct_by_currency=_float_map(
            params.get("entry_freeze_drawdown_pct_by_currency"),
            {"KRW": 0.035},
        ),
        risk_off_drawdown_pct_by_currency=_float_map(
            params.get("risk_off_drawdown_pct_by_currency"),
            {"KRW": 0.055},
        ),
        entry_freeze_cycle_loss_pct_by_currency=_float_map(
            params.get("entry_freeze_cycle_loss_pct_by_currency"),
            {"KRW": 0.025},
        ),
        risk_off_cycle_loss_pct_by_currency=_float_map(
            params.get("risk_off_cycle_loss_pct_by_currency"),
            {"KRW": 0.050},
        ),
        entry_freeze_cap_pct_by_currency=_float_map(
            params.get("entry_freeze_cap_pct_by_currency"),
            {"KRW": 0.85},
        ),
        risk_off_cap_pct_by_currency=_float_map(
            params.get("risk_off_cap_pct_by_currency"),
            {"KRW": 0.70},
        ),
        recovery_from_trough_pct_by_currency=_float_map(
            params.get("recovery_from_trough_pct_by_currency"),
            {"KRW": 0.006},
        ),
        recovery_confirmation_cycles=int(params.get("recovery_confirmation_cycles", 2)),
        intraday_market_guard_enabled=bool(params.get("intraday_market_guard_enabled", False)),
        intraday_guard_symbol=str(params.get("intraday_guard_symbol", "KRX:069500")),
        intraday_guard_reference_alpha_id=str(params.get("intraday_guard_reference_alpha_id", ETF_SAFETY_ALPHA_ID)),
        intraday_entry_freeze_return_pct=float(params.get("intraday_entry_freeze_return_pct", -0.02)),
        intraday_risk_off_return_pct=float(params.get("intraday_risk_off_return_pct", -0.04)),
        intraday_entry_freeze_cap_pct_by_currency=_float_map(
            params.get("intraday_entry_freeze_cap_pct_by_currency"),
            {"KRW": 0.55},
        ),
        intraday_risk_off_cap_pct_by_currency=_float_map(
            params.get("intraday_risk_off_cap_pct_by_currency"),
            {"KRW": 0.35},
        ),
        intraday_guard_exempt_symbols=tuple(params.get("intraday_guard_exempt_symbols") or ()),
        intraday_open_entry_freeze_until=params.get("intraday_open_entry_freeze_until"),
        intraday_guard_high_drawdown_enabled=bool(params.get("intraday_guard_high_drawdown_enabled", False)),
        intraday_guard_high_entry_freeze_return_pct=float(
            params.get("intraday_guard_high_entry_freeze_return_pct", -0.006)
        ),
        intraday_guard_high_risk_off_return_pct=float(
            params.get("intraday_guard_high_risk_off_return_pct", -0.012)
        ),
        intraday_guard_smoothing_enabled=bool(params.get("intraday_guard_smoothing_enabled", False)),
        intraday_guard_cap_curve=str(params.get("intraday_guard_cap_curve", "smoothstep")),
        intraday_guard_hard_entry_freeze=bool(params.get("intraday_guard_hard_entry_freeze", True)),
        intraday_guard_recovery_enabled=bool(params.get("intraday_guard_recovery_enabled", False)),
        intraday_guard_recovery_from_low_pct=float(params.get("intraday_guard_recovery_from_low_pct", 0.006)),
        intraday_guard_recovery_confirmation_cycles=int(
            params.get("intraday_guard_recovery_confirmation_cycles", 2)
        ),
        intraday_guard_recovery_cap_pct_by_currency=_float_map(
            params.get("intraday_guard_recovery_cap_pct_by_currency"),
            {"KRW": 0.45},
        ),
        symbol_guard_enabled=bool(params.get("symbol_guard_enabled", False)),
        symbol_guard_exempt_symbols=tuple(params.get("symbol_guard_exempt_symbols") or ()),
        symbol_entry_block_intraday_return_pct=float(
            params.get("symbol_entry_block_intraday_return_pct", -0.025)
        ),
        symbol_entry_block_high_drawdown_pct=float(
            params.get("symbol_entry_block_high_drawdown_pct", -0.040)
        ),
        symbol_entry_block_unrealized_loss_pct=float(
            params.get("symbol_entry_block_unrealized_loss_pct", -0.015)
        ),
        symbol_entry_block_sma10_buffer_pct=_safe_float(params.get("symbol_entry_block_sma10_buffer_pct")),
        symbol_entry_block_sma20_buffer_pct=_safe_float(params.get("symbol_entry_block_sma20_buffer_pct")),
        symbol_reduce_half_unrealized_loss_pct=float(
            params.get("symbol_reduce_half_unrealized_loss_pct", -0.035)
        ),
        symbol_exit_unrealized_loss_pct=float(params.get("symbol_exit_unrealized_loss_pct", -0.060)),
        symbol_reduce_half_high_drawdown_pct=float(
            params.get("symbol_reduce_half_high_drawdown_pct", -0.065)
        ),
        symbol_exit_high_drawdown_pct=float(params.get("symbol_exit_high_drawdown_pct", -0.100)),
        symbol_reduce_half_sma10_buffer_pct=float(
            params.get("symbol_reduce_half_sma10_buffer_pct", -0.005)
        ),
        symbol_exit_sma20_buffer_pct=float(params.get("symbol_exit_sma20_buffer_pct", -0.005)),
        symbol_reduce_fraction=float(params.get("symbol_reduce_fraction", 0.50)),
        symbol_guard_volatility_adjusted_enabled=bool(
            params.get("symbol_guard_volatility_adjusted_enabled", False)
        ),
        symbol_guard_reference_volatility_pct=float(
            params.get("symbol_guard_reference_volatility_pct", 0.04)
        ),
        symbol_guard_min_volatility_multiplier=float(
            params.get("symbol_guard_min_volatility_multiplier", 0.75)
        ),
        symbol_guard_max_volatility_multiplier=float(
            params.get("symbol_guard_max_volatility_multiplier", 1.75)
        ),
        symbol_guard_entry_max_volatility_multiplier=float(
            params.get("symbol_guard_entry_max_volatility_multiplier", 1.25)
        ),
        symbol_guard_recovery_confirmation_cycles=int(
            params.get("symbol_guard_recovery_confirmation_cycles", 3)
        ),
        symbol_pullback_add_enabled=bool(params.get("symbol_pullback_add_enabled", False)),
        symbol_pullback_add_fraction=float(params.get("symbol_pullback_add_fraction", 0.50)),
        symbol_pullback_add_min_intraday_return_pct=float(
            params.get("symbol_pullback_add_min_intraday_return_pct", 0.0)
        ),
        symbol_pullback_add_min_unrealized_pnl_pct=float(
            params.get("symbol_pullback_add_min_unrealized_pnl_pct", 0.0)
        ),
        symbol_pullback_add_min_sma10_gap_pct=float(params.get("symbol_pullback_add_min_sma10_gap_pct", 0.0)),
        symbol_pullback_add_min_sma20_gap_pct=float(params.get("symbol_pullback_add_min_sma20_gap_pct", 0.0)),
        symbol_pullback_add_min_alpha_count=int(params.get("symbol_pullback_add_min_alpha_count", 2)),
        reject_invalid_snapshot=bool(params.get("reject_invalid_snapshot", True)),
        require_fresh_for_entries=bool(params.get("require_fresh_for_entries", True)),
    )


def _reject(target: PortfolioTarget, reason: str, metadata: dict[str, object] | None = None) -> RiskDecision:
    return RiskDecision(
        original_target=target,
        approved_target=None,
        status=RiskDecisionStatus.REJECTED,
        reason=reason,
        metadata=metadata or {},
    )


def _no_room_reason(
    *,
    requested_quantity: int,
    current_quantity: int,
    position_limited_quantity: int,
    exposure_limited_quantity: int,
    cash_limited_quantity: int,
) -> str:
    if requested_quantity > current_quantity:
        if position_limited_quantity <= current_quantity:
            return "position_limit_no_room"
        if exposure_limited_quantity <= current_quantity:
            return "exposure_limit_no_room"
        if cash_limited_quantity <= current_quantity:
            return "cash_limit_no_room"
    if requested_quantity < current_quantity:
        return "target_reduction_blocked"
    return "risk_clamped_to_current"


def _risk_tag(tag: object, reason: str) -> str:
    base = str(tag or "").strip()
    token = f"risk:{reason}"
    if token in base:
        return base
    if not base:
        return token
    return f"{base}|{token}"


def _float_map(value, fallback: dict[str, float]) -> dict[str, float]:
    if not isinstance(value, dict):
        return dict(fallback)
    result = dict(fallback)
    for key, item in value.items():
        result[str(key).upper()] = float(item)
    return result


def _nested_float_map(value) -> dict[str, dict[str, float]] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, dict[str, float]] = {}
    for currency, table in value.items():
        if not isinstance(table, dict):
            continue
        result[str(currency).upper()] = {str(name): float(item) for name, item in table.items()}
    return result


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _scale_loss_threshold(value: float, multiplier: float) -> float:
    if value < 0:
        return value * multiplier
    return value


def _first_metadata_float(metadata: Mapping[str, object], *names: str) -> float | None:
    for name in names:
        value = _safe_float(metadata.get(name))
        if value is not None:
            return value
    return None


def _parse_time(value: object) -> time | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        hour, minute = text.split(":", maxsplit=1)
        return time(hour=int(hour), minute=int(minute[:2]))
    except (TypeError, ValueError):
        return None


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _currency_regime(regime: dict[str, object], currency: str) -> dict[str, object]:
    by_currency = regime.get("by_currency")
    if not isinstance(by_currency, dict):
        return {}
    item = by_currency.get(str(currency).upper())
    return dict(item) if isinstance(item, dict) else {}


def _overlay_severity(overlay: str) -> int:
    if overlay == INTRADAY_RISK_OFF:
        return 2
    if overlay == ENTRY_FREEZE:
        return 1
    return 0


def _stronger_overlay(left: str, right: str) -> str:
    return left if _overlay_severity(left) >= _overlay_severity(right) else right


def _overlay_trigger(overlay: str) -> str:
    if overlay == INTRADAY_RISK_OFF:
        return "equity_drawdown_or_cycle_loss_risk_off"
    if overlay == ENTRY_FREEZE:
        return "equity_drawdown_or_cycle_loss_entry_freeze"
    return "no_equity_overlay"


def _smooth_intraday_cap(
    *,
    value: float,
    entry_threshold: float,
    risk_threshold: float,
    base_cap: float,
    entry_cap: float,
    risk_cap: float,
    curve: str,
) -> tuple[float, float]:
    if value >= 0.0:
        return base_cap, 0.0
    entry = min(0.0, float(entry_threshold))
    risk = min(entry, float(risk_threshold))
    if risk == entry:
        return (risk_cap, 1.0) if value <= risk else (base_cap, 0.0)
    if value <= risk:
        return risk_cap, 1.0
    if value <= entry:
        progress = (entry - value) / (entry - risk)
        shaped = _shape_progress(progress, curve)
        return _lerp(entry_cap, risk_cap, shaped), 0.5 + (0.5 * shaped)
    progress = (0.0 - value) / (0.0 - entry) if entry < 0.0 else 0.0
    shaped = _shape_progress(progress, curve)
    return _lerp(base_cap, entry_cap, shaped), 0.5 * shaped


def _shape_progress(progress: float, curve: str) -> float:
    x = min(1.0, max(0.0, progress))
    if str(curve).lower() == "linear":
        return x
    return x * x * (3.0 - (2.0 * x))


def _lerp(left: float, right: float, progress: float) -> float:
    return left + ((right - left) * min(1.0, max(0.0, progress)))

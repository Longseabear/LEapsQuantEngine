from __future__ import annotations

from collections.abc import Mapping
from datetime import time

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
        return RiskDecisionBatch(
            sleeve_id=context.sleeve_id,
            decisions=tuple(decisions),
            state_patches=self._state_patches(context, regime),
        )

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
        if (
            target.quantity > current_quantity
            and bool(currency_regime.get("entry_freeze", False))
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

        position_limited_quantity = self._clamp_position(context, target, price)
        exposure_limited_quantity = self._clamp_total_exposure(
            context,
            target,
            position_limited_quantity,
            price,
            approved_quantities,
            approved_symbols,
            max_total_exposure_pct_by_currency,
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

        approved = PortfolioTarget(symbol=target.symbol, quantity=cash_limited_quantity, tag=target.tag)
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
    ) -> int:
        currency = currency_for_symbol(target.symbol)
        equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
        if equity <= 0:
            return 0
        max_total_exposure = equity * max_total_exposure_pct_by_currency.get(currency, 0.80)
        exposure_without_target = 0.0
        for symbol_key, quantity in approved_quantities.items():
            if symbol_key == target.symbol.key:
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
    ) -> tuple[StatePatch, ...]:
        return (
            *self._equity_overlay_state_patches(context, regime),
            *self._intraday_guard_state_patches(context, regime),
        )

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
                    "current_price": state.get("current_price"),
                    "reference_price": state.get("reference_price"),
                    "reference_return": state.get("reference_return"),
                    "drawdown_from_session_high": state.get("drawdown_from_session_high"),
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
        guard_state = self._intraday_guard_state(context, float(guard_bar.close), reference)
        if reference is None or reference <= 0:
            result = dict(regime)
            result["intraday_guard_state"] = guard_state
            return result
        intraday_return = (float(guard_bar.close) / reference) - 1.0
        guard_state["reference_return"] = intraday_return
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
        by_currency["KRW"] = {
            "overlay": overlay,
            "entry_freeze": True,
            "max_total_exposure_pct": cap_table.get("KRW"),
            "source": "intraday_market_guard",
            "guard_symbol": self.intraday_guard_symbol,
            "reference_price": reference,
            "current_price": float(guard_bar.close),
            "intraday_return": intraday_return,
            "drawdown_from_session_high": guard_state["drawdown_from_session_high"],
            "session_high_price": guard_state["session_high_price"],
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

    def _intraday_guard_state(
        self,
        context: RiskManagementContext,
        current_price: float,
        reference_price: float | None,
    ) -> dict[str, object]:
        session_date = context.data.time.date().isoformat()
        previous_high = None
        record = context.model_state.get(
            sleeve_id=context.sleeve_id,
            model_id=MODEL_ID,
            namespace=INTRADAY_GUARD_NAMESPACE,
            symbol_key=self.intraday_guard_symbol,
        )
        if record is not None and isinstance(record.value, Mapping):
            if str(record.value.get("session_date") or "") == session_date:
                previous_high = _safe_float(record.value.get("session_high_price"))
        session_high = max(previous_high or current_price, current_price)
        drawdown = (current_price / session_high) - 1.0 if session_high > 0 else 0.0
        return {
            "guard_symbol": self.intraday_guard_symbol,
            "session_date": session_date,
            "session_high_price": session_high,
            "current_price": current_price,
            "reference_price": reference_price,
            "reference_return": None,
            "drawdown_from_session_high": drawdown,
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

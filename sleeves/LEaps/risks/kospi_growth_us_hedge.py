from __future__ import annotations

from leaps_quant_engine.framework.risk import (
    RiskDecision,
    RiskDecisionBatch,
    RiskDecisionStatus,
    RiskManagementContext,
)
from leaps_quant_engine.models import PortfolioTarget
from leaps_quant_engine.portfolio import currency_for_symbol


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
        self.reject_invalid_snapshot = reject_invalid_snapshot
        self.require_fresh_for_entries = require_fresh_for_entries

    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        decisions: list[RiskDecision] = []
        regime = self._market_regime(context)
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
        currencies = sorted({currency_for_symbol(target.symbol) for target in context.targets} | set(context.portfolio.currencies()))
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
        return RiskDecisionBatch(sleeve_id=context.sleeve_id, decisions=tuple(decisions))

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
        return result

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
            return {
                "name": "risk_off" if stop_count else "neutral",
                "market_breadth": 0.0,
                "average_momentum": 0.0,
                "average_volatility": 0.0,
                "stop_pressure": stop_count,
                "source": "active_insights",
            }

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
        elif breadth >= 0.55 and average_momentum >= 0.18 and average_volatility <= 0.14:
            name = "strong_risk_on"
        elif breadth >= 0.45 and average_momentum >= 0.08 and average_volatility <= 0.16:
            name = "risk_on"
        else:
            name = "neutral"
        return {
            "name": name,
            "market_breadth": breadth,
            "average_momentum": average_momentum,
            "average_volatility": average_volatility,
            "stop_pressure": stop_count,
            "source": "active_insights",
        }


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


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0

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
        reject_invalid_snapshot: bool = True,
        require_fresh_for_entries: bool = True,
    ) -> None:
        self.long_only = long_only
        self.max_position_pct_by_currency = max_position_pct_by_currency or {"KRW": 0.40, "USD": 0.30}
        self.max_total_exposure_pct_by_currency = max_total_exposure_pct_by_currency or {"KRW": 0.95, "USD": 0.65}
        self.cash_buffer_pct_by_currency = cash_buffer_pct_by_currency or {"KRW": 0.02, "USD": 0.08}
        self.reject_invalid_snapshot = reject_invalid_snapshot
        self.require_fresh_for_entries = require_fresh_for_entries

    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        decisions: list[RiskDecision] = []
        approved_quantities = {
            holding.symbol.key: holding.quantity
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
            decision, remaining_cash = self._evaluate_target(context, target, approved_quantities, available_cash)
            available_cash[currency_for_symbol(target.symbol)] = remaining_cash
            if decision.approved_target is not None:
                approved_quantities[decision.approved_target.symbol.key] = decision.approved_target.quantity
            decisions.append(decision)
        return RiskDecisionBatch(sleeve_id=context.sleeve_id, decisions=tuple(decisions))

    def _evaluate_target(
        self,
        context: RiskManagementContext,
        target: PortfolioTarget,
        approved_quantities: dict[str, int],
        available_cash_by_currency: dict[str, float],
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

        target_quantity = self._clamp_position(context, target, price)
        target_quantity = self._clamp_total_exposure(context, target, target_quantity, price, approved_quantities)
        target_quantity, remaining_cash = self._clamp_cash(
            current_quantity=current_quantity,
            target_quantity=target_quantity,
            price=price,
            available_cash=available_cash,
        )
        if target_quantity == current_quantity and target.quantity != current_quantity:
            return (
                _reject(
                    target,
                    "insufficient_cash_or_position_too_small",
                    {
                        "currency": currency,
                        "price": price,
                        "current_quantity": current_quantity,
                        "requested_quantity": target.quantity,
                        "available_cash": available_cash,
                    },
                ),
                remaining_cash,
            )

        approved = PortfolioTarget(symbol=target.symbol, quantity=target_quantity, tag=target.tag)
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
                    "approved_quantity": target_quantity,
                    "max_position_pct": self.max_position_pct_by_currency.get(currency),
                    "max_total_exposure_pct": self.max_total_exposure_pct_by_currency.get(currency),
                    "cash_buffer_pct": self.cash_buffer_pct_by_currency.get(currency),
                    "available_cash_after": remaining_cash,
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
    ) -> int:
        currency = currency_for_symbol(target.symbol)
        equity = context.portfolio.equity_by_currency(context.data, (currency,)).get(currency, 0.0)
        if equity <= 0:
            return 0
        max_total_exposure = equity * self.max_total_exposure_pct_by_currency.get(currency, 0.80)
        exposure_without_target = 0.0
        for holding in context.portfolio.holdings.values():
            if holding.symbol.key == target.symbol.key or currency_for_symbol(holding.symbol) != currency:
                continue
            mark = context.portfolio.mark_price(holding.symbol, context.data)
            if mark is None:
                continue
            quantity = approved_quantities.get(holding.symbol.key, holding.quantity)
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


def _float_map(value, fallback: dict[str, float]) -> dict[str, float]:
    if not isinstance(value, dict):
        return dict(fallback)
    result = dict(fallback)
    for key, item in value.items():
        result[str(key).upper()] = float(item)
    return result

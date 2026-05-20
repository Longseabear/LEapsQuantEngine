from dataclasses import dataclass
from math import floor

from leaps_quant_engine.framework import (
    BasicRiskManagementModel,
    RiskDecision,
    RiskDecisionBatch,
    RiskDecisionStatus,
    RiskLimits,
    RiskManagementContext,
    RiskManagementModel,
)
from leaps_quant_engine.models import PortfolioTarget
from leaps_quant_engine.portfolio import currency_for_symbol


def create_risk_model(params):
    base_model = BasicRiskManagementModel(
        limits=RiskLimits(
            long_only=bool(params.get("long_only", True)),
            max_position_pct=float(params.get("max_position_pct", 0.30)),
            max_total_exposure_pct=float(params.get("max_total_exposure_pct", 0.95)),
            cash_buffer_pct=float(params.get("cash_buffer_pct", 0.05)),
            require_fresh_for_entries=bool(params.get("require_fresh_for_entries", True)),
            reject_invalid_snapshot=bool(params.get("reject_invalid_snapshot", True)),
        )
    )
    max_cycle_buy_notional = _optional_positive_float(params.get("max_cycle_buy_notional"))
    max_cycle_buy_notional_pct = _optional_positive_float(params.get("max_cycle_buy_notional_pct"))
    if max_cycle_buy_notional is None and max_cycle_buy_notional_pct is None:
        return base_model
    return CycleBuyNotionalRiskModel(
        base_model=base_model,
        max_cycle_buy_notional=max_cycle_buy_notional,
        max_cycle_buy_notional_pct=max_cycle_buy_notional_pct,
        currency=str(params.get("cycle_buy_notional_currency", "USD")),
    )


@dataclass(frozen=True, slots=True)
class CycleBuyNotionalRiskModel:
    base_model: RiskManagementModel
    max_cycle_buy_notional: float | None = None
    max_cycle_buy_notional_pct: float | None = None
    currency: str = "USD"

    def manage_risk(self, context: RiskManagementContext) -> RiskDecisionBatch:
        base_batch = self.base_model.manage_risk(context)
        cap = self._cycle_buy_notional_cap(context)
        if cap is None:
            return base_batch

        entries = _cycle_buy_entries(context, base_batch, currency=self.currency)
        requested_notional = sum(entry.requested_delta * entry.price for entry in entries)
        if requested_notional <= cap:
            return base_batch

        approved_deltas = _allocate_cycle_buy_deltas(entries, max_notional=cap)
        approved_notional = sum(approved_deltas[index] * entry.price for index, entry in enumerate(entries))
        decisions_by_index = {
            entry.decision_index: (entry, approved_deltas[index])
            for index, entry in enumerate(entries)
        }
        decisions: list[RiskDecision] = []
        for index, decision in enumerate(base_batch.decisions):
            entry_and_delta = decisions_by_index.get(index)
            if entry_and_delta is None:
                decisions.append(decision)
                continue

            entry, approved_delta = entry_and_delta
            if approved_delta >= entry.requested_delta:
                decisions.append(decision)
                continue

            approved_target = PortfolioTarget(
                symbol=entry.approved_target.symbol,
                quantity=entry.current_quantity + approved_delta,
                tag=entry.approved_target.tag,
            )
            decisions.append(
                RiskDecision(
                    original_target=decision.original_target,
                    approved_target=approved_target,
                    status=RiskDecisionStatus.CLAMPED,
                    reason="cycle_buy_notional_clamped",
                    metadata={
                        **dict(decision.metadata),
                        "base_risk_status": decision.status.value,
                        "base_risk_reason": decision.reason,
                        "max_cycle_buy_notional": cap,
                        "requested_cycle_buy_notional": requested_notional,
                        "approved_cycle_buy_notional": approved_notional,
                        "requested_delta_quantity": entry.requested_delta,
                        "approved_delta_quantity": approved_delta,
                        "price": entry.price,
                        "currency": self.currency.upper(),
                    },
                )
            )
        return RiskDecisionBatch(
            sleeve_id=base_batch.sleeve_id,
            decisions=tuple(decisions),
            state_patches=base_batch.state_patches,
        )

    def _cycle_buy_notional_cap(self, context: RiskManagementContext) -> float | None:
        caps: list[float] = []
        if self.max_cycle_buy_notional is not None:
            caps.append(self.max_cycle_buy_notional)
        if self.max_cycle_buy_notional_pct is not None:
            equity = context.portfolio.equity_by_currency(context.data, (self.currency,)).get(self.currency.upper(), 0.0)
            if equity > 0:
                caps.append(equity * self.max_cycle_buy_notional_pct)
        if not caps:
            return None
        return max(0.0, min(caps))


@dataclass(frozen=True, slots=True)
class _CycleBuyEntry:
    decision_index: int
    approved_target: PortfolioTarget
    current_quantity: int
    requested_delta: int
    price: float


def _cycle_buy_entries(
    context: RiskManagementContext,
    batch: RiskDecisionBatch,
    *,
    currency: str,
) -> tuple[_CycleBuyEntry, ...]:
    entries: list[_CycleBuyEntry] = []
    target_currency = currency.upper()
    for index, decision in enumerate(batch.decisions):
        approved_target = decision.approved_target
        if approved_target is None:
            continue
        if currency_for_symbol(approved_target.symbol) != target_currency:
            continue
        current_quantity = context.portfolio.quantity(approved_target.symbol)
        requested_delta = approved_target.quantity - current_quantity
        if requested_delta <= 0:
            continue
        price = _decision_price(context, decision, approved_target)
        if price is None or price <= 0:
            continue
        entries.append(
            _CycleBuyEntry(
                decision_index=index,
                approved_target=approved_target,
                current_quantity=current_quantity,
                requested_delta=requested_delta,
                price=price,
            )
        )
    return tuple(entries)


def _decision_price(
    context: RiskManagementContext,
    decision: RiskDecision,
    target: PortfolioTarget,
) -> float | None:
    metadata_price = decision.metadata.get("price")
    try:
        price = float(metadata_price)
    except (TypeError, ValueError):
        price = context.portfolio.mark_price(target.symbol, context.data)
    return price


def _allocate_cycle_buy_deltas(entries: tuple[_CycleBuyEntry, ...], *, max_notional: float) -> dict[int, int]:
    requested_notional = sum(entry.requested_delta * entry.price for entry in entries)
    if requested_notional <= 0:
        return {index: 0 for index in range(len(entries))}
    scale = min(1.0, max_notional / requested_notional)
    allocations: dict[int, int] = {}
    fractions: list[tuple[float, int]] = []
    used_notional = 0.0
    for index, entry in enumerate(entries):
        scaled_delta = entry.requested_delta * scale
        approved_delta = min(entry.requested_delta, int(floor(scaled_delta)))
        allocations[index] = approved_delta
        fractions.append((scaled_delta - approved_delta, index))
        used_notional += approved_delta * entry.price

    remaining_notional = max_notional - used_notional
    for _, index in sorted(fractions, key=lambda item: (-item[0], item[1])):
        entry = entries[index]
        if allocations[index] >= entry.requested_delta:
            continue
        if remaining_notional + 1e-9 < entry.price:
            continue
        allocations[index] += 1
        remaining_notional -= entry.price
    return allocations


def _optional_positive_float(value) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if parsed > 0 else None

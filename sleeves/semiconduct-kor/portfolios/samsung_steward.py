from dataclasses import dataclass

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
)
from leaps_quant_engine.runtime_state import StatePatch


SAMSUNG_SYMBOL_KEY = "KRX:005930"
STEWARD_ALPHA_ID = "semiconduct-kor-samsung-steward"
TRAILING_STOP_ALPHA_ID = "semiconduct-kor-volatility-trailing-stop"
PORTFOLIO_MODEL_ID = "semiconduct-kor-samsung-steward-portfolio"
CAPITULATION_NAMESPACE = "capitulation"
CAPITULATION_REBALANCE_BAND = 0.025


@dataclass(frozen=True, slots=True)
class SamsungStewardPortfolioConstructionModel:
    max_target_percent: float = 1.0
    fallback_hold_existing: bool = True
    min_cash_to_add_pct: float = 0.01

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_target_percent <= 1.0:
            raise ValueError("max_target_percent must be between 0 and 1.")
        if not 0.0 <= self.min_cash_to_add_pct <= 1.0:
            raise ValueError("min_cash_to_add_pct must be between 0 and 1.")

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        samsung = _samsung_symbol(context)
        if samsung is None:
            return ()

        decision = _samsung_decision(
            context,
            samsung,
            max_target_percent=self.max_target_percent,
            min_cash_to_add_pct=self.min_cash_to_add_pct,
        )
        if decision is None:
            if not self.fallback_hold_existing or context.portfolio.quantity(samsung) == 0:
                return ()
            target_percent = min(self.max_target_percent, _current_percent(context, samsung.key))
            return (
                PortfolioAllocationTarget(
                    symbol=samsung,
                    target_percent=target_percent,
                    tag="samsung_steward:fallback_hold_existing",
                ),
            )

        insight, target_percent, action = decision
        return (
            PortfolioAllocationTarget(
                symbol=samsung,
                target_percent=target_percent,
                tag=f"samsung_steward:{insight.alpha_id}:{action}:{insight.direction.value}",
            ),
        )

    def state_patches(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[StatePatch, ...]:
        return _capitulation_state_patches(context, targets)


def create_portfolio_model(params):
    return SamsungStewardPortfolioConstructionModel(
        max_target_percent=float(params.get("max_target_percent", 1.0)),
        fallback_hold_existing=bool(params.get("fallback_hold_existing", True)),
        min_cash_to_add_pct=float(params.get("min_cash_to_add_pct", 0.01)),
    )


def _capitulation_state_patches(context: PortfolioConstructionContext, targets: tuple[PortfolioAllocationTarget, ...]) -> tuple[StatePatch, ...]:
    samsung = _samsung_symbol(context)
    if samsung is None:
        return ()
    insight = _latest_steward_insight(context.active_insights)
    if insight is None or _action(insight, fallback="") != "risk_capitulation_accumulate":
        return ()
    target = next((item for item in targets if item.symbol.key == samsung.key), None)
    if target is None:
        return ()

    base_target = _clamp(_metadata_float(insight, "base_target_percent") or 0.35, 0.0, 1.0)
    current_price = context.portfolio.mark_price(samsung, context.data)
    stop_price = _metadata_float(insight, "capitulation_stop_price")
    stopped = current_price is not None and stop_price is not None and current_price < stop_price
    active = False if stopped else target.target_percent > base_target + 0.001
    return (
        StatePatch(
            key=context.model_state.key(
                model_id=PORTFOLIO_MODEL_ID,
                namespace=CAPITULATION_NAMESPACE,
                symbol_key=samsung.key,
            ),
            value={
                "active": active,
                "last_price": current_price,
                "last_target_percent": target.target_percent,
                "base_target_percent": base_target,
                "trigger_price": _metadata_float(insight, "capitulation_trigger_price"),
                "stop_price": stop_price,
            },
            reason="samsung_steward_capitulation_target_mark",
        ),
    )


def _samsung_symbol(context: PortfolioConstructionContext):
    for symbol in (*context.managed_symbols, *context.held_symbols):
        if symbol.key == SAMSUNG_SYMBOL_KEY:
            return symbol
    for insight in context.active_insights:
        if insight.symbol_key == SAMSUNG_SYMBOL_KEY:
            return insight.symbol
    return None


def _samsung_decision(
    context: PortfolioConstructionContext,
    samsung,
    *,
    max_target_percent: float,
    min_cash_to_add_pct: float,
) -> tuple[Insight, float, str] | None:
    risk_override = _latest_risk_override(context.active_insights)
    if risk_override is not None:
        return (
            risk_override,
            _target_percent_from_insight(risk_override, max_target_percent=max_target_percent),
            _action(risk_override, fallback="risk_override"),
        )

    insight = _latest_steward_insight(context.active_insights)
    if insight is None:
        return None

    current_percent = _current_percent(context, samsung.key)
    target_percent = _steward_target_percent(
        context,
        samsung,
        insight,
        current_percent=current_percent,
        max_target_percent=max_target_percent,
        min_cash_to_add_pct=min_cash_to_add_pct,
    )
    return insight, target_percent, _action(insight, fallback="target")


def _latest_steward_insight(insights: tuple[Insight, ...]) -> Insight | None:
    latest: Insight | None = None
    for insight in insights:
        if insight.symbol_key != SAMSUNG_SYMBOL_KEY or insight.alpha_id != STEWARD_ALPHA_ID:
            continue
        if latest is None or insight.generated_at > latest.generated_at:
            latest = insight
    return latest


def _latest_risk_override(insights: tuple[Insight, ...]) -> Insight | None:
    latest: Insight | None = None
    for insight in insights:
        if insight.symbol_key != SAMSUNG_SYMBOL_KEY or not _is_risk_override(insight):
            continue
        if latest is None or insight.generated_at > latest.generated_at:
            latest = insight
    return latest


def _is_risk_override(insight: Insight) -> bool:
    target_percent = _metadata_float(insight, "target_percent")
    if insight.alpha_id == TRAILING_STOP_ALPHA_ID:
        return True
    if target_percent is not None and target_percent <= 0.0:
        return True
    return insight.direction in {InsightDirection.FLAT, InsightDirection.DOWN} and insight.weight == 0.0


def _steward_target_percent(
    context: PortfolioConstructionContext,
    samsung,
    insight: Insight,
    *,
    current_percent: float,
    max_target_percent: float,
    min_cash_to_add_pct: float,
) -> float:
    raw_target = _target_percent_from_insight(insight, max_target_percent=max_target_percent)
    action = _action(insight, fallback="")
    if action == "risk_capitulation_accumulate":
        return _capitulation_target_percent(
            context,
            samsung,
            insight,
            current_percent=current_percent,
            raw_target=raw_target,
            max_target_percent=max_target_percent,
            min_cash_to_add_pct=min_cash_to_add_pct,
        )
    if action.startswith("accumulate") or action == "rebuild_after_defense":
        delta = max(_metadata_float(insight, "target_delta_percent") or 0.0, 0.0)
        signal_cap = _clamp(_metadata_float(insight, "max_target_percent") or raw_target, 0.0, max_target_percent)
        desired = min(signal_cap, current_percent + delta) if delta > 0 else raw_target
        return _cap_add_by_cash(
            context,
            samsung,
            current_percent=current_percent,
            desired_percent=desired,
            min_cash_to_add_pct=min_cash_to_add_pct,
        )
    if raw_target > current_percent:
        return _cap_add_by_cash(
            context,
            samsung,
            current_percent=current_percent,
            desired_percent=raw_target,
            min_cash_to_add_pct=min_cash_to_add_pct,
        )
    return raw_target


def _capitulation_target_percent(
    context: PortfolioConstructionContext,
    samsung,
    insight: Insight,
    *,
    current_percent: float,
    raw_target: float,
    max_target_percent: float,
    min_cash_to_add_pct: float,
) -> float:
    base_target = _clamp(
        _metadata_float(insight, "base_target_percent") or raw_target,
        0.0,
        max_target_percent,
    )
    stopped = _capitulation_price_stopped(context, samsung, insight)
    active = _capitulation_active(context, samsung.key)
    triggered = _capitulation_price_triggered(context, samsung, insight)
    if stopped:
        return base_target
    if not active and current_percent > base_target + 0.015:
        return base_target
    if not active and not triggered:
        return base_target

    delta = max(_metadata_float(insight, "target_delta_percent") or 0.0, 0.0)
    signal_cap = _clamp(
        _metadata_float(insight, "max_target_percent") or base_target,
        base_target,
        max_target_percent,
    )
    desired = min(signal_cap, base_target + delta)
    if abs(current_percent - desired) <= CAPITULATION_REBALANCE_BAND:
        return current_percent
    return _cap_add_by_cash(
        context,
        samsung,
        current_percent=current_percent,
        desired_percent=desired,
        min_cash_to_add_pct=min_cash_to_add_pct,
    )


def _capitulation_price_triggered(context: PortfolioConstructionContext, samsung, insight: Insight) -> bool:
    current_price = context.portfolio.mark_price(samsung, context.data)
    trigger_price = _metadata_float(insight, "capitulation_trigger_price")
    stop_price = _metadata_float(insight, "capitulation_stop_price")
    if current_price is None or trigger_price is None:
        return False
    if current_price > trigger_price:
        return False
    return stop_price is None or current_price >= stop_price


def _capitulation_price_stopped(context: PortfolioConstructionContext, samsung, insight: Insight) -> bool:
    current_price = context.portfolio.mark_price(samsung, context.data)
    stop_price = _metadata_float(insight, "capitulation_stop_price")
    return current_price is not None and stop_price is not None and current_price < stop_price


def _capitulation_active(context: PortfolioConstructionContext, symbol_key: str) -> bool:
    record = context.model_state.get(
        model_id=PORTFOLIO_MODEL_ID,
        namespace=CAPITULATION_NAMESPACE,
        symbol_key=symbol_key,
    )
    return bool(record is not None and record.value.get("active"))


def _target_percent_from_insight(insight: Insight, *, max_target_percent: float) -> float:
    raw = insight.metadata.get("target_percent")
    if raw is not None:
        return _clamp(float(raw), 0.0, max_target_percent)
    if insight.direction is InsightDirection.UP:
        return max_target_percent
    return 0.0


def _cap_add_by_cash(
    context: PortfolioConstructionContext,
    samsung,
    *,
    current_percent: float,
    desired_percent: float,
    min_cash_to_add_pct: float,
) -> float:
    if desired_percent <= current_percent:
        return desired_percent
    target_value = context.target_value_for_symbol(samsung)
    if target_value <= 0:
        return current_percent
    available_cash_pct = max(context.portfolio.cash_for_symbol(samsung), 0.0) / target_value
    if available_cash_pct < min_cash_to_add_pct:
        return current_percent
    return _clamp(min(desired_percent, current_percent + available_cash_pct), 0.0, 1.0)


def _current_percent(context: PortfolioConstructionContext, symbol_key: str) -> float:
    symbol = next((item for item in context.held_symbols if item.key == symbol_key), None)
    if symbol is None:
        return 0.0
    target_value = context.target_value_for_symbol(symbol)
    if target_value <= 0:
        return 0.0
    return _clamp(context.portfolio.position_value(symbol, context.data) / target_value, 0.0, 1.0)


def _metadata_float(insight: Insight, name: str) -> float | None:
    value = insight.metadata.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _action(insight: Insight, *, fallback: str) -> str:
    value = str(insight.metadata.get("action") or "").strip()
    return value or fallback


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

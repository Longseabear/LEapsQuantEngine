from dataclasses import dataclass

from leaps_quant_engine.alpha import Insight, InsightDirection
from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
)


SAMSUNG_SYMBOL_KEY = "KRX:005930"
STRIKE_REENTRY_ALPHA_ID = "semiconduct-kor-samsung-strike-reentry"


@dataclass(frozen=True, slots=True)
class SamsungBuyOnlyPortfolioConstructionModel:
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
        insight = _latest_buy_only_insight(context.active_insights)
        current_percent = _current_percent(context, samsung.key)
        if insight is None:
            if self.fallback_hold_existing and current_percent > 0:
                return (
                    PortfolioAllocationTarget(
                        symbol=samsung,
                        target_percent=current_percent,
                        tag="samsung_buy_only:fallback_hold_existing",
                    ),
                )
            return ()

        target_percent = _buy_only_target_percent(
            context,
            samsung,
            insight,
            current_percent=current_percent,
            max_target_percent=self.max_target_percent,
            min_cash_to_add_pct=self.min_cash_to_add_pct,
        )
        return (
            PortfolioAllocationTarget(
                symbol=samsung,
                target_percent=target_percent,
                tag=f"samsung_buy_only:{insight.alpha_id}:{_action(insight)}:{insight.direction.value}",
            ),
        )


def create_portfolio_model(params):
    return SamsungBuyOnlyPortfolioConstructionModel(
        max_target_percent=float(params.get("max_target_percent", 1.0)),
        fallback_hold_existing=bool(params.get("fallback_hold_existing", True)),
        min_cash_to_add_pct=float(params.get("min_cash_to_add_pct", 0.01)),
    )


def _samsung_symbol(context: PortfolioConstructionContext):
    for symbol in (*context.managed_symbols, *context.held_symbols):
        if symbol.key == SAMSUNG_SYMBOL_KEY:
            return symbol
    for insight in context.active_insights:
        if insight.symbol_key == SAMSUNG_SYMBOL_KEY:
            return insight.symbol
    return None


def _latest_buy_only_insight(insights: tuple[Insight, ...]) -> Insight | None:
    latest: Insight | None = None
    for insight in insights:
        if insight.symbol_key != SAMSUNG_SYMBOL_KEY or insight.alpha_id != STRIKE_REENTRY_ALPHA_ID:
            continue
        if insight.direction is not InsightDirection.UP:
            continue
        if latest is None or insight.generated_at > latest.generated_at:
            latest = insight
    return latest


def _buy_only_target_percent(
    context: PortfolioConstructionContext,
    samsung,
    insight: Insight,
    *,
    current_percent: float,
    max_target_percent: float,
    min_cash_to_add_pct: float,
) -> float:
    raw_target = _target_percent_from_insight(insight, max_target_percent=max_target_percent)
    delta = max(_metadata_float(insight, "target_delta_percent") or 0.0, 0.0)
    desired = min(raw_target, current_percent + delta) if delta > 0 else raw_target
    desired = max(current_percent, desired)
    if desired <= current_percent:
        return current_percent
    return _cap_add_by_cash(
        context,
        samsung,
        current_percent=current_percent,
        desired_percent=desired,
        min_cash_to_add_pct=min_cash_to_add_pct,
    )


def _cap_add_by_cash(
    context: PortfolioConstructionContext,
    samsung,
    *,
    current_percent: float,
    desired_percent: float,
    min_cash_to_add_pct: float,
) -> float:
    target_value = context.target_value_for_symbol(samsung)
    if target_value <= 0:
        return current_percent
    available_cash_pct = max(context.portfolio.cash_for_symbol(samsung), 0.0) / target_value
    if available_cash_pct < min_cash_to_add_pct:
        return current_percent
    return _clamp(min(desired_percent, current_percent + available_cash_pct), current_percent, 1.0)


def _current_percent(context: PortfolioConstructionContext, symbol_key: str) -> float:
    symbol = next((item for item in context.held_symbols if item.key == symbol_key), None)
    if symbol is None:
        return 0.0
    target_value = context.target_value_for_symbol(symbol)
    if target_value <= 0:
        return 0.0
    return _clamp(context.portfolio.position_value(symbol, context.data) / target_value, 0.0, 1.0)


def _target_percent_from_insight(insight: Insight, *, max_target_percent: float) -> float:
    raw = insight.metadata.get("target_percent")
    if raw is not None:
        return _clamp(float(raw), 0.0, max_target_percent)
    return _clamp(float(insight.weight or 0.0), 0.0, max_target_percent)


def _metadata_float(insight: Insight, name: str) -> float | None:
    value = insight.metadata.get(name)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _action(insight: Insight) -> str:
    return str(insight.metadata.get("action") or "buy_only_target")


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))

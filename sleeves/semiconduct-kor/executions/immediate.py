from __future__ import annotations

from leaps_quant_engine.execution import StandardExecutionModel
from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, PortfolioTarget
from leaps_quant_engine.portfolio import Portfolio


class SemiconductKorExecutionModel:
    def __init__(
        self,
        tag_prefix: str = "semiconduct-kor",
        order_type: str = "limit",
        time_in_force: str = "day",
        limit_offset_bps: float = 8.0,
        max_slice_quantity: int | None = None,
        max_slice_notional: float | None = 1_500_000.0,
        max_slices: int | None = 3,
        urgency: str = "normal",
        max_order_age_seconds: float | None = 900.0,
        price_drift_bps: float | None = 80.0,
        min_replace_interval_seconds: float | None = 180.0,
        max_replacements: int | None = 2,
        allow_sells: bool = False,
        buy_window: str = "",
        sell_window: str = "",
        window_timezone: str = "Asia/Seoul",
    ) -> None:
        self.allow_sells = bool(allow_sells)
        self.base_model = StandardExecutionModel(
            order_type=order_type,
            time_in_force=time_in_force,
            limit_offset_bps=limit_offset_bps,
            max_slice_quantity=max_slice_quantity,
            max_slice_notional=max_slice_notional,
            max_slices=max_slices,
            tag_prefix=tag_prefix,
            urgency=urgency,
            max_order_age_seconds=max_order_age_seconds,
            price_drift_bps=price_drift_bps,
            min_replace_interval_seconds=min_replace_interval_seconds,
            max_replacements=max_replacements,
            buy_window=buy_window,
            sell_window=sell_window,
            window_timezone=window_timezone,
        )

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
        **kwargs,
    ) -> list[OrderIntent]:
        orders = self.base_model.create_orders(sleeve_id, portfolio, data, targets, **kwargs)
        if self.allow_sells:
            return orders
        return [order for order in orders if order.side is not OrderSide.SELL]


def create_execution_model(params):
    return SemiconductKorExecutionModel(
        tag_prefix=str(params.get("tag_prefix", "semiconduct-kor")),
        order_type=str(params.get("order_type", "limit")),
        time_in_force=str(params.get("time_in_force", "day")),
        limit_offset_bps=float(params.get("limit_offset_bps", 8.0)),
        max_slice_quantity=_optional_int(params.get("max_slice_quantity")),
        max_slice_notional=_optional_float(params.get("max_slice_notional"), default=1_500_000.0),
        max_slices=_optional_int(params.get("max_slices"), default=3),
        urgency=str(params.get("urgency", "normal")),
        max_order_age_seconds=_optional_float(params.get("max_order_age_seconds"), default=900.0),
        price_drift_bps=_optional_float(params.get("price_drift_bps"), default=80.0),
        min_replace_interval_seconds=_optional_float(params.get("min_replace_interval_seconds"), default=180.0),
        max_replacements=_optional_int(params.get("max_replacements"), default=2),
        allow_sells=_optional_bool(params.get("allow_sells"), default=False),
        buy_window=str(params.get("buy_window", "")),
        sell_window=str(params.get("sell_window", "")),
        window_timezone=str(params.get("window_timezone", "Asia/Seoul")),
    )


def _optional_int(value, *, default=None):
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _optional_float(value, *, default=None):
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def _optional_bool(value, *, default=False):
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

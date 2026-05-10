from __future__ import annotations

from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, TimeInForce
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol


class LeapsMomentumExecutionModel:
    def __init__(
        self,
        tag_prefix: str = "leaps",
        order_type: str = "limit",
        time_in_force: str = "day",
        buy_limit_offset_bps: float = 8.0,
        sell_limit_offset_bps: float = 15.0,
        stop_sell_limit_offset_bps: float = 35.0,
        max_slice_quantity: int | None = None,
        max_slice_notional: float | None = 2_000_000.0,
        max_slices: int | None = 3,
        max_daily_volume_participation_bps: float | None = 50.0,
        chase_guard_intraday_return_bps: float | None = 900.0,
        chase_guard_size_multiplier: float = 0.5,
    ) -> None:
        self.tag_prefix = tag_prefix
        self.order_type = OrderType(str(order_type or "limit").strip().lower())
        self.time_in_force = TimeInForce(str(time_in_force or "day").strip().lower())
        self.buy_limit_offset_bps = float(buy_limit_offset_bps)
        self.sell_limit_offset_bps = float(sell_limit_offset_bps)
        self.stop_sell_limit_offset_bps = float(stop_sell_limit_offset_bps)
        self.max_slice_quantity = max_slice_quantity
        self.max_slice_notional = max_slice_notional
        self.max_slices = max_slices
        self.max_daily_volume_participation_bps = max_daily_volume_participation_bps
        self.chase_guard_intraday_return_bps = chase_guard_intraday_return_bps
        self.chase_guard_size_multiplier = float(chase_guard_size_multiplier)

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
    ) -> list[OrderIntent]:
        orders: list[OrderIntent] = []
        for target in targets:
            bar = data.get(target.symbol)
            if bar is None or bar.close <= 0:
                continue
            current_quantity = portfolio.quantity(target.symbol)
            delta = target.quantity - current_quantity
            if delta == 0:
                continue

            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            parent_quantity = abs(delta)
            executable_quantity, execution_notes = self._execution_quantity(
                parent_quantity,
                side=side,
                bar=bar,
            )
            if executable_quantity <= 0:
                continue

            limit_offset_bps = self._limit_offset_bps(side, target.tag)
            limit_price = _limit_price(
                bar.close,
                side=side,
                order_type=self.order_type,
                limit_offset_bps=limit_offset_bps,
            )
            quantities = _split_quantity(
                executable_quantity,
                reference_price=bar.close,
                max_slice_quantity=self.max_slice_quantity,
                max_slice_notional=self.max_slice_notional,
                max_slices=self.max_slices,
            )
            submitted_quantity = sum(quantities)
            slice_count = len(quantities)
            for index, quantity in enumerate(quantities, start=1):
                orders.append(
                    OrderIntent(
                        sleeve_id=sleeve_id,
                        symbol=target.symbol,
                        side=side,
                        quantity=quantity,
                        reference_price=bar.close,
                        tag=f"{self.tag_prefix}:{currency_for_symbol(target.symbol).lower()}:{target.tag}",
                        order_type=self.order_type,
                        limit_price=limit_price,
                        time_in_force=self.time_in_force,
                        metadata={
                            "execution_style": "leaps_momentum",
                            "target_quantity": target.quantity,
                            "current_quantity": current_quantity,
                            "delta_quantity": delta,
                            "parent_quantity": parent_quantity,
                            "executable_quantity": executable_quantity,
                            "submitted_quantity": submitted_quantity,
                            "deferred_quantity": max(executable_quantity - submitted_quantity, 0),
                            "slice_index": index,
                            "slice_count": slice_count,
                            "limit_offset_bps": limit_offset_bps,
                            **execution_notes,
                        },
                    )
                )
        return orders

    def _execution_quantity(self, quantity: int, *, side: OrderSide, bar) -> tuple[int, dict[str, object]]:
        result = int(quantity)
        notes: dict[str, object] = {
            "volume": int(bar.volume or 0),
            "volume_participation_bps": self.max_daily_volume_participation_bps,
        }
        if side is OrderSide.BUY and self.chase_guard_intraday_return_bps is not None and bar.open > 0:
            intraday_return_bps = ((bar.close / bar.open) - 1.0) * 10_000.0
            notes["intraday_return_bps"] = intraday_return_bps
            if intraday_return_bps >= self.chase_guard_intraday_return_bps:
                result = max(1, int(result * self.chase_guard_size_multiplier))
                notes["chase_guard"] = "reduced_size"

        if self.max_daily_volume_participation_bps is not None and bar.volume > 0:
            volume_cap = max(1, int((bar.volume * self.max_daily_volume_participation_bps) // 10_000))
            if result > volume_cap:
                result = volume_cap
                notes["participation_cap"] = "clamped"
                notes["participation_cap_quantity"] = volume_cap
        return result, notes

    def _limit_offset_bps(self, side: OrderSide, target_tag: str) -> float:
        if side is OrderSide.BUY:
            return self.buy_limit_offset_bps
        tag = str(target_tag or "").lower()
        if "stop" in tag or "inactive" in tag or "flat" in tag:
            return self.stop_sell_limit_offset_bps
        return self.sell_limit_offset_bps


def create_execution_model(params):
    return LeapsMomentumExecutionModel(
        tag_prefix=str(params.get("tag_prefix", "leaps")),
        order_type=str(params.get("order_type", "limit")),
        time_in_force=str(params.get("time_in_force", "day")),
        buy_limit_offset_bps=float(params.get("buy_limit_offset_bps", params.get("limit_offset_bps", 8.0))),
        sell_limit_offset_bps=float(params.get("sell_limit_offset_bps", 15.0)),
        stop_sell_limit_offset_bps=float(params.get("stop_sell_limit_offset_bps", 35.0)),
        max_slice_quantity=_optional_int(params.get("max_slice_quantity")),
        max_slice_notional=_optional_float(params.get("max_slice_notional"), default=2_000_000.0),
        max_slices=_optional_int(params.get("max_slices"), default=3),
        max_daily_volume_participation_bps=_optional_float(params.get("max_daily_volume_participation_bps"), default=50.0),
        chase_guard_intraday_return_bps=_optional_float(params.get("chase_guard_intraday_return_bps"), default=900.0),
        chase_guard_size_multiplier=float(params.get("chase_guard_size_multiplier", 0.5)),
    )


def _limit_price(
    reference_price: float,
    *,
    side: OrderSide,
    order_type: OrderType,
    limit_offset_bps: float,
) -> float | None:
    if order_type is OrderType.MARKET:
        return None
    offset = float(limit_offset_bps) / 10_000.0
    if side is OrderSide.BUY:
        return max(0.0, reference_price * (1.0 + offset))
    return max(0.0, reference_price * (1.0 - offset))


def _split_quantity(
    quantity: int,
    *,
    reference_price: float,
    max_slice_quantity: int | None,
    max_slice_notional: float | None,
    max_slices: int | None,
) -> tuple[int, ...]:
    quantity = int(quantity)
    if quantity <= 0:
        return ()
    slice_cap = quantity
    if max_slice_quantity is not None:
        slice_cap = min(slice_cap, int(max_slice_quantity))
    if max_slice_notional is not None and reference_price > 0:
        slice_cap = min(slice_cap, max(1, int(float(max_slice_notional) // reference_price)))
    slice_cap = max(1, slice_cap)
    chunks: list[int] = []
    remaining = quantity
    while remaining > 0:
        if max_slices is not None and len(chunks) >= max_slices:
            break
        chunk = min(slice_cap, remaining)
        chunks.append(chunk)
        remaining -= chunk
    return tuple(chunks)


def _optional_int(value, *, default=None):
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _optional_float(value, *, default=None):
    if value is None or str(value).strip() == "":
        return default
    return float(value)


LeapsImmediateExecutionModel = LeapsMomentumExecutionModel

from __future__ import annotations

from typing import Any

from leaps_quant_engine.execution import ExecutionContext
from leaps_quant_engine.market_rules import MarketSession, round_krx_price_to_tick
from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, TimeInForce
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol


_REGULAR_AUCTION_PHASES = frozenset({"regular_open_auction", "regular_close_auction"})
_EXTENDED_SESSION_PHASES = frozenset({"pre_open_after_hours", "after_hours_close", "pre_market", "after_market"})
_BLOCKED_SINGLE_PRICE_PHASE = "after_hours_single_price"


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
        regular_auction_buy_multiplier: float = 0.65,
        regular_auction_sell_multiplier: float = 1.0,
        extended_session_buy_multiplier: float = 0.35,
        extended_session_sell_multiplier: float = 1.0,
        block_after_hours_single_price: bool = True,
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
        self.regular_auction_buy_multiplier = float(regular_auction_buy_multiplier)
        self.regular_auction_sell_multiplier = float(regular_auction_sell_multiplier)
        self.extended_session_buy_multiplier = float(extended_session_buy_multiplier)
        self.extended_session_sell_multiplier = float(extended_session_sell_multiplier)
        self.block_after_hours_single_price = bool(block_after_hours_single_price)

    def create_orders(
        self,
        sleeve_id: str,
        portfolio: Portfolio,
        data: DataSlice,
        targets: list[PortfolioTarget],
        execution_context: ExecutionContext | None = None,
        market_session: MarketSession | None = None,
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
                session=_session_for_target(target, execution_context=execution_context, market_session=market_session),
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
            if limit_price is not None and currency_for_symbol(target.symbol) == "KRW":
                limit_price = float(round_krx_price_to_tick(limit_price, side=side))
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

    def _execution_quantity(
        self,
        quantity: int,
        *,
        side: OrderSide,
        bar,
        session: MarketSession | None,
    ) -> tuple[int, dict[str, object]]:
        result = int(quantity)
        notes: dict[str, object] = {
            "volume": int(bar.volume or 0),
            "volume_participation_bps": self.max_daily_volume_participation_bps,
        }
        session_multiplier, session_notes = self._session_quantity_policy(side=side, session=session)
        notes.update(session_notes)
        if session_multiplier <= 0.0:
            return 0, notes
        if session_multiplier < 1.0:
            notes["session_original_quantity"] = result
            result = max(1, int(result * session_multiplier))
            notes["session_quantity_clamp"] = "reduced_size"

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

    def _session_quantity_policy(self, *, side: OrderSide, session: MarketSession | None) -> tuple[float, dict[str, object]]:
        if session is None:
            return 1.0, {}
        notes: dict[str, object] = {
            "session_phase": session.session_phase,
            "session_orderable": session.is_orderable,
            "session_regular_open": session.is_regular_market_open,
        }
        if not session.is_orderable:
            notes["session_policy"] = "blocked_not_orderable"
            return 0.0, notes
        if session.session_phase == _BLOCKED_SINGLE_PRICE_PHASE and self.block_after_hours_single_price:
            notes["session_policy"] = "blocked_after_hours_single_price"
            return 0.0, notes
        if session.session_phase in _REGULAR_AUCTION_PHASES:
            notes["session_policy"] = "regular_auction"
            multiplier = self.regular_auction_buy_multiplier if side is OrderSide.BUY else self.regular_auction_sell_multiplier
            notes["session_quantity_multiplier"] = multiplier
            return max(multiplier, 0.0), notes
        if session.session_phase in _EXTENDED_SESSION_PHASES:
            notes["session_policy"] = "extended_session"
            multiplier = self.extended_session_buy_multiplier if side is OrderSide.BUY else self.extended_session_sell_multiplier
            notes["session_quantity_multiplier"] = multiplier
            return max(multiplier, 0.0), notes
        notes["session_policy"] = "regular"
        notes["session_quantity_multiplier"] = 1.0
        return 1.0, notes

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
        regular_auction_buy_multiplier=float(params.get("regular_auction_buy_multiplier", 0.65)),
        regular_auction_sell_multiplier=float(params.get("regular_auction_sell_multiplier", 1.0)),
        extended_session_buy_multiplier=float(params.get("extended_session_buy_multiplier", 0.35)),
        extended_session_sell_multiplier=float(params.get("extended_session_sell_multiplier", 1.0)),
        block_after_hours_single_price=_bool(params.get("block_after_hours_single_price"), default=True),
    )


def _session_for_target(
    target: PortfolioTarget,
    *,
    execution_context: ExecutionContext | None,
    market_session: MarketSession | None,
) -> MarketSession | None:
    if execution_context is not None:
        return execution_context.session_for_symbol(target.symbol)
    return market_session


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


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


LeapsImmediateExecutionModel = LeapsMomentumExecutionModel

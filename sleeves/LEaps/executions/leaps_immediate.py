from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from math import ceil
from typing import Any

from leaps_quant_engine.execution import ExecutionContext, PendingOrderState
from leaps_quant_engine.market_rules import MarketSession, round_krx_price_to_tick
from leaps_quant_engine.models import DataSlice, OrderIntent, OrderSide, OrderType, PortfolioTarget, TimeInForce
from leaps_quant_engine.portfolio import Portfolio, currency_for_symbol
from leaps_quant_engine.runtime_state import StatePatch


_REGULAR_AUCTION_PHASES = frozenset({"regular_open_auction", "regular_close_auction"})
_EXTENDED_SESSION_PHASES = frozenset({"pre_open_after_hours", "after_hours_close", "pre_market", "after_market"})
_BLOCKED_SINGLE_PRICE_PHASE = "after_hours_single_price"
MODEL_ID = "leaps-v4.3-notional-band-execution"
MODEL_VERSION = "4.4.1"
TARGET_STATE_NAMESPACE = "target_fulfillment"
RISK_STATE_MODEL_ID = "leaps-kospi-growth-us-hedge-risk"
RISK_STATE_NAMESPACE = "symbol_guard"


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
        dynamic_slice_notional_enabled: bool = False,
        dynamic_slice_equity_pct: float | None = 0.20,
        dynamic_slice_min_notional: float | None = 1_000_000.0,
        dynamic_slice_max_notional: float | None = 5_000_000.0,
        dynamic_slice_liquidity_bps: float | None = 8.0,
        max_slices: int | None = 3,
        max_daily_volume_participation_bps: float | None = 50.0,
        auction_volume_participation_enabled: bool = True,
        volume_participation_use_liquidity_notional: bool = False,
        volume_participation_min_notional: float | None = None,
        chase_guard_intraday_return_bps: float | None = 900.0,
        chase_guard_size_multiplier: float = 0.5,
        regular_auction_buy_multiplier: float = 0.65,
        regular_auction_sell_multiplier: float = 1.0,
        extended_session_buy_multiplier: float = 0.35,
        extended_session_sell_multiplier: float = 1.0,
        block_after_hours_single_price: bool = True,
        model_id: str = MODEL_ID,
        model_version: str = MODEL_VERSION,
        target_state_namespace: str = TARGET_STATE_NAMESPACE,
        reused_target_suppress_buy_add: bool = False,
        reused_target_sell_no_trade_max_quantity_delta: int = 2,
        reused_target_sell_no_trade_max_notional: float = 300_000.0,
        reused_target_sell_no_trade_pct_of_target: float = 0.05,
        anti_oscillation_enabled: bool = False,
        notional_rebalance_band_enabled: bool = False,
        rebalance_no_trade_min_notional: float = 0.0,
        rebalance_no_trade_pct_of_target: float = 0.0,
        opposite_rebalance_cooldown_minutes: float = 60.0,
        opposite_rebalance_require_small_change: bool = True,
        same_source_opposite_rebalance_guard: bool = True,
        opposite_rebalance_no_trade_max_quantity_delta: int = 2,
        opposite_rebalance_no_trade_max_notional: float = 300_000.0,
        opposite_rebalance_no_trade_pct_of_position: float = 0.05,
        risk_reentry_cooldown_minutes: float = 60.0,
        risk_state_model_id: str = RISK_STATE_MODEL_ID,
        risk_state_namespace: str = RISK_STATE_NAMESPACE,
    ) -> None:
        self.tag_prefix = tag_prefix
        self.order_type = OrderType(str(order_type or "limit").strip().lower())
        self.time_in_force = TimeInForce(str(time_in_force or "day").strip().lower())
        self.buy_limit_offset_bps = float(buy_limit_offset_bps)
        self.sell_limit_offset_bps = float(sell_limit_offset_bps)
        self.stop_sell_limit_offset_bps = float(stop_sell_limit_offset_bps)
        self.max_slice_quantity = max_slice_quantity
        self.max_slice_notional = max_slice_notional
        self.dynamic_slice_notional_enabled = bool(dynamic_slice_notional_enabled)
        self.dynamic_slice_equity_pct = dynamic_slice_equity_pct
        self.dynamic_slice_min_notional = dynamic_slice_min_notional
        self.dynamic_slice_max_notional = dynamic_slice_max_notional
        self.dynamic_slice_liquidity_bps = dynamic_slice_liquidity_bps
        self.max_slices = max_slices
        self.max_daily_volume_participation_bps = max_daily_volume_participation_bps
        self.auction_volume_participation_enabled = bool(auction_volume_participation_enabled)
        self.volume_participation_use_liquidity_notional = bool(volume_participation_use_liquidity_notional)
        self.volume_participation_min_notional = volume_participation_min_notional
        self.chase_guard_intraday_return_bps = chase_guard_intraday_return_bps
        self.chase_guard_size_multiplier = float(chase_guard_size_multiplier)
        self.regular_auction_buy_multiplier = float(regular_auction_buy_multiplier)
        self.regular_auction_sell_multiplier = float(regular_auction_sell_multiplier)
        self.extended_session_buy_multiplier = float(extended_session_buy_multiplier)
        self.extended_session_sell_multiplier = float(extended_session_sell_multiplier)
        self.block_after_hours_single_price = bool(block_after_hours_single_price)
        self.model_id = str(model_id or MODEL_ID)
        self.model_version = str(model_version or MODEL_VERSION)
        self.target_state_namespace = str(target_state_namespace or TARGET_STATE_NAMESPACE)
        self.reused_target_suppress_buy_add = bool(reused_target_suppress_buy_add)
        self.reused_target_sell_no_trade_max_quantity_delta = max(0, int(reused_target_sell_no_trade_max_quantity_delta))
        self.reused_target_sell_no_trade_max_notional = max(0.0, float(reused_target_sell_no_trade_max_notional))
        self.reused_target_sell_no_trade_pct_of_target = max(0.0, float(reused_target_sell_no_trade_pct_of_target))
        self.anti_oscillation_enabled = bool(anti_oscillation_enabled)
        self.notional_rebalance_band_enabled = bool(notional_rebalance_band_enabled)
        self.rebalance_no_trade_min_notional = max(0.0, float(rebalance_no_trade_min_notional))
        self.rebalance_no_trade_pct_of_target = max(0.0, float(rebalance_no_trade_pct_of_target))
        self.opposite_rebalance_cooldown_minutes = max(0.0, float(opposite_rebalance_cooldown_minutes))
        self.opposite_rebalance_require_small_change = bool(opposite_rebalance_require_small_change)
        self.same_source_opposite_rebalance_guard = bool(same_source_opposite_rebalance_guard)
        self.opposite_rebalance_no_trade_max_quantity_delta = max(0, int(opposite_rebalance_no_trade_max_quantity_delta))
        self.opposite_rebalance_no_trade_max_notional = max(0.0, float(opposite_rebalance_no_trade_max_notional))
        self.opposite_rebalance_no_trade_pct_of_position = max(0.0, float(opposite_rebalance_no_trade_pct_of_position))
        self.risk_reentry_cooldown_minutes = max(0.0, float(risk_reentry_cooldown_minutes))
        self.risk_state_model_id = str(risk_state_model_id or RISK_STATE_MODEL_ID)
        self.risk_state_namespace = str(risk_state_namespace or RISK_STATE_NAMESPACE)
        self._last_target_notes: dict[str, dict[str, object]] = {}

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
        self._last_target_notes = {}
        same_cycle_sell_symbols = self._same_cycle_sell_symbols(portfolio, targets)
        for target in targets:
            bar = data.get(target.symbol)
            if bar is None or bar.close <= 0:
                continue
            current_quantity = portfolio.quantity(target.symbol)
            delta = target.quantity - current_quantity
            if delta == 0:
                continue

            side = OrderSide.BUY if delta > 0 else OrderSide.SELL
            pending_orders = execution_context.pending_orders if execution_context is not None else PendingOrderState()
            bypass_unordered_quantity = _bypass_unordered_quantity_guard(target, delta)
            unordered_delta = (
                delta
                if bypass_unordered_quantity
                else pending_orders.unordered_delta(
                    target.symbol,
                    target_quantity=target.quantity,
                    current_quantity=current_quantity,
                )
            )
            if unordered_delta == 0:
                continue
            unordered_side = OrderSide.BUY if unordered_delta > 0 else OrderSide.SELL
            if unordered_side is not side and not bypass_unordered_quantity:
                continue
            reused_target = self._same_source_target_seen(execution_context, target)
            suppression_notes: dict[str, object] = {}
            suppression_reason = None
            if (
                self.anti_oscillation_enabled
                and unordered_side is OrderSide.BUY
                and target.symbol.key.upper() in same_cycle_sell_symbols
                and not _is_forced_or_risk_target(target)
            ):
                suppression_reason = "same_cycle_opposite_target_conflict"
            if suppression_reason is None:
                suppression_reason = self._reused_target_suppression_reason(
                    target,
                    unordered_delta=unordered_delta,
                    current_quantity=current_quantity,
                    reference_price=float(bar.close),
                    reused_target=reused_target,
                    bypass_unordered_quantity=bypass_unordered_quantity,
                )
            if suppression_reason is None:
                suppression_reason, suppression_notes = self._notional_rebalance_suppression_reason(
                    target,
                    unordered_delta=unordered_delta,
                    current_quantity=current_quantity,
                    reference_price=float(bar.close),
                    bypass_unordered_quantity=bypass_unordered_quantity,
                )
            if suppression_reason is None:
                suppression_reason = self._anti_oscillation_suppression_reason(
                    execution_context,
                    target,
                    unordered_delta=unordered_delta,
                    current_quantity=current_quantity,
                    reference_price=float(bar.close),
                    bypass_unordered_quantity=bypass_unordered_quantity,
                )
            if suppression_reason is not None:
                self._last_target_notes[target.symbol.key.upper()] = {
                    "suppressed_this_cycle": True,
                    "suppression_reason": suppression_reason,
                    "suppressed_side": unordered_side.value,
                    "suppressed_quantity": abs(int(unordered_delta)),
                    "suppressed_notional": abs(int(unordered_delta)) * float(bar.close),
                    **suppression_notes,
                }
                continue

            parent_quantity = abs(unordered_delta)
            executable_quantity, execution_notes = self._execution_quantity(
                parent_quantity,
                side=unordered_side,
                bar=bar,
                session=_session_for_target(target, execution_context=execution_context, market_session=market_session),
            )
            if executable_quantity <= 0:
                continue

            limit_offset_bps = self._limit_offset_bps(unordered_side, target.tag)
            limit_price = _limit_price(
                bar.close,
                side=unordered_side,
                order_type=self.order_type,
                limit_offset_bps=limit_offset_bps,
            )
            if limit_price is not None and currency_for_symbol(target.symbol) == "KRW":
                limit_price = float(round_krx_price_to_tick(limit_price, side=unordered_side))
            max_slice_notional, slice_notes = self._slice_notional_policy(portfolio, data, bar)
            quantities = _split_quantity(
                executable_quantity,
                reference_price=bar.close,
                max_slice_quantity=self.max_slice_quantity,
                max_slice_notional=max_slice_notional,
                max_slices=self.max_slices,
            )
            submitted_quantity = sum(quantities)
            slice_count = len(quantities)
            for index, quantity in enumerate(quantities, start=1):
                orders.append(
                    OrderIntent(
                        sleeve_id=sleeve_id,
                        symbol=target.symbol,
                        side=unordered_side,
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
                            "raw_delta_quantity": delta,
                            "unordered_delta_quantity": unordered_delta,
                            "unordered_quantity_bypassed": bypass_unordered_quantity,
                            "reused_target_suppression_enabled": self.reused_target_suppress_buy_add,
                            "reused_source_target_seen": reused_target,
                            "anti_oscillation_enabled": self.anti_oscillation_enabled,
                            "notional_rebalance_band_enabled": self.notional_rebalance_band_enabled,
                            "execution_model_version": self.model_version,
                            "target_tag": target.tag,
                            "parent_quantity": parent_quantity,
                            "executable_quantity": executable_quantity,
                            "submitted_quantity": submitted_quantity,
                            "deferred_quantity": max(executable_quantity - submitted_quantity, 0),
                            "slice_index": index,
                            "slice_count": slice_count,
                            "limit_offset_bps": limit_offset_bps,
                            "target_batch_id": execution_context.target_batch_id if execution_context else "",
                            "source_target_batch_id": execution_context.source_target_batch_id if execution_context else "",
                            "target_lifecycle": "order_intent_created",
                            **pending_orders.symbol_metadata(
                                target.symbol,
                                current_quantity=current_quantity,
                                target_quantity=target.quantity,
                            ),
                            **execution_notes,
                            **slice_notes,
                        },
                    )
                )
        return orders

    def state_patches(
        self,
        *,
        context: ExecutionContext,
        orders: tuple[OrderIntent, ...],
    ) -> tuple[StatePatch, ...]:
        patches: list[StatePatch] = []
        orders_by_symbol: dict[str, list[OrderIntent]] = {}
        for order in orders:
            orders_by_symbol.setdefault(order.symbol.key.upper(), []).append(order)
        for target in context.approved_targets:
            symbol_key = target.symbol.key.upper()
            previous = self._target_state_value(context, target)
            symbol_orders = tuple(orders_by_symbol.get(symbol_key, ()))
            ordered_quantity = sum(order.quantity for order in symbol_orders)
            order_side = _single_order_side(symbol_orders)
            notes = self._last_target_notes.get(symbol_key, {})
            payload = dict(previous)
            payload.update(
                {
                    "model_version": self.model_version,
                    "source_target_batch_id": context.source_target_batch_id,
                    "target_batch_id": context.target_batch_id,
                    "target_quantity": int(target.quantity),
                    "current_quantity": int(context.portfolio.quantity(target.symbol)),
                    "ordered_this_cycle": bool(symbol_orders),
                    "suppressed_this_cycle": bool(notes.get("suppressed_this_cycle")),
                    "suppression_reason": notes.get("suppression_reason"),
                    "suppressed_side": notes.get("suppressed_side"),
                    "suppressed_quantity": notes.get("suppressed_quantity"),
                    "suppressed_notional": notes.get("suppressed_notional"),
                    "suppressed_threshold_notional": notes.get("suppressed_threshold_notional"),
                    "suppressed_target_notional": notes.get("suppressed_target_notional"),
                    "suppressed_current_notional": notes.get("suppressed_current_notional"),
                    "last_seen_at": context.generated_at.isoformat(),
                }
            )
            if symbol_orders and order_side is not None:
                payload.update(
                    {
                        "last_order_side": order_side.value,
                        "last_order_quantity": int(ordered_quantity),
                        "last_ordered_at": context.generated_at.isoformat(),
                        "last_order_target_quantity": int(target.quantity),
                        "last_order_source_target_batch_id": context.source_target_batch_id,
                        "last_order_reference_price": float(symbol_orders[0].reference_price),
                    }
                )
                if order_side is OrderSide.BUY:
                    payload.update(
                        {
                            "last_add_at": context.generated_at.isoformat(),
                            "last_add_quantity": int(ordered_quantity),
                            "last_add_target_quantity": int(target.quantity),
                        }
                    )
                if order_side is OrderSide.SELL:
                    payload.update(
                        {
                            "last_reduction_at": context.generated_at.isoformat(),
                            "last_reduction_quantity": int(ordered_quantity),
                            "last_reduction_target_quantity": int(target.quantity),
                            "last_reduction_reason": _target_reduction_reason(target),
                        }
                    )
            patches.append(
                StatePatch(
                    key=context.model_state.key(
                        model_id=self.model_id,
                        namespace=self.target_state_namespace,
                        symbol_key=symbol_key,
                    ),
                    value=payload,
                    reason="leaps_execution_target_seen",
                )
            )
        return tuple(patches)

    def _target_state_value(
        self,
        context: ExecutionContext | None,
        target: PortfolioTarget,
    ) -> dict[str, object]:
        if context is None:
            return {}
        record = context.model_state.get(
            model_id=self.model_id,
            namespace=self.target_state_namespace,
            symbol_key=target.symbol.key.upper(),
        )
        if record is not None and isinstance(record.value, Mapping):
            return dict(record.value)
        return {}

    def _same_cycle_sell_symbols(self, portfolio: Portfolio, targets: list[PortfolioTarget]) -> set[str]:
        symbols: set[str] = set()
        for target in targets:
            current_quantity = portfolio.quantity(target.symbol)
            if current_quantity > 0 and target.quantity < current_quantity:
                symbols.add(target.symbol.key.upper())
        return symbols

    def _same_source_target_seen(
        self,
        context: ExecutionContext | None,
        target: PortfolioTarget,
    ) -> bool:
        if context is None or not context.source_target_batch_id:
            return False
        value = self._target_state_value(context, target)
        return str(value.get("source_target_batch_id") or "") == context.source_target_batch_id

    def _reused_target_suppression_reason(
        self,
        target: PortfolioTarget,
        *,
        unordered_delta: int,
        current_quantity: int,
        reference_price: float,
        reused_target: bool,
        bypass_unordered_quantity: bool,
    ) -> str | None:
        if not reused_target or bypass_unordered_quantity:
            return None
        if unordered_delta > 0 and self.reused_target_suppress_buy_add:
            return "reused_target_buy_add"
        if unordered_delta >= 0:
            return None
        if current_quantity <= 0 or target.quantity <= 0:
            return None
        quantity_delta = abs(int(unordered_delta))
        target_quantity = max(abs(int(target.quantity)), 1)
        notional_delta = quantity_delta * max(0.0, float(reference_price))
        pct_delta = quantity_delta / target_quantity
        if (
            quantity_delta <= self.reused_target_sell_no_trade_max_quantity_delta
            or notional_delta <= self.reused_target_sell_no_trade_max_notional
            or pct_delta <= self.reused_target_sell_no_trade_pct_of_target
        ):
            return "reused_target_small_sell"
        return None

    def _notional_rebalance_suppression_reason(
        self,
        target: PortfolioTarget,
        *,
        unordered_delta: int,
        current_quantity: int,
        reference_price: float,
        bypass_unordered_quantity: bool,
    ) -> tuple[str | None, dict[str, object]]:
        if (
            not self.notional_rebalance_band_enabled
            or unordered_delta == 0
            or current_quantity <= 0
            or target.quantity <= 0
            or reference_price <= 0
            or bypass_unordered_quantity
            or _is_forced_or_risk_target(target)
        ):
            return None, {}
        quantity_delta = abs(int(unordered_delta))
        notional_delta = quantity_delta * float(reference_price)
        current_notional = abs(int(current_quantity)) * float(reference_price)
        target_notional = abs(int(target.quantity)) * float(reference_price)
        base_notional = max(current_notional, target_notional, 0.0)
        threshold = max(
            self.rebalance_no_trade_min_notional,
            base_notional * self.rebalance_no_trade_pct_of_target,
        )
        notes = {
            "suppressed_threshold_notional": threshold,
            "suppressed_target_notional": target_notional,
            "suppressed_current_notional": current_notional,
        }
        if threshold <= 0:
            return None, notes
        if notional_delta < threshold:
            return "rebalance_notional_no_trade_band", notes
        return None, notes

    def _anti_oscillation_suppression_reason(
        self,
        context: ExecutionContext | None,
        target: PortfolioTarget,
        *,
        unordered_delta: int,
        current_quantity: int,
        reference_price: float,
        bypass_unordered_quantity: bool,
    ) -> str | None:
        if (
            not self.anti_oscillation_enabled
            or context is None
            or unordered_delta == 0
            or bypass_unordered_quantity
            or _is_forced_or_risk_target(target)
        ):
            return None
        side = OrderSide.BUY if unordered_delta > 0 else OrderSide.SELL
        if side is OrderSide.BUY:
            risk_reason = self._risk_reentry_suppression_reason(context, target)
            if risk_reason is not None:
                return risk_reason

        previous = self._target_state_value(context, target)
        if side is OrderSide.BUY:
            risk_reason = self._target_state_reentry_suppression_reason(context, previous)
            if risk_reason is not None:
                return risk_reason
        last_side = _order_side_from_text(previous.get("last_order_side"))
        if last_side is None or last_side is side:
            return None
        if (
            self.same_source_opposite_rebalance_guard
            and context.source_target_batch_id
            and str(previous.get("last_order_source_target_batch_id") or "") == context.source_target_batch_id
        ):
            return "same_source_opposite_rebalance"
        last_ordered_at = _parse_datetime(previous.get("last_ordered_at"))
        if not _within_minutes(last_ordered_at, context.generated_at, self.opposite_rebalance_cooldown_minutes):
            return None
        if not self.opposite_rebalance_require_small_change:
            return "opposite_rebalance_cooldown"
        if not self._small_opposite_rebalance(
            unordered_delta=unordered_delta,
            current_quantity=current_quantity,
            target_quantity=target.quantity,
            reference_price=reference_price,
        ):
            return None
        return "opposite_rebalance_cooldown"

    def _risk_reentry_suppression_reason(
        self,
        context: ExecutionContext,
        target: PortfolioTarget,
    ) -> str | None:
        if self.risk_reentry_cooldown_minutes <= 0:
            return None
        record = context.model_state.get(
            sleeve_id=context.sleeve_id,
            model_id=self.risk_state_model_id,
            namespace=self.risk_state_namespace,
            symbol_key=target.symbol.key.upper(),
        )
        if record is None or not isinstance(record.value, Mapping):
            return None
        value = dict(record.value)
        status = str(value.get("status") or "").strip().lower()
        last_risk_status = str(value.get("last_risk_status") or status).strip().lower()
        if status in {"reduced", "exited", "recovering"}:
            risk_event_at = _parse_datetime(value.get("last_risk_event_at")) or _parse_datetime(value.get("updated_at"))
        elif last_risk_status in {"reduced", "exited"}:
            risk_event_at = _parse_datetime(value.get("last_risk_event_at"))
        else:
            return None
        if not _within_minutes(risk_event_at, context.generated_at, self.risk_reentry_cooldown_minutes):
            return None
        return "risk_guard_reentry_cooldown"

    def _target_state_reentry_suppression_reason(
        self,
        context: ExecutionContext,
        previous: Mapping[str, object],
    ) -> str | None:
        if self.risk_reentry_cooldown_minutes <= 0:
            return None
        reason = str(previous.get("last_reduction_reason") or "").strip().lower()
        if not reason or reason == "rebalance":
            return None
        reduced_at = _parse_datetime(previous.get("last_reduction_at"))
        if not _within_minutes(reduced_at, context.generated_at, self.risk_reentry_cooldown_minutes):
            return None
        return "risk_guard_reentry_cooldown"

    def _small_opposite_rebalance(
        self,
        *,
        unordered_delta: int,
        current_quantity: int,
        target_quantity: int,
        reference_price: float,
    ) -> bool:
        quantity_delta = abs(int(unordered_delta))
        notional_delta = quantity_delta * max(0.0, float(reference_price))
        base_quantity = max(abs(int(current_quantity)), abs(int(target_quantity)), 1)
        base_notional = base_quantity * max(0.0, float(reference_price))
        pct_delta = quantity_delta / base_quantity
        threshold = max(
            self.opposite_rebalance_no_trade_max_notional,
            base_notional * self.opposite_rebalance_no_trade_pct_of_position,
        )
        if threshold > 0 and notional_delta < threshold:
            return True
        return (
            self.opposite_rebalance_no_trade_max_quantity_delta > 0
            and quantity_delta <= self.opposite_rebalance_no_trade_max_quantity_delta
            and pct_delta <= self.opposite_rebalance_no_trade_pct_of_position
        )

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

        if (
            session is not None
            and session.session_phase in _REGULAR_AUCTION_PHASES
            and not self.auction_volume_participation_enabled
        ):
            notes["participation_cap"] = "skipped_auction_phase"
        elif self.max_daily_volume_participation_bps is not None:
            participation_volume, participation_notes = self._participation_volume(bar)
            notes.update(participation_notes)
            if participation_volume <= 0:
                return result, notes
            raw_volume_cap = max(1, int((participation_volume * self.max_daily_volume_participation_bps) // 10_000))
            volume_cap = raw_volume_cap
            min_notional = _positive_float_or_none(self.volume_participation_min_notional)
            if min_notional is not None and bar.close > 0:
                floor_quantity = max(1, int(ceil(min_notional / float(bar.close))))
                notes["participation_cap_min_notional"] = min_notional
                notes["participation_cap_min_quantity"] = floor_quantity
                if volume_cap < floor_quantity:
                    notes["participation_cap_floor"] = "min_notional"
                    notes["participation_cap_quantity_before_floor"] = volume_cap
                    volume_cap = floor_quantity
            notes["participation_cap_quantity"] = volume_cap
            if result > volume_cap:
                result = volume_cap
                notes["participation_cap"] = "clamped"
        return result, notes

    def _participation_volume(self, bar) -> tuple[int, dict[str, object]]:
        if self.volume_participation_use_liquidity_notional and bar.close > 0:
            liquidity_notional, liquidity_source = _liquidity_notional(bar)
            if liquidity_notional is not None and liquidity_notional > 0:
                volume = max(1, int(liquidity_notional / float(bar.close)))
                return volume, {
                    "participation_volume": volume,
                    "participation_volume_source": liquidity_source,
                    "participation_liquidity_notional": liquidity_notional,
                }
        return int(bar.volume or 0), {
            "participation_volume": int(bar.volume or 0),
            "participation_volume_source": "bar_volume",
        }

    def _slice_notional_policy(
        self,
        portfolio: Portfolio,
        data: DataSlice,
        bar,
    ) -> tuple[float | None, dict[str, object]]:
        fallback = self.max_slice_notional
        if not self.dynamic_slice_notional_enabled:
            return fallback, {
                "slice_notional_policy": "static",
                "slice_notional_cap": fallback,
            }

        currency = currency_for_symbol(bar.symbol)
        equity = portfolio.equity_for_currency(currency, data)
        equity_pct = _positive_float_or_none(self.dynamic_slice_equity_pct)
        min_notional = _positive_float_or_none(self.dynamic_slice_min_notional)
        max_notional = _positive_float_or_none(self.dynamic_slice_max_notional)
        liquidity_bps = _positive_float_or_none(self.dynamic_slice_liquidity_bps)

        notes: dict[str, object] = {
            "slice_notional_policy": "dynamic",
            "slice_equity": equity,
            "slice_equity_pct": equity_pct,
        }
        if equity > 0 and equity_pct is not None:
            cap = equity * equity_pct
            notes["slice_notional_source"] = "equity_pct"
        else:
            cap = fallback
            notes["slice_notional_source"] = "fallback"

        if cap is None:
            notes["slice_notional_cap"] = None
            return None, notes
        cap = float(cap)
        if max_notional is not None:
            cap = min(cap, max_notional)
        if min_notional is not None:
            cap = max(cap, min_notional)

        liquidity_notional, liquidity_source = _liquidity_notional(bar)
        if liquidity_notional is not None and liquidity_bps is not None:
            liquidity_cap = liquidity_notional * (liquidity_bps / 10_000.0)
            cap = min(cap, liquidity_cap)
            notes["slice_liquidity_notional"] = liquidity_notional
            notes["slice_liquidity_source"] = liquidity_source
            notes["slice_liquidity_bps"] = liquidity_bps
            notes["slice_liquidity_cap"] = liquidity_cap

        cap = max(cap, float(bar.close)) if bar.close > 0 else max(cap, 0.0)
        notes["slice_notional_cap"] = cap
        return cap, notes

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
        dynamic_slice_notional_enabled=_bool(params.get("dynamic_slice_notional_enabled"), default=False),
        dynamic_slice_equity_pct=_optional_float(params.get("dynamic_slice_equity_pct"), default=0.20),
        dynamic_slice_min_notional=_optional_float(params.get("dynamic_slice_min_notional"), default=1_000_000.0),
        dynamic_slice_max_notional=_optional_float(params.get("dynamic_slice_max_notional"), default=5_000_000.0),
        dynamic_slice_liquidity_bps=_optional_float(params.get("dynamic_slice_liquidity_bps"), default=8.0),
        max_slices=_optional_int(params.get("max_slices"), default=3),
        max_daily_volume_participation_bps=_optional_float(params.get("max_daily_volume_participation_bps"), default=50.0),
        auction_volume_participation_enabled=_bool(params.get("auction_volume_participation_enabled"), default=True),
        volume_participation_use_liquidity_notional=_bool(
            params.get("volume_participation_use_liquidity_notional"),
            default=False,
        ),
        volume_participation_min_notional=_optional_float(params.get("volume_participation_min_notional")),
        chase_guard_intraday_return_bps=_optional_float(params.get("chase_guard_intraday_return_bps"), default=900.0),
        chase_guard_size_multiplier=float(params.get("chase_guard_size_multiplier", 0.5)),
        regular_auction_buy_multiplier=float(params.get("regular_auction_buy_multiplier", 0.65)),
        regular_auction_sell_multiplier=float(params.get("regular_auction_sell_multiplier", 1.0)),
        extended_session_buy_multiplier=float(params.get("extended_session_buy_multiplier", 0.35)),
        extended_session_sell_multiplier=float(params.get("extended_session_sell_multiplier", 1.0)),
        block_after_hours_single_price=_bool(params.get("block_after_hours_single_price"), default=True),
        model_id=str(params.get("model_id", MODEL_ID)),
        model_version=str(params.get("model_version", MODEL_VERSION)),
        target_state_namespace=str(params.get("target_state_namespace", TARGET_STATE_NAMESPACE)),
        reused_target_suppress_buy_add=_bool(params.get("reused_target_suppress_buy_add"), default=False),
        reused_target_sell_no_trade_max_quantity_delta=int(
            params.get("reused_target_sell_no_trade_max_quantity_delta", 2)
        ),
        reused_target_sell_no_trade_max_notional=float(
            params.get("reused_target_sell_no_trade_max_notional", 300_000.0)
        ),
        reused_target_sell_no_trade_pct_of_target=float(
            params.get("reused_target_sell_no_trade_pct_of_target", 0.05)
        ),
        anti_oscillation_enabled=_bool(params.get("anti_oscillation_enabled"), default=False),
        notional_rebalance_band_enabled=_bool(params.get("notional_rebalance_band_enabled"), default=False),
        rebalance_no_trade_min_notional=float(params.get("rebalance_no_trade_min_notional", 0.0)),
        rebalance_no_trade_pct_of_target=float(params.get("rebalance_no_trade_pct_of_target", 0.0)),
        opposite_rebalance_cooldown_minutes=float(params.get("opposite_rebalance_cooldown_minutes", 60.0)),
        opposite_rebalance_require_small_change=_bool(
            params.get("opposite_rebalance_require_small_change"),
            default=True,
        ),
        same_source_opposite_rebalance_guard=_bool(
            params.get("same_source_opposite_rebalance_guard"),
            default=True,
        ),
        opposite_rebalance_no_trade_max_quantity_delta=int(
            params.get("opposite_rebalance_no_trade_max_quantity_delta", 2)
        ),
        opposite_rebalance_no_trade_max_notional=float(
            params.get("opposite_rebalance_no_trade_max_notional", 300_000.0)
        ),
        opposite_rebalance_no_trade_pct_of_position=float(
            params.get("opposite_rebalance_no_trade_pct_of_position", 0.05)
        ),
        risk_reentry_cooldown_minutes=float(params.get("risk_reentry_cooldown_minutes", 60.0)),
        risk_state_model_id=str(params.get("risk_state_model_id", RISK_STATE_MODEL_ID)),
        risk_state_namespace=str(params.get("risk_state_namespace", RISK_STATE_NAMESPACE)),
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


def _bypass_unordered_quantity_guard(target: PortfolioTarget, delta: int) -> bool:
    if delta >= 0:
        return False
    tag = str(target.tag or "").strip().lower()
    return any(
        token in tag
        for token in (
            "hard_exit",
            "urgent",
            "risk_exit",
            "risk-off",
            "risk_off",
            "stop",
            "trailing",
            "force_exit",
            "forced_exit",
        )
    )


def _is_forced_or_risk_target(target: PortfolioTarget) -> bool:
    tag = str(target.tag or "").strip().lower()
    return any(
        token in tag
        for token in (
            "hard_exit",
            "urgent",
            "risk_exit",
            "risk:",
            "risk-off",
            "risk_off",
            "symbol_guard",
            "stop",
            "trailing",
            "force_exit",
            "forced_exit",
            "manual",
            "operator",
        )
    )


def _single_order_side(orders: tuple[OrderIntent, ...]) -> OrderSide | None:
    sides = {order.side for order in orders}
    if len(sides) != 1:
        return None
    return next(iter(sides))


def _order_side_from_text(value: object) -> OrderSide | None:
    text = str(value or "").strip().lower()
    if text == OrderSide.BUY.value:
        return OrderSide.BUY
    if text == OrderSide.SELL.value:
        return OrderSide.SELL
    return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _within_minutes(start: datetime | None, end: datetime, minutes: float) -> bool:
    if start is None or minutes <= 0:
        return False
    elapsed = (end - start).total_seconds()
    return 0 <= elapsed <= minutes * 60.0


def _target_reduction_reason(target: PortfolioTarget) -> str:
    tag = str(target.tag or "").strip().lower()
    if "symbol_guard_exit" in tag:
        return "symbol_guard_exit"
    if "symbol_guard_reduce_half" in tag:
        return "symbol_guard_reduce_half"
    if "risk:" in tag:
        return "risk"
    if "stop" in tag or "trailing" in tag:
        return "stop"
    return "rebalance"


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


def _positive_float_or_none(value) -> float | None:
    if value is None:
        return None
    resolved = float(value)
    return resolved if resolved > 0 else None


def _liquidity_notional(bar) -> tuple[float | None, str | None]:
    for key in (
        "rolling_dollar_volume_20",
        "average_dollar_volume_20",
        "avg_dollar_volume_20",
        "dollar_volume_ma20",
    ):
        value = _metadata_float(bar, key)
        if value is not None and value > 0:
            return value, key
    return None, None


def _metadata_float(bar, key: str) -> float | None:
    metadata = getattr(bar, "metadata", {}) or {}
    try:
        value = metadata.get(key)
    except AttributeError:
        return None
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


LeapsImmediateExecutionModel = LeapsMomentumExecutionModel

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.engine_guard import EngineGuard, EngineGuardReport
from leaps_quant_engine.market_rules import MarketSession
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol, TimeInForce
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrationResult, MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import OrderRuntimeStateStore
from leaps_quant_engine.order_status import OrderRuntimeStatusReport, build_order_runtime_status
from leaps_quant_engine.orders import OrderCoordinationResult, OrderCoordinator
from leaps_quant_engine.security import SecurityCatalog
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


ORDER_INTENT_BATCH_ARTIFACT_SCHEMA_VERSION = "order_intent_batches.v1"
_ORDER_DROP_GUARD_REASONS = frozenset(
    {
        "target_quantity_already_covered_by_pending_orders",
        "reserved_sell_quantity_exceeded",
    }
)
_ORDER_CLAMP_GUARD_REASONS = frozenset(
    {
        "order_quantity_exceeds_unreserved_target_delta",
    }
)


@dataclass(frozen=True, slots=True)
class OrderRuntimeSubmitReport:
    generated_at: datetime
    runtime_id: str
    broker: str
    commit: bool
    allowed_sleeve_ids: tuple[str, ...]
    batch_count: int
    order_count: int
    total_notional: float
    coordination: OrderCoordinationResult
    final_status: OrderRuntimeStatusReport
    orchestration: MultiSleeveOrderOrchestrationResult | None = None
    guard: EngineGuardReport | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.errors:
            return "blocked"
        if not self.commit:
            return "dry_run"
        if self.warnings or self.coordination.has_collisions:
            return "submitted_with_warnings"
        return "submitted"

    def to_dict(self, *, include_details: bool = True) -> dict[str, Any]:
        return {
            "status": self.status,
            "generated_at": self.generated_at.isoformat(),
            "runtime_id": self.runtime_id,
            "broker": self.broker,
            "commit": self.commit,
            "allowed_sleeve_ids": list(self.allowed_sleeve_ids),
            "batch_count": self.batch_count,
            "order_count": self.order_count,
            "total_notional": self.total_notional,
            "coordination": self.coordination.to_dict() if include_details else {
                "ticket_count": len(self.coordination.tickets),
                "event_count": len(self.coordination.events),
                "collision_count": len(self.coordination.collisions),
                "has_collisions": self.coordination.has_collisions,
            },
            "guard": self.guard.to_dict() if self.guard is not None else None,
            "orchestration": self.orchestration.to_dict(include_details=include_details)
            if self.orchestration is not None
            else None,
            "final_status": self.final_status.to_dict(include_details=include_details),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class OrderRuntimeSubmitter:
    runtime_id: str
    order_state_store: OrderRuntimeStateStore
    account_store: VirtualSleeveAccountStore
    orchestrator: MultiSleeveOrderOrchestrator | None = None
    order_store_path: Path | None = None
    account_store_path: Path | None = None
    broker_account_id: str | None = None
    market_scope: str | None = None
    currency: str = "KRW"
    coordinator: OrderCoordinator = OrderCoordinator()
    engine_guard: EngineGuard = EngineGuard()
    require_orderable_session: bool = False
    market_session: MarketSession | None = None
    security_catalog: SecurityCatalog | None = None

    def submit_batches(
        self,
        batches: Iterable[OrderIntentBatch],
        *,
        allowed_sleeve_ids: tuple[str, ...],
        broker: str = "paper",
        commit: bool = False,
        confirm_live_submit: bool = False,
        poll_after_submit: bool = False,
        max_submit_notional: float | None = None,
        allowed_symbols: tuple[str, ...] = (),
        recent_events: int = 10,
        generated_at: datetime | None = None,
        initial_errors: tuple[str, ...] = (),
    ) -> OrderRuntimeSubmitReport:
        generated_at = generated_at or datetime.now()
        batches_tuple = _enrich_batches_with_runtime_metadata(
            tuple(batches),
            market_session=self.market_session,
            security_catalog=self.security_catalog,
        )
        coordination = self.coordinator.coordinate(batches_tuple, generated_at=generated_at)
        request_errors, warnings = _validate_submit_request(
            batches_tuple,
            broker=broker,
            commit=commit,
            confirm_live_submit=confirm_live_submit,
            allowed_sleeve_ids=allowed_sleeve_ids,
            max_submit_notional=max_submit_notional,
            allowed_symbols=allowed_symbols,
        )
        errors = initial_errors + request_errors
        guard = self.engine_guard.evaluate(
            batches=batches_tuple,
            account_store=self.account_store,
            order_state_store=self.order_state_store,
            account_id=self.broker_account_id,
            market_scope=self.market_scope,
            broker=broker,
            commit=commit,
            require_orderable_session=self.require_orderable_session or (commit and broker == "broker-engine" and confirm_live_submit),
            market_session=self.market_session,
            security_catalog=self.security_catalog,
            generated_at=generated_at,
        )

        if commit and not errors and guard.blocked:
            filtered_batches, dropped, adjusted = _repair_guard_rejected_order_intents(batches_tuple, guard)
            if dropped or adjusted:
                batches_tuple = filtered_batches
                coordination = self.coordinator.coordinate(batches_tuple, generated_at=generated_at)
                request_errors, request_warnings = _validate_submit_request(
                    batches_tuple,
                    broker=broker,
                    commit=commit,
                    confirm_live_submit=confirm_live_submit,
                    allowed_sleeve_ids=allowed_sleeve_ids,
                    max_submit_notional=max_submit_notional,
                    allowed_symbols=allowed_symbols,
                )
                errors = initial_errors + request_errors
                warnings = warnings + request_warnings + _dropped_order_warnings(dropped) + _adjusted_order_warnings(adjusted)
                guard = self.engine_guard.evaluate(
                    batches=batches_tuple,
                    account_store=self.account_store,
                    order_state_store=self.order_state_store,
                    account_id=self.broker_account_id,
                    market_scope=self.market_scope,
                    broker=broker,
                    commit=commit,
                    require_orderable_session=self.require_orderable_session
                    or (commit and broker == "broker-engine" and confirm_live_submit),
                    market_session=self.market_session,
                    security_catalog=self.security_catalog,
                    generated_at=generated_at,
                )
        errors = errors + guard.errors
        warnings = warnings + guard.warnings
        errors = tuple(dict.fromkeys(errors))
        warnings = tuple(dict.fromkeys(warnings))
        orchestration = None
        if commit and not errors:
            if self.orchestrator is None:
                errors = errors + ("commit_requested_without_orchestrator",)
            else:
                try:
                    orchestration = self.orchestrator.run_batches(
                        batches_tuple,
                        generated_at=generated_at,
                        poll_after_submit=poll_after_submit,
                    )
                    coordination = orchestration.coordination
                except Exception as exc:  # noqa: BLE001
                    errors = errors + (f"orchestration_failed: {exc}",)

        final_status = build_order_runtime_status(
            runtime_id=self.runtime_id,
            sleeve_ids=allowed_sleeve_ids,
            order_state_store=self.order_state_store,
            account_store=self.account_store,
            order_store_path=self.order_store_path,
            account_store_path=self.account_store_path,
            broker_account_id=self.broker_account_id,
            market_scope=self.market_scope,
            currency=self.currency,
            recent_events=recent_events,
            generated_at=datetime.now(),
        )
        return OrderRuntimeSubmitReport(
            generated_at=generated_at,
            runtime_id=self.runtime_id,
            broker=broker,
            commit=commit,
            allowed_sleeve_ids=allowed_sleeve_ids,
            batch_count=len(batches_tuple),
            order_count=sum(batch.order_count for batch in batches_tuple),
            total_notional=sum(order.notional for batch in batches_tuple for order in batch.order_intents),
            coordination=coordination,
            orchestration=orchestration,
            final_status=final_status,
            guard=guard,
            errors=errors,
            warnings=warnings,
        )


def _repair_guard_rejected_order_intents(
    batches: tuple[OrderIntentBatch, ...],
    guard: EngineGuardReport,
) -> tuple[tuple[OrderIntentBatch, ...], tuple[EngineGuardDecisionDrop, ...], tuple[EngineGuardDecisionAdjustment, ...]]:
    drop_keys: dict[tuple[str, str, str], EngineGuardDecisionDrop] = {}
    clamp_limits: dict[tuple[str, str, str], EngineGuardDecisionAdjustment] = {}
    for decision in guard.decisions:
        if decision.status != "rejected":
            continue
        if not decision.sleeve_id or not decision.symbol or not decision.order_side:
            continue
        key = (decision.sleeve_id, decision.symbol, decision.order_side)
        if decision.reason in _ORDER_DROP_GUARD_REASONS:
            drop_keys.setdefault(
                key,
                EngineGuardDecisionDrop(
                    sleeve_id=decision.sleeve_id,
                    symbol=decision.symbol,
                    order_side=decision.order_side,
                    reason=decision.reason,
                ),
            )
            continue
        if decision.reason in _ORDER_CLAMP_GUARD_REASONS:
            max_quantity = _clamp_quantity_from_guard_decision(decision.metadata, order_side=decision.order_side)
            if max_quantity is not None:
                clamp_limits.setdefault(
                    key,
                    EngineGuardDecisionAdjustment(
                        sleeve_id=decision.sleeve_id,
                        symbol=decision.symbol,
                        order_side=decision.order_side,
                        reason=decision.reason,
                        max_quantity=max_quantity,
                    ),
                )
    if not drop_keys and not clamp_limits:
        return batches, (), ()

    filtered_batches: list[OrderIntentBatch] = []
    dropped: list[EngineGuardDecisionDrop] = []
    dropped_keys: set[tuple[str, str, str]] = set()
    adjusted: list[EngineGuardDecisionAdjustment] = []
    remaining_by_key = {key: adjustment.max_quantity for key, adjustment in clamp_limits.items()}
    for batch in batches:
        kept_orders: list[OrderIntent] = []
        for order in batch.order_intents:
            key = (order.sleeve_id, order.symbol.key, order.side.value)
            drop = drop_keys.get(key)
            if drop is not None:
                if key not in dropped_keys:
                    dropped.append(drop)
                    dropped_keys.add(key)
                continue

            adjustment = clamp_limits.get(key)
            if adjustment is None:
                kept_orders.append(order)
                continue
            remaining = remaining_by_key.get(key, 0)
            if remaining <= 0:
                if key not in dropped_keys:
                    dropped.append(
                        EngineGuardDecisionDrop(
                            sleeve_id=order.sleeve_id,
                            symbol=order.symbol.key,
                            order_side=order.side.value,
                            reason=adjustment.reason,
                        )
                    )
                    dropped_keys.add(key)
                continue
            if order.quantity <= remaining:
                kept_orders.append(order)
                remaining_by_key[key] = remaining - order.quantity
                continue
            kept_orders.append(
                replace(
                    order,
                    quantity=remaining,
                    metadata={
                        **dict(order.metadata),
                        "engine_guard_original_quantity": order.quantity,
                        "engine_guard_adjusted_quantity": remaining,
                        "engine_guard_adjustment_reason": adjustment.reason,
                    },
                )
            )
            remaining_by_key[key] = 0
            adjusted.append(
                EngineGuardDecisionAdjustment(
                    sleeve_id=adjustment.sleeve_id,
                    symbol=adjustment.symbol,
                    order_side=adjustment.order_side,
                    reason=adjustment.reason,
                    max_quantity=adjustment.max_quantity,
                    original_quantity=order.quantity,
                    adjusted_quantity=remaining,
                )
            )
        if kept_orders:
            filtered_batches.append(replace(batch, order_intents=tuple(kept_orders)))
    return tuple(filtered_batches), tuple(dropped), tuple(adjusted)


@dataclass(frozen=True, slots=True)
class EngineGuardDecisionDrop:
    sleeve_id: str
    symbol: str
    order_side: str
    reason: str


@dataclass(frozen=True, slots=True)
class EngineGuardDecisionAdjustment:
    sleeve_id: str
    symbol: str
    order_side: str
    reason: str
    max_quantity: int
    original_quantity: int | None = None
    adjusted_quantity: int | None = None


def _dropped_order_warnings(dropped: tuple[EngineGuardDecisionDrop, ...]) -> tuple[str, ...]:
    return tuple(
        f"dropped_guard_rejected_order_intent:{drop.sleeve_id}:{drop.symbol}:{drop.order_side}:{drop.reason}"
        for drop in dropped
    )


def _adjusted_order_warnings(adjusted: tuple[EngineGuardDecisionAdjustment, ...]) -> tuple[str, ...]:
    return tuple(
        "adjusted_guard_rejected_order_intent:"
        f"{item.sleeve_id}:{item.symbol}:{item.order_side}:{item.reason}:"
        f"{item.original_quantity}->{item.adjusted_quantity}"
        for item in adjusted
        if item.original_quantity is not None and item.adjusted_quantity is not None
    )


def _clamp_quantity_from_guard_decision(metadata: Mapping[str, Any], *, order_side: str) -> int | None:
    unreserved_delta = _int_or_none(metadata.get("unreserved_delta"))
    if unreserved_delta is None or unreserved_delta == 0:
        return None
    if order_side == OrderSide.BUY.value and unreserved_delta > 0:
        return unreserved_delta
    if order_side == OrderSide.SELL.value and unreserved_delta < 0:
        return abs(unreserved_delta)
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _order_count(batches: tuple[OrderIntentBatch, ...]) -> int:
    return sum(batch.order_count for batch in batches)


def _enrich_batches_with_market_session(
    batches: tuple[OrderIntentBatch, ...],
    market_session: MarketSession | None,
) -> tuple[OrderIntentBatch, ...]:
    return _enrich_batches_with_runtime_metadata(
        batches,
        market_session=market_session,
        security_catalog=None,
    )


def _enrich_batches_with_runtime_metadata(
    batches: tuple[OrderIntentBatch, ...],
    *,
    market_session: MarketSession | None,
    security_catalog: SecurityCatalog | None,
) -> tuple[OrderIntentBatch, ...]:
    if market_session is None and security_catalog is None:
        return batches
    enriched_batches: list[OrderIntentBatch] = []
    for batch in batches:
        enriched_orders = tuple(
            replace(
                order,
                metadata=_metadata_with_runtime_metadata(
                    order,
                    market_session=market_session,
                    security_catalog=security_catalog,
                ),
            )
            for order in batch.order_intents
        )
        enriched_batches.append(replace(batch, order_intents=enriched_orders))
    return tuple(enriched_batches)


def _metadata_with_runtime_metadata(
    order: OrderIntent,
    *,
    market_session: MarketSession | None,
    security_catalog: SecurityCatalog | None,
) -> dict[str, Any]:
    enriched = dict(order.metadata)
    if market_session is not None:
        enriched = _metadata_with_market_session(enriched, market_session)
    if security_catalog is not None and "symbol_properties" not in enriched:
        enriched["symbol_properties"] = security_catalog.resolve(order.symbol).to_dict()
    return enriched


def _metadata_with_market_session(
    metadata: Mapping[str, Any],
    market_session: MarketSession,
) -> dict[str, Any]:
    enriched = dict(metadata)
    enriched.setdefault("order_session", market_session.session_phase)
    enriched.setdefault("market_session_phase", market_session.session_phase)
    enriched.setdefault("market_session_scope", market_session.market_scope)
    enriched.setdefault("market_session_source", market_session.source)
    enriched.setdefault("is_regular_market_open", market_session.is_regular_market_open)
    return enriched


def load_order_intent_batches(path: Path) -> tuple[OrderIntentBatch, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        batch_payloads = payload
    elif isinstance(payload, dict) and isinstance(payload.get("batches"), list):
        batch_payloads = payload["batches"]
    elif isinstance(payload, dict):
        batch_payloads = [payload]
    else:
        raise ValueError("order intent batch file must contain an object, a list, or {'batches': [...]}.")
    return tuple(_parse_order_intent_batch(dict(raw)) for raw in batch_payloads if isinstance(raw, dict))


def write_order_intent_batches(
    path: Path,
    batches: Iterable[OrderIntentBatch],
    *,
    runtime_id: str = "",
    config_version: str = "",
    source: str = "",
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    batches_tuple = tuple(batches)
    payload = {
        "schema_version": ORDER_INTENT_BATCH_ARTIFACT_SCHEMA_VERSION,
        "runtime_id": runtime_id,
        "config_version": config_version,
        "source": source,
        "generated_at": (generated_at or datetime.now()).isoformat(),
        "batch_count": len(batches_tuple),
        "order_count": sum(batch.order_count for batch in batches_tuple),
        "batches": [batch.to_dict() for batch in batches_tuple],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    temporary_path.replace(path)
    return {
        "path": str(path),
        "schema_version": payload["schema_version"],
        "batch_count": payload["batch_count"],
        "order_count": payload["order_count"],
    }


def _parse_order_intent_batch(payload: Mapping[str, Any]) -> OrderIntentBatch:
    sleeve_id = _required_text(payload, "sleeve_id")
    orders_payload = payload.get("order_intents", payload.get("orders", ()))
    if not orders_payload and isinstance(payload.get("execution"), Mapping):
        execution = dict(payload["execution"])
        orders_payload = execution.get("order_intents", execution.get("orders", ()))
    if not isinstance(orders_payload, list):
        raise ValueError("OrderIntentBatch orders must be a list.")
    return OrderIntentBatch(
        sleeve_id=sleeve_id,
        generated_at=_parse_datetime(payload.get("generated_at")) or datetime.now(),
        order_intents=tuple(_parse_order_intent(dict(order), default_sleeve_id=sleeve_id) for order in orders_payload),
        model_name=str(payload.get("model_name") or ""),
        reason=str(payload.get("reason") or ""),
        metadata=dict(payload.get("metadata") or {}),
        batch_id=str(payload.get("batch_id") or f"order-intents:{sleeve_id}:{datetime.now().isoformat()}"),
    )


def _parse_order_intent(payload: Mapping[str, Any], *, default_sleeve_id: str) -> OrderIntent:
    sleeve_id = str(payload.get("sleeve_id") or default_sleeve_id)
    return OrderIntent(
        sleeve_id=sleeve_id,
        symbol=_parse_symbol(payload),
        side=OrderSide(str(payload.get("side") or "").strip().lower()),
        quantity=_positive_int(payload.get("quantity"), "quantity"),
        reference_price=_positive_float(payload.get("reference_price"), "reference_price"),
        tag=str(payload.get("tag") or ""),
        order_type=_parse_order_type(payload.get("order_type")),
        limit_price=_optional_float(payload.get("limit_price")),
        time_in_force=_parse_time_in_force(payload.get("time_in_force")),
        metadata=dict(_object(payload.get("metadata"), default={})),
    )


def _parse_symbol(payload: Mapping[str, Any]) -> Symbol:
    raw_symbol = payload.get("symbol")
    market = str(payload.get("market") or "").strip().upper()
    if isinstance(raw_symbol, Mapping):
        ticker = str(raw_symbol.get("ticker") or raw_symbol.get("symbol") or "").strip().upper()
        market = str(raw_symbol.get("market") or market or "KRX").strip().upper()
        if not ticker:
            raise ValueError("order symbol ticker is required.")
        return Symbol(ticker=ticker, market=market)
    text = str(raw_symbol or payload.get("ticker") or "").strip().upper()
    if ":" in text:
        parsed_market, ticker = text.split(":", 1)
        if not ticker.strip():
            raise ValueError("order symbol ticker is required.")
        return Symbol(ticker=ticker.strip(), market=parsed_market.strip())
    if not text:
        raise ValueError("order symbol is required.")
    return Symbol(ticker=text, market=market or "KRX")


def _validate_submit_request(
    batches: tuple[OrderIntentBatch, ...],
    *,
    broker: str,
    commit: bool,
    confirm_live_submit: bool,
    allowed_sleeve_ids: tuple[str, ...],
    max_submit_notional: float | None,
    allowed_symbols: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    errors: list[str] = []
    warnings: list[str] = []
    allowed_sleeves = set(allowed_sleeve_ids)
    allowed_symbol_keys = {symbol.upper() for symbol in allowed_symbols}
    total_notional = 0.0
    if commit and broker == "broker-engine" and not confirm_live_submit:
        errors.append("broker_engine_submit_requires_confirm_live_submit")
    for batch in batches:
        if batch.sleeve_id not in allowed_sleeves:
            errors.append(f"batch_sleeve_not_allowed:{batch.sleeve_id}")
        for order in batch.order_intents:
            if order.sleeve_id != batch.sleeve_id:
                errors.append(f"order_sleeve_mismatch:{batch.batch_id}:{order.sleeve_id}")
            if order.sleeve_id not in allowed_sleeves:
                errors.append(f"order_sleeve_not_allowed:{order.sleeve_id}")
            if order.quantity <= 0:
                errors.append(f"order_quantity_must_be_positive:{batch.batch_id}:{order.symbol.key}")
            if order.reference_price <= 0:
                errors.append(f"order_reference_price_must_be_positive:{batch.batch_id}:{order.symbol.key}")
            if allowed_symbol_keys and order.symbol.key.upper() not in allowed_symbol_keys and order.symbol.ticker.upper() not in allowed_symbol_keys:
                errors.append(f"order_symbol_not_allowed:{order.symbol.key}")
            total_notional += order.notional
    if max_submit_notional is not None and total_notional > max_submit_notional:
        errors.append(f"total_notional_exceeds_limit:{total_notional:g}>{max_submit_notional:g}")
    if not batches:
        warnings.append("no_order_intent_batches")
    return tuple(dict.fromkeys(errors)), tuple(dict.fromkeys(warnings))


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    text = str(payload.get(key) or "").strip()
    if not text:
        raise ValueError(f"{key} is required.")
    return text


def _positive_int(value: Any, key: str) -> int:
    number = int(float(str(value).replace(",", "").strip()))
    if number <= 0:
        raise ValueError(f"{key} must be positive.")
    return number


def _positive_float(value: Any, key: str) -> float:
    number = float(str(value).replace(",", "").strip())
    if number <= 0:
        raise ValueError(f"{key} must be positive.")
    return number


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    return float(text)


def _parse_order_type(value: Any) -> OrderType:
    text = str(value or OrderType.LIMIT.value).strip().lower()
    return OrderType(text)


def _parse_time_in_force(value: Any) -> TimeInForce:
    text = str(value or TimeInForce.DAY.value).strip().lower()
    return TimeInForce(text)


def _object(value: Any, *, default: Mapping[str, Any]) -> Mapping[str, Any]:
    if value is None:
        return default
    if not isinstance(value, Mapping):
        raise ValueError("metadata must be an object.")
    return value

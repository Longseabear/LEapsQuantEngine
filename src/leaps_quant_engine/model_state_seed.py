from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from leaps_quant_engine.runtime_state import ModelStateKey, RuntimeStateStore, StatePatch
from leaps_quant_engine.virtual_account import PositionState


DEFAULT_TRAILING_STOP_MODEL_ID = "leaps-volatility-trailing-stop"
DEFAULT_TRAILING_STOP_NAMESPACE = "trailing_stop"


@dataclass(frozen=True, slots=True)
class RuntimeStateSeedRow:
    symbol: str
    quantity: int
    high_watermark_price: float
    last_price: float | None
    prior_high_watermark_price: float | None = None
    event_id: str | None = None
    new_version: int | None = None
    status: str = "seeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "high_watermark_price": self.high_watermark_price,
            "last_price": self.last_price,
            "prior_high_watermark_price": self.prior_high_watermark_price,
            "event_id": self.event_id,
            "new_version": self.new_version,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class RuntimeStateSeedReport:
    sleeve_id: str
    model_id: str
    namespace: str
    position_count: int
    seeded_count: int
    event_count: int
    rows: tuple[RuntimeStateSeedRow, ...]

    @property
    def status(self) -> str:
        if self.position_count == 0:
            return "no_positions"
        if self.seeded_count == self.position_count and self.event_count == self.seeded_count:
            return "seeded"
        return "partial"

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "sleeve_id": self.sleeve_id,
            "model_id": self.model_id,
            "namespace": self.namespace,
            "position_count": self.position_count,
            "seeded_count": self.seeded_count,
            "event_count": self.event_count,
        }
        if include_rows:
            payload["rows"] = [row.to_dict() for row in self.rows]
        return payload


def seed_trailing_stop_state_from_positions(
    positions: Iterable[PositionState],
    store: RuntimeStateStore,
    *,
    sleeve_id: str,
    model_id: str = DEFAULT_TRAILING_STOP_MODEL_ID,
    namespace: str = DEFAULT_TRAILING_STOP_NAMESPACE,
    applied_at: datetime | None = None,
) -> RuntimeStateSeedReport:
    position_tuple = tuple(position for position in positions if position.quantity > 0)
    patches: list[StatePatch] = []
    pending_rows: list[RuntimeStateSeedRow] = []
    for position in position_tuple:
        key = ModelStateKey(
            sleeve_id=sleeve_id,
            model_id=model_id,
            namespace=namespace,
            symbol_key=position.symbol.key,
        )
        existing = store.get(key)
        prior_high = _float_or_none((existing.value if existing else {}).get("high_watermark_price"))
        high_candidates = [
            value
            for value in (
                prior_high,
                position.high_watermark_price,
                position.last_price,
                position.average_entry_price,
            )
            if value is not None and value > 0
        ]
        if not high_candidates:
            pending_rows.append(
                RuntimeStateSeedRow(
                    symbol=position.symbol.key,
                    quantity=position.quantity,
                    high_watermark_price=0.0,
                    last_price=position.last_price,
                    prior_high_watermark_price=prior_high,
                    status="skipped_invalid_price",
                )
            )
            continue
        seed_high = max(high_candidates)
        last_price = position.last_price if position.last_price is not None else seed_high
        patches.append(
            StatePatch(
                key=key,
                value={
                    "quantity": position.quantity,
                    "average_entry_price": position.average_entry_price,
                    "entry_time": position.entry_time.isoformat(),
                    "high_watermark_price": seed_high,
                    "high_watermark_at": position.high_watermark_at.isoformat(),
                    "last_price": last_price,
                    "last_updated_at": position.last_updated_at.isoformat() if position.last_updated_at else "",
                    "last_stop_price": position.last_stop_price,
                    "seeded_from": "virtual_account_position_state",
                },
                reason="seed_from_virtual_position_state",
            )
        )
        pending_rows.append(
            RuntimeStateSeedRow(
                symbol=position.symbol.key,
                quantity=position.quantity,
                high_watermark_price=seed_high,
                last_price=last_price,
                prior_high_watermark_price=prior_high,
            )
        )
    events = store.apply_patches(tuple(patches), applied_at=applied_at)
    rows = _merge_seed_events(pending_rows, events)
    seeded_count = sum(1 for row in rows if row.status == "seeded")
    return RuntimeStateSeedReport(
        sleeve_id=sleeve_id,
        model_id=model_id,
        namespace=namespace,
        position_count=len(position_tuple),
        seeded_count=seeded_count,
        event_count=len(events),
        rows=rows,
    )


def _merge_seed_events(
    pending_rows: list[RuntimeStateSeedRow],
    events: tuple[Any, ...],
) -> tuple[RuntimeStateSeedRow, ...]:
    event_index = 0
    rows: list[RuntimeStateSeedRow] = []
    for row in pending_rows:
        if row.status != "seeded":
            rows.append(row)
            continue
        event = events[event_index] if event_index < len(events) else None
        event_index += 1
        rows.append(
            RuntimeStateSeedRow(
                symbol=row.symbol,
                quantity=row.quantity,
                high_watermark_price=row.high_watermark_price,
                last_price=row.last_price,
                prior_high_watermark_price=row.prior_high_watermark_price,
                event_id=event.event_id if event is not None else None,
                new_version=event.new_version if event is not None else None,
                status="seeded" if event is not None else "not_committed",
            )
        )
    return tuple(rows)


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

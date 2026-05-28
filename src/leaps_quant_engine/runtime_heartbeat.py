from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4


RUNTIME_HEARTBEAT_SCHEMA_VERSION = "runtime_heartbeat.v1"


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeat:
    runtime_id: str
    component: str
    status: str
    updated_at: datetime
    config_path: str = ""
    config_version: str = ""
    sleeve_ids: tuple[str, ...] = ()
    cycle_index: int | None = None
    process_id: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = RUNTIME_HEARTBEAT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", str(self.status or "unknown").strip().lower())
        object.__setattr__(self, "component", str(self.component or "").strip())
        object.__setattr__(self, "sleeve_ids", tuple(str(item) for item in self.sleeve_ids))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "runtime_id": self.runtime_id,
            "component": self.component,
            "status": self.status,
            "updated_at": self.updated_at.isoformat(),
            "config_path": self.config_path,
            "config_version": self.config_version,
            "sleeve_ids": list(self.sleeve_ids),
            "cycle_index": self.cycle_index,
            "process_id": self.process_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RuntimeHeartbeat":
        process_id = _optional_int(payload.get("process_id"))
        return cls(
            schema_version=str(payload.get("schema_version") or RUNTIME_HEARTBEAT_SCHEMA_VERSION),
            runtime_id=str(payload.get("runtime_id") or ""),
            component=str(payload.get("component") or ""),
            status=str(payload.get("status") or "unknown"),
            updated_at=_parse_datetime(payload.get("updated_at")),
            config_path=str(payload.get("config_path") or ""),
            config_version=str(payload.get("config_version") or ""),
            sleeve_ids=tuple(str(item) for item in payload.get("sleeve_ids") or ()),
            cycle_index=_optional_int(payload.get("cycle_index")),
            process_id=process_id,
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class RuntimeHeartbeatEvaluation:
    name: str
    status: str
    reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


def write_runtime_heartbeat(path: str | Path, heartbeat: RuntimeHeartbeat) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    tmp.write_text(json.dumps(heartbeat.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def read_runtime_heartbeat(path: str | Path) -> RuntimeHeartbeat | None:
    target = Path(path)
    if not target.exists():
        return None
    payload = json.loads(target.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        return None
    return RuntimeHeartbeat.from_dict(payload)


def evaluate_runtime_heartbeat(
    path: str | Path,
    *,
    runtime_id: str,
    max_age_seconds: float,
    now: datetime | None = None,
    component: str | None = None,
    missing_status: str = "warning",
    stale_status: str = "warning",
) -> RuntimeHeartbeatEvaluation:
    target = Path(path)
    now = now or datetime.now().astimezone()
    heartbeat = read_runtime_heartbeat(target)
    if heartbeat is None:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            missing_status,
            reason="heartbeat_missing",
            metadata={"path": str(target), "runtime_id": runtime_id, "component": component},
        )

    updated_at, comparable_now = _same_datetime_kind(heartbeat.updated_at, now)
    age_seconds = max(0.0, (comparable_now - updated_at).total_seconds())
    metadata = {
        "path": str(target),
        "runtime_id": heartbeat.runtime_id,
        "component": heartbeat.component,
        "heartbeat_status": heartbeat.status,
        "updated_at": heartbeat.updated_at.isoformat(),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "config_path": heartbeat.config_path,
        "config_version": heartbeat.config_version,
        "sleeve_ids": list(heartbeat.sleeve_ids),
        "cycle_index": heartbeat.cycle_index,
        "process_id": heartbeat.process_id,
        "process_id_liveness_checked": False,
        "heartbeat_metadata": dict(heartbeat.metadata),
    }
    if heartbeat.runtime_id != runtime_id:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            "warning",
            reason="runtime_id_mismatch",
            metadata=metadata,
        )
    if component is not None and heartbeat.component != component:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            "warning",
            reason="component_mismatch",
            metadata=metadata,
        )
    if heartbeat.status in {"critical", "error", "failed"}:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            "critical",
            reason=f"heartbeat_status={heartbeat.status}",
            metadata=metadata,
        )
    if heartbeat.status in {"stopped", "shutdown"}:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            stale_status,
            reason=f"heartbeat_status={heartbeat.status}",
            metadata=metadata,
        )
    if age_seconds > max_age_seconds:
        return RuntimeHeartbeatEvaluation(
            "runtime_heartbeat",
            stale_status,
            reason="heartbeat_stale",
            metadata=metadata,
        )
    return RuntimeHeartbeatEvaluation("runtime_heartbeat", "ok", metadata=metadata)


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.now().astimezone()
    return datetime.fromisoformat(text)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _same_datetime_kind(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None and end.tzinfo is not None:
        return start.replace(tzinfo=end.tzinfo), end
    if start.tzinfo is not None and end.tzinfo is None:
        return start.replace(tzinfo=None), end
    return start, end

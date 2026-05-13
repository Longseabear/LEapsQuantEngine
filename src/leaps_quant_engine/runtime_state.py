from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
import sqlite3
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Protocol
from uuid import uuid4


MODEL_STATE_SCHEMA_VERSION = "model_state.v1"
MODEL_STATE_EVENT_SCHEMA_VERSION = "model_state_event.v1"


@dataclass(frozen=True, slots=True)
class ModelStateKey:
    """Stable namespace for optional model-owned runtime state."""

    sleeve_id: str
    model_id: str
    namespace: str = "default"
    symbol_key: str = ""
    position_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "sleeve_id", str(self.sleeve_id).strip())
        object.__setattr__(self, "model_id", str(self.model_id).strip())
        object.__setattr__(self, "namespace", str(self.namespace or "default").strip() or "default")
        object.__setattr__(self, "symbol_key", str(self.symbol_key or "").strip())
        object.__setattr__(self, "position_id", str(self.position_id or "").strip())
        if not self.sleeve_id:
            raise ValueError("sleeve_id is required.")
        if not self.model_id:
            raise ValueError("model_id is required.")

    @property
    def tuple_key(self) -> tuple[str, str, str, str, str]:
        return (self.sleeve_id, self.model_id, self.namespace, self.symbol_key, self.position_id)

    def to_dict(self) -> dict[str, str]:
        return {
            "sleeve_id": self.sleeve_id,
            "model_id": self.model_id,
            "namespace": self.namespace,
            "symbol_key": self.symbol_key,
            "position_id": self.position_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModelStateKey":
        return cls(
            sleeve_id=str(payload.get("sleeve_id") or ""),
            model_id=str(payload.get("model_id") or ""),
            namespace=str(payload.get("namespace") or "default"),
            symbol_key=str(payload.get("symbol_key") or ""),
            position_id=str(payload.get("position_id") or ""),
        )


@dataclass(frozen=True, slots=True)
class ModelStateRecord:
    key: ModelStateKey
    value: Mapping[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime
    schema_version: str = MODEL_STATE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", MappingProxyType(_json_normalized_mapping(self.value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "key": self.key.to_dict(),
            "value": dict(self.value),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ModelStateRecord":
        return cls(
            schema_version=str(payload.get("schema_version") or MODEL_STATE_SCHEMA_VERSION),
            key=ModelStateKey.from_dict(dict(payload.get("key") or {})),
            value=dict(payload.get("value") or {}),
            version=int(payload.get("version") or 0),
            created_at=_parse_datetime(payload.get("created_at")),
            updated_at=_parse_datetime(payload.get("updated_at")),
        )


class StatePatchOperation(str, Enum):
    SET = "set"
    MERGE = "merge"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class StatePatch:
    """A model-requested state change committed by the runtime at a boundary."""

    key: ModelStateKey
    value: Mapping[str, Any] = field(default_factory=dict)
    operation: StatePatchOperation | str = StatePatchOperation.MERGE
    reason: str = ""
    generated_at: datetime = field(default_factory=datetime.now)
    patch_id: str = field(default_factory=lambda: f"state-patch-{uuid4()}")

    def __post_init__(self) -> None:
        operation = _coerce_operation(self.operation)
        normalized = _json_normalized_mapping(self.value)
        if operation in {StatePatchOperation.SET, StatePatchOperation.MERGE} and not normalized:
            raise ValueError("set and merge state patches require a non-empty value.")
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "value", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "key": self.key.to_dict(),
            "value": dict(self.value),
            "operation": self.operation.value,
            "reason": self.reason,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StatePatch":
        return cls(
            patch_id=str(payload.get("patch_id") or f"state-patch-{uuid4()}"),
            key=ModelStateKey.from_dict(dict(payload.get("key") or {})),
            value=dict(payload.get("value") or {}),
            operation=str(payload.get("operation") or StatePatchOperation.MERGE.value),
            reason=str(payload.get("reason") or ""),
            generated_at=_parse_datetime(payload.get("generated_at")),
        )


@dataclass(frozen=True, slots=True)
class ModelStateEvent:
    event_id: str
    patch_id: str
    key: ModelStateKey
    operation: StatePatchOperation
    value: Mapping[str, Any]
    applied_at: datetime
    reason: str = ""
    prior_version: int | None = None
    new_version: int | None = None
    schema_version: str = MODEL_STATE_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", MappingProxyType(_json_normalized_mapping(self.value)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "patch_id": self.patch_id,
            "key": self.key.to_dict(),
            "operation": self.operation.value,
            "value": dict(self.value),
            "applied_at": self.applied_at.isoformat(),
            "reason": self.reason,
            "prior_version": self.prior_version,
            "new_version": self.new_version,
        }


class RuntimeStateStore(Protocol):
    def get(self, key: ModelStateKey) -> ModelStateRecord | None:
        """Return one model state record."""

    def entries(
        self,
        *,
        sleeve_id: str | None = None,
        model_id: str | None = None,
        namespace: str | None = None,
        symbol_key: str | None = None,
        position_id: str | None = None,
    ) -> tuple[ModelStateRecord, ...]:
        """Return model state records filtered by namespace."""

    def apply_patches(
        self,
        patches: Iterable[StatePatch],
        *,
        applied_at: datetime | None = None,
    ) -> tuple[ModelStateEvent, ...]:
        """Commit model-requested patches and return audit events."""


class InMemoryRuntimeStateStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str, str, str, str], ModelStateRecord] = {}
        self._events: list[ModelStateEvent] = []

    def get(self, key: ModelStateKey) -> ModelStateRecord | None:
        return self._records.get(key.tuple_key)

    def entries(
        self,
        *,
        sleeve_id: str | None = None,
        model_id: str | None = None,
        namespace: str | None = None,
        symbol_key: str | None = None,
        position_id: str | None = None,
    ) -> tuple[ModelStateRecord, ...]:
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.key.tuple_key)
            if _matches(record.key, sleeve_id, model_id, namespace, symbol_key, position_id)
        )

    def events(self) -> tuple[ModelStateEvent, ...]:
        return tuple(self._events)

    def apply_patches(
        self,
        patches: Iterable[StatePatch],
        *,
        applied_at: datetime | None = None,
    ) -> tuple[ModelStateEvent, ...]:
        timestamp = applied_at or datetime.now()
        events: list[ModelStateEvent] = []
        for patch in patches:
            event = _apply_patch_to_records(self._records, patch, timestamp)
            self._events.append(event)
            events.append(event)
        return tuple(events)


@dataclass(frozen=True, slots=True)
class SQLiteRuntimeStateStore:
    """SQLite-backed runtime state store for model-owned state.

    This store is intentionally separate from the live JSON/JSONL stores until a
    runtime explicitly wires it in.
    """

    path: Path

    def get(self, key: ModelStateKey) -> ModelStateRecord | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT sleeve_id, model_id, namespace, symbol_key, position_id,
                       value_json, version, created_at, updated_at, schema_version
                FROM model_state
                WHERE sleeve_id = ? AND model_id = ? AND namespace = ?
                  AND symbol_key = ? AND position_id = ?
                """,
                key.tuple_key,
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def entries(
        self,
        *,
        sleeve_id: str | None = None,
        model_id: str | None = None,
        namespace: str | None = None,
        symbol_key: str | None = None,
        position_id: str | None = None,
    ) -> tuple[ModelStateRecord, ...]:
        self._ensure_schema()
        filters = {
            "sleeve_id": sleeve_id,
            "model_id": model_id,
            "namespace": namespace,
            "symbol_key": symbol_key,
            "position_id": position_id,
        }
        where, values = _where_clause(filters)
        query = (
            "SELECT sleeve_id, model_id, namespace, symbol_key, position_id, "
            "value_json, version, created_at, updated_at, schema_version "
            "FROM model_state"
            f"{where} ORDER BY sleeve_id, model_id, namespace, symbol_key, position_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def events(
        self,
        *,
        sleeve_id: str | None = None,
        model_id: str | None = None,
        namespace: str | None = None,
        symbol_key: str | None = None,
        position_id: str | None = None,
    ) -> tuple[ModelStateEvent, ...]:
        self._ensure_schema()
        filters = {
            "sleeve_id": sleeve_id,
            "model_id": model_id,
            "namespace": namespace,
            "symbol_key": symbol_key,
            "position_id": position_id,
        }
        where, values = _where_clause(filters)
        query = (
            "SELECT event_id, patch_id, sleeve_id, model_id, namespace, symbol_key, "
            "position_id, operation, value_json, applied_at, reason, "
            "prior_version, new_version, schema_version FROM model_state_events"
            f"{where} ORDER BY applied_at, event_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def apply_patches(
        self,
        patches: Iterable[StatePatch],
        *,
        applied_at: datetime | None = None,
    ) -> tuple[ModelStateEvent, ...]:
        patch_tuple = tuple(patches)
        if not patch_tuple:
            return ()
        self._ensure_schema()
        timestamp = applied_at or datetime.now()
        with self._connect() as connection:
            records = {
                _record_from_row(row).key.tuple_key: _record_from_row(row)
                for row in connection.execute(
                    """
                    SELECT sleeve_id, model_id, namespace, symbol_key, position_id,
                           value_json, version, created_at, updated_at, schema_version
                    FROM model_state
                    """
                ).fetchall()
            }
            events = tuple(_apply_patch_to_records(records, patch, timestamp) for patch in patch_tuple)
            for event in events:
                _write_event(connection, event)
                record = records.get(event.key.tuple_key)
                if record is None:
                    _delete_record(connection, event.key)
                else:
                    _upsert_record(connection, record)
            connection.commit()
        return events

    def _ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_state (
                    sleeve_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    symbol_key TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    PRIMARY KEY (sleeve_id, model_id, namespace, symbol_key, position_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_state_events (
                    event_id TEXT PRIMARY KEY,
                    patch_id TEXT NOT NULL,
                    sleeve_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    symbol_key TEXT NOT NULL,
                    position_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    applied_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    prior_version INTEGER,
                    new_version INTEGER,
                    schema_version TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


def _apply_patch_to_records(
    records: dict[tuple[str, str, str, str, str], ModelStateRecord],
    patch: StatePatch,
    applied_at: datetime,
) -> ModelStateEvent:
    existing = records.get(patch.key.tuple_key)
    prior_version = existing.version if existing is not None else None
    new_record: ModelStateRecord | None
    if patch.operation is StatePatchOperation.DELETE:
        records.pop(patch.key.tuple_key, None)
        new_record = None
    else:
        value = dict(patch.value)
        if patch.operation is StatePatchOperation.MERGE and existing is not None:
            value = {**dict(existing.value), **value}
        created_at = existing.created_at if existing is not None else applied_at
        new_record = ModelStateRecord(
            key=patch.key,
            value=value,
            version=(existing.version + 1) if existing is not None else 1,
            created_at=created_at,
            updated_at=applied_at,
        )
        records[patch.key.tuple_key] = new_record
    return ModelStateEvent(
        event_id=f"model-state-event-{uuid4()}",
        patch_id=patch.patch_id,
        key=patch.key,
        operation=patch.operation,
        value=dict(patch.value),
        applied_at=applied_at,
        reason=patch.reason,
        prior_version=prior_version,
        new_version=new_record.version if new_record is not None else None,
    )


def _matches(
    key: ModelStateKey,
    sleeve_id: str | None,
    model_id: str | None,
    namespace: str | None,
    symbol_key: str | None,
    position_id: str | None,
) -> bool:
    return (
        (sleeve_id is None or key.sleeve_id == sleeve_id)
        and (model_id is None or key.model_id == model_id)
        and (namespace is None or key.namespace == namespace)
        and (symbol_key is None or key.symbol_key == symbol_key)
        and (position_id is None or key.position_id == position_id)
    )


def _record_from_row(row: sqlite3.Row) -> ModelStateRecord:
    return ModelStateRecord(
        key=ModelStateKey(
            sleeve_id=row["sleeve_id"],
            model_id=row["model_id"],
            namespace=row["namespace"],
            symbol_key=row["symbol_key"],
            position_id=row["position_id"],
        ),
        value=json.loads(row["value_json"]),
        version=int(row["version"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        schema_version=row["schema_version"],
    )


def _event_from_row(row: sqlite3.Row) -> ModelStateEvent:
    return ModelStateEvent(
        event_id=row["event_id"],
        patch_id=row["patch_id"],
        key=ModelStateKey(
            sleeve_id=row["sleeve_id"],
            model_id=row["model_id"],
            namespace=row["namespace"],
            symbol_key=row["symbol_key"],
            position_id=row["position_id"],
        ),
        operation=StatePatchOperation(row["operation"]),
        value=json.loads(row["value_json"]),
        applied_at=_parse_datetime(row["applied_at"]),
        reason=row["reason"],
        prior_version=row["prior_version"],
        new_version=row["new_version"],
        schema_version=row["schema_version"],
    )


def _write_event(connection: sqlite3.Connection, event: ModelStateEvent) -> None:
    connection.execute(
        """
        INSERT INTO model_state_events (
            event_id, patch_id, sleeve_id, model_id, namespace, symbol_key,
            position_id, operation, value_json, applied_at, reason,
            prior_version, new_version, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.patch_id,
            event.key.sleeve_id,
            event.key.model_id,
            event.key.namespace,
            event.key.symbol_key,
            event.key.position_id,
            event.operation.value,
            _json_dumps(event.value),
            event.applied_at.isoformat(),
            event.reason,
            event.prior_version,
            event.new_version,
            event.schema_version,
        ),
    )


def _upsert_record(connection: sqlite3.Connection, record: ModelStateRecord) -> None:
    connection.execute(
        """
        INSERT INTO model_state (
            sleeve_id, model_id, namespace, symbol_key, position_id,
            value_json, version, created_at, updated_at, schema_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sleeve_id, model_id, namespace, symbol_key, position_id)
        DO UPDATE SET
            value_json = excluded.value_json,
            version = excluded.version,
            updated_at = excluded.updated_at,
            schema_version = excluded.schema_version
        """,
        (
            record.key.sleeve_id,
            record.key.model_id,
            record.key.namespace,
            record.key.symbol_key,
            record.key.position_id,
            _json_dumps(record.value),
            record.version,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
            record.schema_version,
        ),
    )


def _delete_record(connection: sqlite3.Connection, key: ModelStateKey) -> None:
    connection.execute(
        """
        DELETE FROM model_state
        WHERE sleeve_id = ? AND model_id = ? AND namespace = ?
          AND symbol_key = ? AND position_id = ?
        """,
        key.tuple_key,
    )


def _where_clause(filters: Mapping[str, str | None]) -> tuple[str, tuple[str, ...]]:
    clauses: list[str] = []
    values: list[str] = []
    for field_name, field_value in filters.items():
        if field_value is None:
            continue
        clauses.append(f"{field_name} = ?")
        values.append(field_value)
    if not clauses:
        return "", ()
    return " WHERE " + " AND ".join(clauses), tuple(values)


def _coerce_operation(value: StatePatchOperation | str) -> StatePatchOperation:
    if isinstance(value, StatePatchOperation):
        return value
    return StatePatchOperation(str(value or StatePatchOperation.MERGE.value).strip().lower())


def _json_normalized_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("model state value must be a mapping.")
    return json.loads(_json_dumps(value))


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(_plain_json_value(value), ensure_ascii=False, sort_keys=True)


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain_json_value(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.now()
    return datetime.fromisoformat(text)

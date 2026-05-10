from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from leaps_quant_engine.fundamentals.domain import FundamentalValue, PointInTimeFundamentalStore
from leaps_quant_engine.models import Symbol


FUNDAMENTAL_ARTIFACT_SCHEMA_VERSION = 1
FUNDAMENTAL_ARTIFACT_TYPE = "fundamental_snapshot"
DEFAULT_FUNDAMENTAL_ARTIFACT_ROOT = Path("data") / "fundamentals"


@dataclass(frozen=True, slots=True)
class FundamentalArtifact:
    market: str
    as_of: datetime
    created_at: datetime
    source: str
    values: Mapping[str, Mapping[str, FundamentalValue]]
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = FUNDAMENTAL_ARTIFACT_SCHEMA_VERSION
    artifact_type: str = FUNDAMENTAL_ARTIFACT_TYPE

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", _normalize_market(self.market))
        object.__setattr__(self, "source", str(self.source or ""))
        object.__setattr__(self, "values", _freeze_values(self.values))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @classmethod
    def from_values(
        cls,
        *,
        market: str,
        as_of: datetime,
        source: str,
        values: Mapping[Symbol | str, Mapping[str, float]],
        created_at: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "FundamentalArtifact":
        normalized_values: dict[str, dict[str, FundamentalValue]] = {}
        for symbol, symbol_values in values.items():
            symbol_key = symbol.key if isinstance(symbol, Symbol) else str(symbol)
            if not symbol_key:
                continue
            normalized_values[symbol_key] = {
                _normalize_name(name): FundamentalValue(
                    name=name,
                    value=value,
                    as_of=as_of,
                    reported_at=as_of,
                    effective_at=as_of,
                    source=source,
                    metadata={"artifact_market": _normalize_market(market)},
                )
                for name, value in symbol_values.items()
            }
        return cls(
            market=market,
            as_of=as_of,
            created_at=created_at or datetime.now(tz=as_of.tzinfo),
            source=source,
            values=normalized_values,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FundamentalArtifact":
        schema_version = int(payload.get("schema_version") or 0)
        if schema_version != FUNDAMENTAL_ARTIFACT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported fundamental artifact schema_version: {schema_version}")
        artifact_type = str(payload.get("artifact_type") or "")
        if artifact_type != FUNDAMENTAL_ARTIFACT_TYPE:
            raise ValueError(f"Unsupported fundamental artifact_type: {artifact_type}")
        values_payload = payload.get("values") or {}
        if not isinstance(values_payload, Mapping):
            raise ValueError("Fundamental artifact values must be an object.")
        values: dict[str, dict[str, FundamentalValue]] = {}
        for symbol_key, symbol_values in values_payload.items():
            if not isinstance(symbol_values, Mapping):
                continue
            values[str(symbol_key)] = {
                _normalize_name(name): _fundamental_value_from_dict(item)
                for name, item in symbol_values.items()
                if isinstance(item, Mapping)
            }
        return cls(
            market=str(payload.get("market") or ""),
            as_of=_parse_datetime(str(payload.get("as_of") or "")),
            created_at=_parse_datetime(str(payload.get("created_at") or "")),
            source=str(payload.get("source") or ""),
            values=values,
            metadata=dict(payload.get("metadata") or {}),
            schema_version=schema_version,
            artifact_type=artifact_type,
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.values))

    @property
    def names(self) -> tuple[str, ...]:
        names = {
            name
            for symbol_values in self.values.values()
            for name in symbol_values
        }
        return tuple(sorted(names))

    @property
    def symbol_count(self) -> int:
        return len(self.values)

    @property
    def value_count(self) -> int:
        return sum(len(symbol_values) for symbol_values in self.values.values())

    def to_store(self, store: PointInTimeFundamentalStore | None = None) -> PointInTimeFundamentalStore:
        resolved_store = store or PointInTimeFundamentalStore()
        for symbol_key, symbol_values in self.values.items():
            for item in symbol_values.values():
                resolved_store.add(
                    symbol_key,
                    item.name,
                    item.value,
                    as_of=item.as_of,
                    reported_at=item.reported_at,
                    effective_at=item.effective_at,
                    source=item.source,
                    stale_after=item.stale_after,
                    metadata=item.metadata,
                )
        return resolved_store

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "market": self.market,
            "as_of": self.as_of.isoformat(),
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "metadata": dict(self.metadata),
            "symbols": list(self.symbols),
            "names": list(self.names),
            "symbol_count": self.symbol_count,
            "value_count": self.value_count,
            "values": {
                symbol_key: {
                    name: item.to_dict()
                    for name, item in symbol_values.items()
                }
                for symbol_key, symbol_values in self.values.items()
            },
        }

    def summary(self, *, path: Path | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "market": self.market,
            "as_of": self.as_of.isoformat(),
            "created_at": self.created_at.isoformat(),
            "source": self.source,
            "symbol_count": self.symbol_count,
            "value_count": self.value_count,
            "names": list(self.names),
        }
        if path is not None:
            payload["path"] = str(path)
        return payload


@dataclass(frozen=True, slots=True)
class FundamentalArtifactRecord:
    path: Path
    artifact: FundamentalArtifact

    def summary(self) -> dict[str, Any]:
        return self.artifact.summary(path=self.path)


@dataclass(slots=True)
class FileFundamentalArtifactStore:
    root: Path = DEFAULT_FUNDAMENTAL_ARTIFACT_ROOT

    def artifact_path(self, market: str, as_of: datetime) -> Path:
        market_slug = _normalize_market(market).lower()
        return self.root / market_slug / f"{as_of.date().isoformat()}.json"

    def write(self, artifact: FundamentalArtifact, *, overwrite: bool = False) -> Path:
        path = self.artifact_path(artifact.market, artifact.as_of)
        if path.exists() and not overwrite:
            raise FileExistsError(f"Fundamental artifact already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    def read(
        self,
        *,
        market: str | None = None,
        as_of: datetime | None = None,
        path: Path | None = None,
    ) -> FundamentalArtifactRecord:
        resolved_path = path
        if resolved_path is None:
            if market is None or as_of is None:
                raise ValueError("market and as_of are required when path is not provided.")
            resolved_path = self.artifact_path(market, as_of)
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Fundamental artifact must be an object: {resolved_path}")
        return FundamentalArtifactRecord(path=resolved_path, artifact=FundamentalArtifact.from_dict(payload))

    def list_records(
        self,
        *,
        market: str | None = None,
        end: datetime | None = None,
    ) -> tuple[FundamentalArtifactRecord, ...]:
        paths = sorted(self._artifact_paths(market=market))
        records: list[FundamentalArtifactRecord] = []
        for path in paths:
            try:
                record = self.read(path=path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if end is not None and record.artifact.as_of > end:
                continue
            records.append(record)
        return tuple(records)

    def latest(self, *, market: str | None = None) -> FundamentalArtifactRecord | None:
        records = self.list_records(market=market)
        if not records:
            return None
        return max(records, key=lambda record: (record.artifact.as_of, record.artifact.created_at, str(record.path)))

    def load_to_store(
        self,
        store: PointInTimeFundamentalStore | None = None,
        *,
        market: str | None = None,
        end: datetime | None = None,
        names: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[PointInTimeFundamentalStore, tuple[FundamentalArtifactRecord, ...]]:
        resolved_store = store or PointInTimeFundamentalStore()
        allowed_names = {_normalize_name(name) for name in names} if names is not None else None
        records = self.list_records(market=market, end=end)
        for record in records:
            for symbol_key, symbol_values in record.artifact.values.items():
                for item in symbol_values.values():
                    if allowed_names is not None and item.name not in allowed_names:
                        continue
                    resolved_store.add(
                        symbol_key,
                        item.name,
                        item.value,
                        as_of=item.as_of,
                        reported_at=item.reported_at,
                        effective_at=item.effective_at,
                        source=item.source,
                        stale_after=item.stale_after,
                        metadata=item.metadata,
                    )
        return resolved_store, records

    def status(self, *, market: str | None = None, include_artifacts: bool = False) -> dict[str, Any]:
        records = self.list_records(market=market)
        latest_by_market: dict[str, FundamentalArtifactRecord] = {}
        for record in records:
            current = latest_by_market.get(record.artifact.market)
            if current is None or (record.artifact.as_of, record.artifact.created_at) > (
                current.artifact.as_of,
                current.artifact.created_at,
            ):
                latest_by_market[record.artifact.market] = record
        payload: dict[str, Any] = {
            "status": "ok" if records else "empty",
            "root": str(self.root),
            "market": _normalize_market(market) if market else None,
            "artifact_count": len(records),
            "markets": sorted(latest_by_market),
            "latest_by_market": {
                market_key: record.summary()
                for market_key, record in sorted(latest_by_market.items())
            },
        }
        if include_artifacts:
            payload["artifacts"] = [record.summary() for record in records]
        return payload

    def _artifact_paths(self, *, market: str | None) -> tuple[Path, ...]:
        if market:
            market_root = self.root / _normalize_market(market).lower()
            return tuple(sorted(market_root.glob("*.json")))
        return tuple(sorted(self.root.glob("*/*.json")))


def _fundamental_value_from_dict(payload: Mapping[str, Any]) -> FundamentalValue:
    return FundamentalValue(
        name=str(payload.get("name") or ""),
        value=float(payload.get("value")),
        as_of=_parse_datetime(str(payload.get("as_of") or "")),
        reported_at=_parse_optional_datetime(payload.get("reported_at")),
        effective_at=_parse_optional_datetime(payload.get("effective_at")),
        source=str(payload.get("source") or ""),
        stale_after=_parse_optional_datetime(payload.get("stale_after")),
        metadata=dict(payload.get("metadata") or {}),
    )


def _parse_optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(str(value))


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _normalize_market(value: str | None) -> str:
    return str(value or "").strip().upper()


def _normalize_name(value: str) -> str:
    return str(value or "").strip().lower()


def _freeze_values(
    values: Mapping[str, Mapping[str, FundamentalValue]],
) -> Mapping[str, Mapping[str, FundamentalValue]]:
    return MappingProxyType(
        {
            symbol_key: MappingProxyType(dict(symbol_values))
            for symbol_key, symbol_values in values.items()
        }
    )

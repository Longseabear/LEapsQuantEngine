from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
from types import MappingProxyType
from typing import Any, Mapping

from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(frozen=True, slots=True)
class FineUniverseEntry:
    symbol: Symbol
    bar: Bar | None = None
    updated_at: datetime | None = None
    failed_at: datetime | None = None
    failure_message: str | None = None
    refresh_count: int = 0

    def age_seconds(self, now: datetime | None = None) -> float | None:
        if self.updated_at is None:
            return None
        return max(((now or datetime.now()) - self.updated_at).total_seconds(), 0.0)

    def is_fresh(self, *, max_age_seconds: float, now: datetime | None = None) -> bool:
        age = self.age_seconds(now)
        return age is not None and age <= max_age_seconds

    def to_dict(self, *, now: datetime | None = None, max_age_seconds: float | None = None) -> dict[str, Any]:
        age = self.age_seconds(now)
        payload: dict[str, Any] = {
            "symbol": self.symbol.key,
            "has_bar": self.bar is not None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "age_seconds": age,
            "failed_at": self.failed_at.isoformat() if self.failed_at else None,
            "failure_message": self.failure_message,
            "refresh_count": self.refresh_count,
        }
        if self.bar is not None:
            payload["bar"] = {
                "time": self.bar.time.isoformat(),
                "open": self.bar.open,
                "high": self.bar.high,
                "low": self.bar.low,
                "close": self.bar.close,
                "volume": self.bar.volume,
            }
        if max_age_seconds is not None:
            payload["is_fresh"] = self.is_fresh(max_age_seconds=max_age_seconds, now=now)
        return payload


@dataclass(frozen=True, slots=True)
class FineUniverseRefreshFailure:
    symbol_key: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"symbol": self.symbol_key, "message": self.message}


@dataclass(frozen=True, slots=True)
class FineUniverseRefreshReport:
    universe_id: str
    source: str
    requested_symbol_count: int
    updated_symbol_count: int
    failed_symbol_count: int
    cached_symbol_count: int
    fresh_symbol_count: int
    max_age_seconds: float
    started_at: datetime
    completed_at: datetime
    elapsed_ms: float
    failures: tuple[FineUniverseRefreshFailure, ...] = ()

    def to_dict(self, *, include_failures: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "universe_id": self.universe_id,
            "source": self.source,
            "requested_symbol_count": self.requested_symbol_count,
            "updated_symbol_count": self.updated_symbol_count,
            "failed_symbol_count": self.failed_symbol_count,
            "cached_symbol_count": self.cached_symbol_count,
            "fresh_symbol_count": self.fresh_symbol_count,
            "max_age_seconds": self.max_age_seconds,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "elapsed_ms": self.elapsed_ms,
        }
        if include_failures:
            payload["failures"] = [failure.to_dict() for failure in self.failures]
        return payload


@dataclass(slots=True)
class FineUniverseCache:
    entries_by_key: dict[str, FineUniverseEntry] = field(default_factory=dict)

    def update_bar(self, bar: Bar, *, updated_at: datetime | None = None) -> FineUniverseEntry:
        previous = self.entries_by_key.get(bar.symbol.key)
        entry = FineUniverseEntry(
            symbol=bar.symbol,
            bar=bar,
            updated_at=updated_at or datetime.now(),
            refresh_count=(previous.refresh_count + 1) if previous else 1,
        )
        self.entries_by_key[bar.symbol.key] = entry
        return entry

    def mark_failure(self, symbol: Symbol, message: str, *, failed_at: datetime | None = None) -> FineUniverseEntry:
        previous = self.entries_by_key.get(symbol.key)
        entry = FineUniverseEntry(
            symbol=symbol,
            bar=previous.bar if previous else None,
            updated_at=previous.updated_at if previous else None,
            failed_at=failed_at or datetime.now(),
            failure_message=message,
            refresh_count=previous.refresh_count if previous else 0,
        )
        self.entries_by_key[symbol.key] = entry
        return entry

    def entry(self, symbol: Symbol | str) -> FineUniverseEntry | None:
        key = symbol.key if isinstance(symbol, Symbol) else symbol
        return self.entries_by_key.get(key)

    def cached_symbols(self) -> tuple[Symbol, ...]:
        return tuple(entry.symbol for entry in self.entries_by_key.values() if entry.bar is not None)

    def fresh_symbols(self, *, max_age_seconds: float, now: datetime | None = None) -> tuple[Symbol, ...]:
        return tuple(
            entry.symbol
            for entry in self.entries_by_key.values()
            if entry.bar is not None and entry.is_fresh(max_age_seconds=max_age_seconds, now=now)
        )

    def snapshot(self) -> Mapping[str, FineUniverseEntry]:
        return MappingProxyType(dict(self.entries_by_key))

    def to_dict(self, *, max_age_seconds: float | None = None) -> dict[str, Any]:
        now = datetime.now()
        return {
            symbol_key: entry.to_dict(now=now, max_age_seconds=max_age_seconds)
            for symbol_key, entry in self.entries_by_key.items()
        }

    def to_universe_definition(
        self,
        base_universe: UniverseDefinition,
        *,
        max_age_seconds: float,
        universe_id: str | None = None,
        now: datetime | None = None,
    ) -> UniverseDefinition:
        symbols = self.fresh_symbols(max_age_seconds=max_age_seconds, now=now)
        properties = {
            symbol.key: dict(base_universe.properties_for(symbol))
            for symbol in symbols
            if base_universe.properties_for(symbol)
        }
        return UniverseDefinition(
            id=universe_id or f"{base_universe.id}-fine",
            market=base_universe.market,
            symbols=symbols,
            indicators=base_universe.indicators,
            tags=(*base_universe.tags, "fine"),
            symbol_properties=properties,
        )


@dataclass(slots=True)
class FineUniverseRuntime:
    universe: UniverseDefinition
    provider: MarketDataProvider
    cache: FineUniverseCache = field(default_factory=FineUniverseCache)
    source: str = "market-data-engine"
    max_age_seconds: float = 300.0

    def refresh_once(
        self,
        *,
        symbols: tuple[Symbol, ...] | list[Symbol] | None = None,
        max_symbols: int | None = None,
        min_success: int | None = None,
    ) -> FineUniverseRefreshReport:
        target_symbols = list(symbols or self.universe.symbols)
        if max_symbols is not None:
            if max_symbols < 0:
                raise ValueError("max_symbols must be non-negative.")
            target_symbols = target_symbols[:max_symbols]
        started_at = datetime.now()
        started = time.perf_counter()
        failures: list[FineUniverseRefreshFailure] = []
        updated_count = 0
        for symbol in target_symbols:
            try:
                bar = self.provider.get_latest_bar(symbol)
            except Exception as exc:  # noqa: BLE001 - fine cache keeps stale entries and reports failures.
                failures.append(FineUniverseRefreshFailure(symbol.key, str(exc)))
                self.cache.mark_failure(symbol, str(exc))
                continue
            self.cache.update_bar(bar)
            updated_count += 1
        completed_at = datetime.now()
        if min_success is not None and updated_count < min_success:
            raise RuntimeError(f"Fine refresh updated {updated_count} symbols, below min_success={min_success}.")
        return FineUniverseRefreshReport(
            universe_id=self.universe.id,
            source=self.source,
            requested_symbol_count=len(target_symbols),
            updated_symbol_count=updated_count,
            failed_symbol_count=len(failures),
            cached_symbol_count=len(self.cache.cached_symbols()),
            fresh_symbol_count=len(self.cache.fresh_symbols(max_age_seconds=self.max_age_seconds, now=completed_at)),
            max_age_seconds=self.max_age_seconds,
            started_at=started_at,
            completed_at=completed_at,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            failures=tuple(failures),
        )

    def fine_universe_definition(self, *, universe_id: str | None = None) -> UniverseDefinition:
        return self.cache.to_universe_definition(
            self.universe,
            max_age_seconds=self.max_age_seconds,
            universe_id=universe_id,
        )

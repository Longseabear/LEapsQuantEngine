from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from types import MappingProxyType
from typing import Any, Mapping, Protocol

from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(frozen=True, slots=True)
class UniverseSelectionCandidate:
    symbol: Symbol
    score: float | None
    selected: bool = False
    forced: bool = False
    reasons: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol.key,
            "score": self.score,
            "selected": self.selected,
            "forced": self.forced,
            "reasons": list(self.reasons),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class UniverseSelectionContext:
    sleeve_id: str
    universe: UniverseDefinition
    indicator_snapshot: IndicatorSnapshot | None = None
    as_of: datetime | None = None
    previous_live_symbols: tuple[Symbol, ...] = ()
    held_symbols: tuple[Symbol, ...] = ()
    open_order_symbols: tuple[Symbol, ...] = ()
    exit_watch_symbols: tuple[Symbol, ...] = ()
    manual_symbols: tuple[Symbol, ...] = ()

    @property
    def generated_at(self) -> datetime:
        if self.as_of is not None:
            return self.as_of
        if self.indicator_snapshot is not None:
            return self.indicator_snapshot.as_of
        return datetime.now()

    @property
    def source_snapshot_id(self) -> str | None:
        return self.indicator_snapshot.snapshot_id if self.indicator_snapshot is not None else None

    @property
    def forced_symbols(self) -> tuple[Symbol, ...]:
        return _dedupe_symbols(
            [
                *self.held_symbols,
                *self.open_order_symbols,
                *self.exit_watch_symbols,
                *self.manual_symbols,
            ]
        )

    @property
    def forced_symbol_keys(self) -> set[str]:
        return {symbol.key for symbol in self.forced_symbols}


@dataclass(frozen=True, slots=True)
class UniverseSelectionResult:
    sleeve_id: str
    universe_id: str
    generated_at: datetime
    source_snapshot_id: str | None
    selection_id: str
    selected_symbols: tuple[Symbol, ...]
    forced_symbols: tuple[Symbol, ...]
    live_symbols: tuple[Symbol, ...]
    added_symbols: tuple[Symbol, ...]
    removed_symbols: tuple[Symbol, ...]
    retained_symbols: tuple[Symbol, ...]
    candidates: Mapping[str, UniverseSelectionCandidate]
    rejected: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", MappingProxyType(dict(self.candidates)))
        object.__setattr__(self, "rejected", MappingProxyType(dict(self.rejected)))

    def to_dict(self, *, include_candidates: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "selection_id": self.selection_id,
            "generated_at": self.generated_at.isoformat(),
            "source_snapshot_id": self.source_snapshot_id,
            "selected_count": len(self.selected_symbols),
            "forced_count": len(self.forced_symbols),
            "live_count": len(self.live_symbols),
            "added_count": len(self.added_symbols),
            "removed_count": len(self.removed_symbols),
            "retained_count": len(self.retained_symbols),
            "selected_symbols": [symbol.key for symbol in self.selected_symbols],
            "forced_symbols": [symbol.key for symbol in self.forced_symbols],
            "live_symbols": [symbol.key for symbol in self.live_symbols],
            "added_symbols": [symbol.key for symbol in self.added_symbols],
            "removed_symbols": [symbol.key for symbol in self.removed_symbols],
            "retained_symbols": [symbol.key for symbol in self.retained_symbols],
            "rejected": {symbol_key: list(reasons) for symbol_key, reasons in self.rejected.items()},
        }
        if include_candidates:
            payload["candidates"] = {
                symbol_key: candidate.to_dict()
                for symbol_key, candidate in self.candidates.items()
            }
        return payload

    def to_universe_definition(self, base_universe: UniverseDefinition, *, universe_id: str | None = None) -> UniverseDefinition:
        properties = {
            symbol.key: dict(base_universe.properties_for(symbol))
            for symbol in self.live_symbols
            if base_universe.properties_for(symbol)
        }
        return UniverseDefinition(
            id=universe_id or f"{base_universe.id}-active",
            market=base_universe.market,
            symbols=self.live_symbols,
            indicators=base_universe.indicators,
            tags=(*base_universe.tags, "active"),
            symbol_properties=properties,
        )


class UniverseSelectionModel(Protocol):
    selection_id: str

    def select(self, context: UniverseSelectionContext) -> UniverseSelectionResult:
        """Select active live symbols from a broader coarse universe."""


@dataclass(frozen=True, slots=True)
class CompositeUniverseSelectionResult:
    sleeve_id: str
    universe_id: str
    generated_at: datetime
    source_snapshot_id: str | None
    selections: Mapping[str, UniverseSelectionResult]
    selected_symbols: tuple[Symbol, ...]
    forced_symbols: tuple[Symbol, ...]
    live_symbols: tuple[Symbol, ...]
    added_symbols: tuple[Symbol, ...]
    removed_symbols: tuple[Symbol, ...]
    retained_symbols: tuple[Symbol, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "selections", MappingProxyType(dict(self.selections)))

    def symbols_for_selection(self, selection_id: str) -> tuple[Symbol, ...]:
        selection = self.selections.get(selection_id)
        return selection.selected_symbols if selection is not None else ()

    def to_dict(self, *, include_candidates: bool = True) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "universe_id": self.universe_id,
            "generated_at": self.generated_at.isoformat(),
            "source_snapshot_id": self.source_snapshot_id,
            "selection_ids": list(self.selections),
            "selected_count": len(self.selected_symbols),
            "forced_count": len(self.forced_symbols),
            "live_count": len(self.live_symbols),
            "added_count": len(self.added_symbols),
            "removed_count": len(self.removed_symbols),
            "retained_count": len(self.retained_symbols),
            "selected_symbols": [symbol.key for symbol in self.selected_symbols],
            "forced_symbols": [symbol.key for symbol in self.forced_symbols],
            "live_symbols": [symbol.key for symbol in self.live_symbols],
            "added_symbols": [symbol.key for symbol in self.added_symbols],
            "removed_symbols": [symbol.key for symbol in self.removed_symbols],
            "retained_symbols": [symbol.key for symbol in self.retained_symbols],
            "selections": {
                selection_id: selection.to_dict(include_candidates=include_candidates)
                for selection_id, selection in self.selections.items()
            },
        }

    def to_universe_definition(self, base_universe: UniverseDefinition, *, universe_id: str | None = None) -> UniverseDefinition:
        properties = {
            symbol.key: dict(base_universe.properties_for(symbol))
            for symbol in self.live_symbols
            if base_universe.properties_for(symbol)
        }
        return UniverseDefinition(
            id=universe_id or f"{base_universe.id}-active",
            market=base_universe.market,
            symbols=self.live_symbols,
            indicators=base_universe.indicators,
            tags=(*base_universe.tags, "active"),
            symbol_properties=properties,
        )


@dataclass(frozen=True, slots=True)
class StaticUniverseSelectionModel:
    max_active_symbols: int = 60
    selection_id: str = "static-top-n"

    def select(self, context: UniverseSelectionContext) -> UniverseSelectionResult:
        if self.max_active_symbols < 0:
            raise ValueError("max_active_symbols must be non-negative.")
        selected = tuple(context.universe.symbols[: self.max_active_symbols])
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=None,
                selected=symbol in selected,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("static_order",) if symbol in selected else (),
            )
            for symbol in context.universe.symbols
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected={},
        )


@dataclass(frozen=True, slots=True)
class MomentumUniverseSelectionModel:
    max_active_symbols: int = 60
    selection_id: str = "momentum-active-selection"
    price_indicator: str = "identity_close"
    moving_average_indicator: str = "sma_5_close"
    momentum_indicator: str = "momentum_5_close"
    liquidity_indicator: str = "rolling_dollar_volume_20"
    volatility_indicator: str | None = "stddev_20_close"
    require_positive_momentum: bool = True
    require_price_above_average: bool = False
    min_liquidity: float | None = None
    max_volatility: float | None = None
    liquidity_weight: float = 0.45
    momentum_weight: float = 0.35
    trend_weight: float = 0.15
    volatility_penalty_weight: float = 0.05

    def select(self, context: UniverseSelectionContext) -> UniverseSelectionResult:
        if self.max_active_symbols < 0:
            raise ValueError("max_active_symbols must be non-negative.")
        if context.indicator_snapshot is None:
            rejected = {
                symbol.key: ("missing_indicator_snapshot",)
                for symbol in context.universe.symbols
            }
            return build_universe_selection_result(
                context,
                (),
                selection_id=self.selection_id,
                candidates={},
                rejected=rejected,
            )

        eligible: list[dict[str, Any]] = []
        rejected: dict[str, tuple[str, ...]] = {}
        for symbol in context.universe.symbols:
            item, reasons = self._candidate_values(context.indicator_snapshot, symbol)
            if reasons:
                rejected[symbol.key] = tuple(reasons)
                continue
            eligible.append(item)

        liquidity_ranks = _percent_ranks({item["symbol"].key: item["liquidity"] for item in eligible})
        momentum_ranks = _percent_ranks({item["symbol"].key: item["momentum"] for item in eligible})
        volatility_ranks = _percent_ranks(
            {
                item["symbol"].key: item["volatility"]
                for item in eligible
                if item.get("volatility") is not None
            }
        )

        candidates: dict[str, UniverseSelectionCandidate] = {}
        for item in eligible:
            symbol = item["symbol"]
            liquidity_rank = liquidity_ranks.get(symbol.key, 0.0)
            momentum_rank = momentum_ranks.get(symbol.key, 0.0)
            volatility_rank = volatility_ranks.get(symbol.key, 0.0)
            trend_bonus = 1.0 if item["price_above_average"] else 0.0
            score = (
                (liquidity_rank * self.liquidity_weight)
                + (momentum_rank * self.momentum_weight)
                + (trend_bonus * self.trend_weight)
                - (volatility_rank * self.volatility_penalty_weight)
            )
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=score,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=tuple(item["reasons"]),
                metadata={
                    "liquidity": item["liquidity"],
                    "momentum": item["momentum"],
                    "price": item["price"],
                    "moving_average": item["moving_average"],
                    "volatility": item.get("volatility"),
                    "liquidity_rank": liquidity_rank,
                    "momentum_rank": momentum_rank,
                    "volatility_rank": volatility_rank,
                    "price_above_average": item["price_above_average"],
                },
            )

        selected = tuple(
            candidate.symbol
            for candidate in sorted(
                candidates.values(),
                key=lambda candidate: (candidate.score if candidate.score is not None else -math.inf, candidate.symbol.key),
                reverse=True,
            )[: self.max_active_symbols]
        )
        selected_keys = {symbol.key for symbol in selected}
        candidates = {
            symbol_key: UniverseSelectionCandidate(
                symbol=candidate.symbol,
                score=candidate.score,
                selected=symbol_key in selected_keys,
                forced=candidate.forced,
                reasons=candidate.reasons,
                metadata=candidate.metadata,
            )
            for symbol_key, candidate in candidates.items()
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )

    def _candidate_values(self, snapshot: IndicatorSnapshot, symbol: Symbol) -> tuple[dict[str, Any], list[str]]:
        reasons: list[str] = []
        price = snapshot.value(symbol.key, self.price_indicator)
        moving_average = snapshot.value(symbol.key, self.moving_average_indicator)
        momentum = snapshot.value(symbol.key, self.momentum_indicator)
        liquidity = snapshot.value(symbol.key, self.liquidity_indicator)
        volatility = snapshot.value(symbol.key, self.volatility_indicator) if self.volatility_indicator else None

        if price is None:
            reasons.append(f"missing_{self.price_indicator}")
        if moving_average is None:
            reasons.append(f"missing_{self.moving_average_indicator}")
        if momentum is None:
            reasons.append(f"missing_{self.momentum_indicator}")
        if liquidity is None:
            reasons.append(f"missing_{self.liquidity_indicator}")
        if self.volatility_indicator and volatility is None:
            reasons.append(f"missing_{self.volatility_indicator}")
        if reasons:
            return {}, reasons

        if self.min_liquidity is not None and liquidity < self.min_liquidity:
            reasons.append("liquidity_below_min")
        if self.max_volatility is not None and volatility is not None and volatility > self.max_volatility:
            reasons.append("volatility_above_max")
        if self.require_positive_momentum and momentum <= 0:
            reasons.append("momentum_not_positive")
        price_above_average = price > moving_average
        if self.require_price_above_average and not price_above_average:
            reasons.append("price_not_above_average")
        if reasons:
            return {}, reasons

        return {
            "symbol": symbol,
            "price": price,
            "moving_average": moving_average,
            "momentum": momentum,
            "liquidity": liquidity,
            "volatility": volatility,
            "price_above_average": price_above_average,
            "reasons": ("momentum_selection",),
        }, []


def build_universe_selection_result(
    context: UniverseSelectionContext,
    selected_symbols: tuple[Symbol, ...],
    *,
    selection_id: str = "selection",
    candidates: Mapping[str, UniverseSelectionCandidate],
    rejected: Mapping[str, tuple[str, ...]],
) -> UniverseSelectionResult:
    selected = _dedupe_symbols(selected_symbols)
    forced = context.forced_symbols
    live = _dedupe_symbols([*selected, *forced])
    previous_keys = {symbol.key for symbol in context.previous_live_symbols}
    live_keys = {symbol.key for symbol in live}
    added = tuple(symbol for symbol in live if symbol.key not in previous_keys)
    retained = tuple(symbol for symbol in live if symbol.key in previous_keys)
    removed = tuple(symbol for symbol in context.previous_live_symbols if symbol.key not in live_keys)
    enriched_candidates = dict(candidates)
    for symbol in forced:
        existing = enriched_candidates.get(symbol.key)
        rejected_reasons = tuple(rejected.get(symbol.key, ()))
        enriched_candidates[symbol.key] = UniverseSelectionCandidate(
            symbol=symbol,
            score=existing.score if existing is not None else None,
            selected=existing.selected if existing is not None else False,
            forced=True,
            reasons=(
                *(existing.reasons if existing is not None else ()),
                *rejected_reasons,
                "forced_watchlist",
            ),
            metadata=existing.metadata if existing is not None else {},
        )
    return UniverseSelectionResult(
        sleeve_id=context.sleeve_id,
        universe_id=context.universe.id,
        generated_at=context.generated_at,
        source_snapshot_id=context.source_snapshot_id,
        selection_id=selection_id,
        selected_symbols=selected,
        forced_symbols=forced,
        live_symbols=live,
        added_symbols=added,
        removed_symbols=removed,
        retained_symbols=retained,
        candidates=enriched_candidates,
        rejected=rejected,
    )


def build_composite_universe_selection_result(
    context: UniverseSelectionContext,
    selections: tuple[UniverseSelectionResult, ...] | list[UniverseSelectionResult],
) -> CompositeUniverseSelectionResult:
    selections_by_id: dict[str, UniverseSelectionResult] = {}
    selected_inputs: list[Symbol] = []
    for selection in selections:
        if selection.sleeve_id != context.sleeve_id:
            raise ValueError("Selection result sleeve_id does not match context.")
        if selection.universe_id != context.universe.id:
            raise ValueError("Selection result universe_id does not match context.")
        if selection.selection_id in selections_by_id:
            raise ValueError(f"Duplicate selection_id: {selection.selection_id}")
        selections_by_id[selection.selection_id] = selection
        selected_inputs.extend(selection.selected_symbols)

    selected = _dedupe_symbols(selected_inputs)
    forced = context.forced_symbols
    live = _dedupe_symbols([*selected, *forced])
    previous_keys = {symbol.key for symbol in context.previous_live_symbols}
    live_keys = {symbol.key for symbol in live}
    added = tuple(symbol for symbol in live if symbol.key not in previous_keys)
    retained = tuple(symbol for symbol in live if symbol.key in previous_keys)
    removed = tuple(symbol for symbol in context.previous_live_symbols if symbol.key not in live_keys)
    return CompositeUniverseSelectionResult(
        sleeve_id=context.sleeve_id,
        universe_id=context.universe.id,
        generated_at=context.generated_at,
        source_snapshot_id=context.source_snapshot_id,
        selections=selections_by_id,
        selected_symbols=selected,
        forced_symbols=forced,
        live_symbols=live,
        added_symbols=added,
        removed_symbols=removed,
        retained_symbols=retained,
    )


def _dedupe_symbols(symbols: list[Symbol] | tuple[Symbol, ...]) -> tuple[Symbol, ...]:
    result: list[Symbol] = []
    seen: set[str] = set()
    for symbol in symbols:
        if symbol.key in seen:
            continue
        seen.add(symbol.key)
        result.append(symbol)
    return tuple(result)


def _percent_ranks(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    sorted_items = sorted(values.items(), key=lambda item: (item[1], item[0]))
    if len(sorted_items) == 1:
        return {sorted_items[0][0]: 1.0}
    denominator = len(sorted_items) - 1
    return {
        symbol_key: index / denominator
        for index, (symbol_key, _) in enumerate(sorted_items)
    }

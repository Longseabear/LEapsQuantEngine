from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.selection import (
    CompositeUniverseSelectionResult,
    UniverseSelectionContext,
    UniverseSelectionModel,
    UniverseSelectionResult,
    build_composite_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class ActiveUniverseResult:
    selection: UniverseSelectionResult | CompositeUniverseSelectionResult
    active_universe: UniverseDefinition


@dataclass(frozen=True, slots=True)
class UniverseSelectionRuntime:
    coarse_universe: UniverseDefinition
    selection_model: UniverseSelectionModel

    def select_active(
        self,
        *,
        sleeve_id: str,
        indicator_snapshot: IndicatorSnapshot | None = None,
        as_of: datetime | None = None,
        previous_live_symbols: tuple[Symbol, ...] = (),
        held_symbols: tuple[Symbol, ...] = (),
        open_order_symbols: tuple[Symbol, ...] = (),
        exit_watch_symbols: tuple[Symbol, ...] = (),
        manual_symbols: tuple[Symbol, ...] = (),
        active_universe_id: str | None = None,
    ) -> ActiveUniverseResult:
        context = UniverseSelectionContext(
            sleeve_id=sleeve_id,
            universe=self.coarse_universe,
            indicator_snapshot=indicator_snapshot,
            as_of=as_of,
            previous_live_symbols=previous_live_symbols,
            held_symbols=held_symbols,
            open_order_symbols=open_order_symbols,
            exit_watch_symbols=exit_watch_symbols,
            manual_symbols=manual_symbols,
        )
        selection = self.selection_model.select(context)
        return ActiveUniverseResult(
            selection=selection,
            active_universe=selection.to_universe_definition(
                self.coarse_universe,
                universe_id=active_universe_id,
            ),
        )


@dataclass(frozen=True, slots=True)
class CompositeUniverseSelectionRuntime:
    coarse_universe: UniverseDefinition
    selection_models: tuple[UniverseSelectionModel, ...]

    def __post_init__(self) -> None:
        if not self.selection_models:
            raise ValueError("selection_models must not be empty.")
        seen: set[str] = set()
        for model in self.selection_models:
            selection_id = getattr(model, "selection_id", None)
            if not selection_id:
                raise ValueError("Each selection model must provide selection_id.")
            if selection_id in seen:
                raise ValueError(f"Duplicate selection_id: {selection_id}")
            seen.add(selection_id)

    def select_active(
        self,
        *,
        sleeve_id: str,
        indicator_snapshot: IndicatorSnapshot | None = None,
        as_of: datetime | None = None,
        previous_live_symbols: tuple[Symbol, ...] = (),
        held_symbols: tuple[Symbol, ...] = (),
        open_order_symbols: tuple[Symbol, ...] = (),
        exit_watch_symbols: tuple[Symbol, ...] = (),
        manual_symbols: tuple[Symbol, ...] = (),
        active_universe_id: str | None = None,
    ) -> ActiveUniverseResult:
        context = UniverseSelectionContext(
            sleeve_id=sleeve_id,
            universe=self.coarse_universe,
            indicator_snapshot=indicator_snapshot,
            as_of=as_of,
            previous_live_symbols=previous_live_symbols,
            held_symbols=held_symbols,
            open_order_symbols=open_order_symbols,
            exit_watch_symbols=exit_watch_symbols,
            manual_symbols=manual_symbols,
        )
        selection = build_composite_universe_selection_result(
            context,
            tuple(model.select(context) for model in self.selection_models),
        )
        return ActiveUniverseResult(
            selection=selection,
            active_universe=selection.to_universe_definition(
                self.coarse_universe,
                universe_id=active_universe_id,
            ),
        )

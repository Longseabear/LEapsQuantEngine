from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from leaps_quant_engine.models import Symbol
from leaps_quant_engine.snapshots import IndicatorSnapshot
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.selection import (
    UniverseSelectionContext,
    UniverseSelectionModel,
    UniverseSelectionResult,
)


@dataclass(frozen=True, slots=True)
class ActiveUniverseResult:
    selection: UniverseSelectionResult
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

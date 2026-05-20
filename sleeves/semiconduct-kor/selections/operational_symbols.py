from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class OperationalSymbolsSelectionModel:
    selection_id: str = "semiconduct-kor-operational-symbols"

    def select(self, context: UniverseSelectionContext):
        selected = context.forced_symbols
        selected_keys = {symbol.key for symbol in selected}
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=None,
                selected=symbol.key in selected_keys,
                forced=True,
                reasons=("operational_symbol",),
            )
            for symbol in selected
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected={},
        )

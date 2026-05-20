from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


SAMSUNG_SYMBOL_KEY = "KRX:005930"


@dataclass(frozen=True, slots=True)
class SamsungCoreSelectionModel:
    selection_id: str = "semiconduct-kor-samsung-core"

    def select(self, context: UniverseSelectionContext):
        selected = tuple(symbol for symbol in context.universe.symbols if symbol.key == SAMSUNG_SYMBOL_KEY)
        selected_keys = {symbol.key for symbol in selected}
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=1.0,
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("samsung_core_position",),
            )
            for symbol in selected
        }
        rejected = {
            symbol.key: ("not_samsung_core",)
            for symbol in context.universe.symbols
            if symbol.key != SAMSUNG_SYMBOL_KEY
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )

from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


MEMORY_LEADER_SYMBOL_KEYS = ("KRX:005930", "KRX:000660")
MEMORY_LEADER_REASONS = {
    "KRX:005930": ("memory_leader_core", "samsung_electronics"),
    "KRX:000660": ("memory_leader_core", "sk_hynix"),
}


@dataclass(frozen=True, slots=True)
class SamsungCoreSelectionModel:
    selection_id: str = "semiconduct-kor-memory-leaders-core"

    def select(self, context: UniverseSelectionContext):
        selected = tuple(symbol for symbol in context.universe.symbols if symbol.key in MEMORY_LEADER_SYMBOL_KEYS)
        selected_keys = {symbol.key for symbol in selected}
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=1.0 - (MEMORY_LEADER_SYMBOL_KEYS.index(symbol.key) * 0.01),
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=MEMORY_LEADER_REASONS.get(symbol.key, ("memory_leader_core",)),
            )
            for symbol in selected
        }
        rejected = {
            symbol.key: ("not_memory_leader_core",)
            for symbol in context.universe.symbols
            if symbol.key not in MEMORY_LEADER_SYMBOL_KEYS
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )

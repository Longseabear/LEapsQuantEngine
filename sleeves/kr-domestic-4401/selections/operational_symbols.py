from dataclasses import dataclass

from leaps_quant_engine.universe.selection import UniverseSelectionContext, build_universe_selection_result


@dataclass(frozen=True, slots=True)
class OperationalSymbolsSelectionModel:
    selection_id: str = "kr-domestic-4401-operational-symbols"

    def select(self, context: UniverseSelectionContext):
        forced = tuple(symbol for symbol in context.forced_symbols if symbol.key.startswith("KRX:"))
        return build_universe_selection_result(
            context,
            forced,
            selection_id=self.selection_id,
            candidates={},
            rejected={},
        )


def create_selection_model(params):
    return OperationalSymbolsSelectionModel(
        selection_id=str(params.get("selection_id", "kr-domestic-4401-operational-symbols"))
    )
